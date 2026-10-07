# Web Chat Specification

DRAFT FOR APPROVAL. Items marked **(decision)** wait for the owner's choice.

A second chat interface for Jarvis, built on assistant-ui (React, MIT licence) and shown inside the desktop app. It adds a model selector and projects (named groups of chats). The Qt chat window (`src/desktop_app/chat_window.spec.md`) stays; a setting chooses which one the tray's `Chat` entry opens.

## Principles

- **Off by default.** `web_chat_enabled` is `false`. When it is off, no socket is opened and nothing in this package runs.
- **Offline and local.** The React app is built once into static files shipped with Jarvis. Running Jarvis needs no Node, no CDN and no hosted service (no assistant-ui Cloud). The page loads nothing from the network, and there is no telemetry or analytics.
- **Loopback only.** The server listens on `127.0.0.1` and applies the Memory Viewer's Host and Origin checks to every request (below). It adds no network exposure.
- **Same text path.** Every message runs through `jarvis.daemon.submit_text_query`, so redaction, the fast path, the one-query-at-a-time lock, central safety and desktop confirmations apply unchanged. The web chat never runs tools itself.
- **Privacy.** Stored messages are the redacted text the dialogue memory holds, never what was typed. The owner can delete a chat or everything. Nothing is sent anywhere.
- **Voice shares the open chat (decision).** See **Voice**.

## Where it runs

The server runs in the daemon process, like Phone Access (`remote/remote.spec.md`), because the dialogue memory, the query lock, the reply mode and the local model all live there. This works the same when the daemon is bundled in the desktop app and when it is a subprocess. The desktop app only embeds the page in a `QWebEngineView`.

| Module | Responsibility |
|--------|----------------|
| `jarvis/memory/chat_store.py` | `ChatStore`: projects, chats and messages in the Jarvis database file. Own connection and schema, following `ActivityStore` (`memory/db.spec.md`) |
| `jarvis/webchat/backend.py` | `DaemonBackend`: the only module that touches `jarvis.daemon`, `bridge.modes` and the LLM backend. Tests replace it with a fake |
| `jarvis/webchat/hub.py` | `ChatHub`: mirrors the dialogue memory into the open chat, query tracking, assistant state, change notification for long polls |
| `jarvis/webchat/server.py` | Threaded HTTP server (standard library), routing, guards, static files |
| `jarvis/webchat/runtime.py` | `start(cfg)` / `stop()`: called by the daemon; starts nothing when disabled |
| `jarvis/webchat/static/` | The built React app. Committed, so a source checkout needs no Node |
| `webchat-ui/` (repository root) | The JavaScript project: sources, `package.json`, `package-lock.json`, build script. Python never imports it and Python tests never need Node |
| `desktop_app/web_chat_window.py` | `WebChatWindow`: HUD-framed window holding the `QWebEngineView`, lifecycle page when the daemon is not running |

`jarvis` knows nothing about `desktop_app`.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `web_chat_enabled` | `false` | Serve the web chat and let the tray open it. Restart Jarvis after changing it |
| `web_chat_port` | `8766` | Loopback TCP port, 1024 to 65535 |

A port that cannot be bound prints one warning at start-up; the Qt chat and the rest of Jarvis are unaffected.

## Server guards

Applied before routing, to every request:

1. `Host` must be a loopback name (`localhost`, `127.0.0.1`, `::1`) or the request gets `403` (stops DNS rebinding).
2. A request carrying `Origin` is served only when that origin is the server itself (stops cross-site requests).
3. Requests with a body must be `application/json` (a cross-site page cannot send that without a preflight, which is refused), and bodies over 64 KiB get `413`.

The check is one shared function that the Memory Viewer also uses. Responses carry `Content-Security-Policy: default-src 'self'` (plus `style-src 'self' 'unsafe-inline'` only if the build needs it), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and `Cache-Control: no-store` for the API.

## Data

Tables in the Jarvis database file (`db_path`), created with `CREATE TABLE IF NOT EXISTS` by `ChatStore`:

- `chat_projects(id, name, position, created_at)`
- `chats(id, project_id NULL, title, created_at, updated_at, last_mode, last_model)`
- `chat_messages(id, chat_id, role, content, ts, source)` where `source` is `typed`, `voice` or `confirmed`
- `chat_state(key, value)`: holds `active_chat_id`

A chat with no project is "Unfiled". Deleting a project keeps its chats and moves them to Unfiled. Deleting a chat deletes its messages. A chat's title is the first 60 characters of its first message until the owner renames it (no model call, so `docs/llm_contexts.md` is unaffected). Identifiers are random hex strings.

## Chats and the shared memory

Jarvis has one dialogue memory, so exactly one chat is *open*: the one whose turns the memory holds. Opening another chat (or creating one) does this, refused with `409 busy` while a query runs:

1. The outgoing conversation's unsaved turns go to the diary through the normal session-end path, so nothing is lost and nothing is summarised twice.
2. The memory is replaced by the chosen chat's last turns, marked as already saved (`DialogueMemory.set_messages(..., saved=True)`). Caches and tool carryover are cleared.
3. `active_chat_id` is stored, and the hub's mirror cursor moves to the new memory.

The hub mirrors every turn the memory gains into the open chat with `DialogueMemory.messages_after`, exactly as Phone Access does. Typed replies, voice turns and the outcome of a confirmed action therefore all land the same way, once. Local notices (busy, no reply, stopped) are shown but never stored.

## Voice **(decision)**

Recommended: **voice goes into the open chat**. A spoken request and a typed follow-up stay one conversation, as today. The page shows which chat voice is joining. With the window closed, voice joins the last open chat, and a first run opens a chat called "Voice and quick questions".

A separate permanent voice chat is not possible without a second dialogue memory, so it is not offered.

## Models **(decision)**

The model selector in the composer lists:

- the reply modes allowed in Settings (`bridge.modes.enabled_modes`): Local, ChatGPT (Codex), Claude;
- for Local, the offered chat models (`OFFERED_CHAT_MODELS`) that the runtime reports as installed (`list_models`), plus the current one. On an OpenAI-compatible provider only the current model is listed.

Choosing a reply mode calls `bridge.modes.switch` (so a mode that is not allowed is refused, a request in flight is cancelled and the choice is persisted, as the tray does). Choosing a local model changes the model for the whole assistant, voice included, and is persisted to the configuration, because the model is loaded once and shared. A live swap replaces the daemon's settings object and the listener's, loads the new model and releases the old one.

Recommended: each chat **remembers** the mode and model it last used (`last_mode`, `last_model`) and shows them, but opening a chat never switches anything on its own. If they differ from the current ones, the page offers one tap to switch back. A cloud mode is never entered by opening a chat.

## HTTP API

All JSON. Static files need no authentication because the server is loopback-only and holds no data in them.

| Method and path | Body | Result |
|-----------------|------|--------|
| `GET /api/state` | | Daemon readiness, reply mode and enabled modes, local model, offered and installed models, open chat id, assistant state |
| `GET /api/projects` / `POST /api/projects` | `{"name"}` | List / create |
| `PATCH /api/projects/<id>` / `DELETE` | `{"name"}` | Rename / delete (chats move to Unfiled) |
| `GET /api/chats?project=<id\|none>` | | Chats, newest first, optionally one project |
| `POST /api/chats` | `{"project_id"?}` | Create and open a chat |
| `GET /api/chats/<id>` | | The chat and its messages |
| `PATCH /api/chats/<id>` | `{"title"?, "project_id"?}` | Rename, move |
| `DELETE /api/chats/<id>` | | Delete |
| `POST /api/chats/<id>/open` | | Make it the open chat; `409` when busy |
| `POST /api/chat` | `{"text"}` | Send to the open chat: `202 {"query_id"}`, `409` busy, `503` daemon not ready, `400` empty or over 4000 characters |
| `POST /api/stop` | | `cancel_active_chat_query` |
| `POST /api/model` | `{"kind": "mode"\|"local", "value"}` | Switch; `409` busy or refused, with the reason |
| `GET /api/poll?after=<id>&rev=<rev>` | | Long poll (25 s) returning the changed state and new messages of the open chat |

## The page

The assistant-ui `ExternalStoreRuntime` holds the open chat's messages, which the server owns; the thread list comes from the chats API. Projects are a sidebar of our own (assistant-ui has no projects) listing projects with their chats, with create, rename, move (drag or menu) and delete. The look is Jarvis's HUD: the server fills the page's CSS variables from `HUD_COLORS` and its icons from `LINE_ICONS` (as the Memory Viewer does), so there is one palette. The composer carries the model selector, Send, and Stop while a query runs. Replies are plain text with Markdown. The page animates only while visible and honours reduced motion.

Desktop confirmations still appear as the desktop dialog, as in the Qt chat (they exist only when the daemon is bundled in the desktop app). The reply shows "Please confirm on your desktop" and the outcome arrives as a normal assistant message.

## Desktop window

`WebChatWindow` is a themed `QMainWindow` (`desktop_app.spec.md`, Theme System) with a `QWebEngineView` on `http://127.0.0.1:<web_chat_port>/`. While the daemon is starting, stopped or crashed it shows a themed page saying so instead of loading. The tray `Chat` entry opens it when `web_chat_enabled` is true and the Qt chat otherwise. When Qt WebEngine is unavailable it opens the page in the default browser. The Qt chat stays until the owner decides to retire it.

## Privacy and logging

- Stored text is already redacted. No message text, title, project name, path or token appears in logs or issue reports; `webchat` debug logs record start, stop, bind failures, refused requests and counts.
- Both the chat history and the diary are separate stores; deleting a chat does not edit the diary.
- The built bundle contains no absolute URLs; a test checks this.

## Building the page

`webchat-ui/README.md` documents: `npm ci`, `npm run build` (writes `jarvis/webchat/static/`), and `npm run dev` against a running Jarvis. The lockfile is committed. The build output is committed and is what `jarvis_desktop.spec` bundles.

## What it does not do (version 1)

- No streaming: the engine returns whole replies.
- No file or image attachments, no voice input in the page, no sharing, no search across chats.
- No sign-in: access is loopback only.
- No phone access: the phone app keeps its own conversation view.

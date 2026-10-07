# Web Chat Specification

A second chat interface for Jarvis, built on assistant-ui (React, MIT licence) and shown inside the desktop app. It adds a model selector and projects (named groups of chats). The Qt chat window (`src/desktop_app/chat_window.spec.md`) stays; the tray's `Chat` entry opens the web chat while `web_chat_enabled` is on and the Qt chat otherwise.

## Principles

- **Off by default.** `web_chat_enabled` is `false`. When it is off, no socket is opened and nothing in this package runs.
- **Offline and local.** The React app is built once into static files shipped with Jarvis. Running Jarvis needs no Node, no CDN and no hosted service (no assistant-ui Cloud). The page loads nothing from the network, and there is no telemetry or analytics.
- **Loopback only.** The server listens on `127.0.0.1` and applies the Memory Viewer's Host and Origin checks to every request. It adds no network exposure.
- **Same text path.** Every message runs through `jarvis.daemon.submit_text_query`, so redaction, the fast path, the one-query-at-a-time lock, central safety and desktop confirmations apply unchanged. The web chat never runs tools itself.
- **Privacy.** Stored messages are the redacted text the dialogue memory holds, never what was typed. The owner can delete a chat or everything. Nothing is sent anywhere.
- **Voice shares the open chat.** See **Chats and the shared memory**.

## Where it runs

The server runs in the daemon process, like Phone Access (`remote/remote.spec.md`), because the dialogue memory, the query lock, the reply mode and the local model all live there. It works the same when the daemon is bundled in the desktop app and when it is a subprocess. The desktop app only embeds the page in a `QWebEngineView`.

| Module | Responsibility |
|--------|----------------|
| `jarvis/memory/chat_store.py` | `ChatStore`: projects, chats and messages in the Jarvis database file. Own connection and schema, following `ActivityStore` (`memory/db.spec.md`) |
| `jarvis/utils/local_guard.py` | `refusal(host, origin)`: the Host and Origin checks shared with the Memory Viewer |
| `jarvis/webchat/backend.py` | `DaemonBackend`: the only module that touches `jarvis.daemon`, `bridge.modes` and the LLM backend. Tests replace it with a fake |
| `jarvis/webchat/hub.py` | `ChatHub`: the open chat, mirroring of the dialogue memory into it, query tracking, notices, change notification for long polls |
| `jarvis/webchat/server.py` | Threaded HTTP server (standard library), routing, guards, static files |
| `jarvis/webchat/runtime.py` | `start(cfg)` / `stop()`: called by the daemon; starts nothing when disabled |
| `jarvis/webchat/static/` | The built React app. Committed, so a source checkout needs no Node |
| `webchat-ui/` (repository root) | The JavaScript project: sources, `package.json`, `package-lock.json`, `README.md` with the build steps. Python never imports it and Python tests never need Node |
| `desktop_app/web_chat_theme.py` | Writes `webchat-ui/src/generated/theme.css` from `HUD_COLORS`; a test fails when the committed file is stale |
| `desktop_app/web_chat_window.py` | `WebChatWindow`: the window holding the `QWebEngineView`, status page while the daemon is not running |

`jarvis` knows nothing about `desktop_app`.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `web_chat_enabled` | `false` | Serve the web chat and let the tray open it. Restart Jarvis after changing it |
| `web_chat_port` | `8766` | Loopback TCP port, 1024 to 65535 |

A port that cannot be bound prints one warning at start-up and the web chat stays off; the Qt chat and the rest of Jarvis are unaffected.

## Server guards

Applied to every request before routing:

1. `Host` must be a loopback name (`localhost`, `127.0.0.1`, `::1`), or the request gets `403` (stops DNS rebinding).
2. A request carrying `Origin` is served only when that origin is the server itself, and a request marked `Sec-Fetch-Site: cross-site` is refused (stops cross-site requests).
3. A request with a body must be `application/json` (`415` otherwise) and at most 64 KiB (`413`).

Responses carry `Content-Security-Policy: default-src 'self'` (images may also be `data:` and styles inline), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and `Cache-Control: no-store` for the API. Static files are served only from the built `index.html` and the `assets/` folder; nothing else on disk is reachable.

## Data

Tables in the Jarvis database file (`db_path`), created with `CREATE TABLE IF NOT EXISTS` by `ChatStore`, with deleted rows overwritten (`secure_delete`):

- `chat_projects(id, name, position, created_at)`
- `chats(id, project_id NULL, title, created_at, updated_at, last_mode, last_model)`
- `chat_messages(id, chat_id, role, content, ts, source, private)` where `source` is `typed`, `voice` or `confirmed`, and `private` marks a turn that quotes the activity log
- `chat_state(key, value)`: holds `active_chat_id`

A chat with no project is unfiled. Deleting a project keeps its chats and unfiles them. Deleting a chat deletes its messages. A chat's title is the first 60 characters of its first message until the owner renames it (no model call, so `docs/llm_contexts.md` is unaffected). A turn marked `private` keeps its flag, so reopening the chat keeps it out of the diary and out of any context sent off the PC. Identifiers are random hex strings.

## Chats and the shared memory

Jarvis has one dialogue memory, so exactly one chat is *open*: the one whose turns the memory holds. **Voice goes into the open chat**, so a spoken request and a typed follow-up stay one conversation. The page says so ("Voice joins this chat"). With the window closed, voice still joins the chat that was open last. A first run opens a chat called "Voice and quick questions".

Opening another chat, or creating one, calls `jarvis.daemon.switch_chat_conversation` and is refused with `busy` while a query runs:

1. The outgoing turns the diary has not seen get a diary pass of their own on a worker thread, so nothing is lost, nothing is summarised twice and the switch does not wait for the summary.
2. The memory is replaced by the chosen chat's last 20 turns, redacted again and marked as already summarised. Caches and tool carryover are cleared.
3. `active_chat_id` is stored and the hub's mirror cursor moves to the new memory.

At start-up an empty memory gets the open chat's recent turns back the same way, so the model keeps the thread across a restart.

The hub mirrors every turn the memory gains into the open chat (`DialogueMemory.messages_after`), exactly as Phone Access does. Typed replies, voice turns and the outcome of a confirmed action therefore all land the same way, once. A turn mirrored while a typed request is in flight is `typed`; a lone assistant turn after an assistant turn is `confirmed`; otherwise a turn is `voice` unless it follows a user turn, whose source it shares. Local notices (busy, no reply, stopped, not ready) are shown but never stored, and are dropped when another chat opens.

## Models

The composer has three pickers, left to right:

1. **Mode**: a switch between Local and the cloud reply modes allowed in Settings (`bridge.modes.enabled_modes`), ChatGPT (Codex) and Claude, shown as "Codex" and "Claude"; the cloud ones say in their tooltip that requests go to the cloud. It is hidden while no cloud mode is allowed.
2. **Model**: the models of the active mode only.
   - Local: every offered chat model (`OFFERED_CHAT_MODELS`), disabled with "Not installed" when the runtime does not report it (`list_models`, cached for 30 seconds), plus the current one when it is not on the list. On an OpenAI-compatible provider only the current model is listed and it cannot be switched.
   - Claude or Codex: every model its bridge reports for this account (`bridge/bridge.spec.md`, Cloud models), with the runtime's description as secondary text. A Claude model is named with its version ("Sonnet 5.5", "Haiku 4.5"), taken from the runtime's own description, because Claude Code's display name is only the alias. Until the bridge has reported its models the picker reads "Checking the Claude models". The models of a mode that is not active are never listed, because that would mean starting its bridge; choosing the mode starts it and the list appears when its check finishes.
3. **Effort**: see below.

**Stop.** The stop button in the message box calls `POST /api/stop` (`cancel_active_chat_query`). In Local mode that ends the work itself: the engine stops at its next step and the model call in flight is dropped, so the model stops generating and the chat accepts the next message at once (`reply/reply.spec.md`, Stopping a reply). A tool that is already running finishes first. In Claude or Codex mode the bridge request is cancelled (`bridge/bridge.spec.md`).

Choosing a reply mode calls `bridge.modes.switch` (a mode that is not allowed is refused, a request in flight is cancelled, the choice is persisted, as the tray does). Choosing a local model while in a cloud mode returns to Local first.

**Effort.** A separate menu beside the picker, shown only while a cloud model that reports effort levels is selected, lists exactly the levels that model offers, which differ per model and per runtime (for example Codex's `ultra`, or none at all for Claude's Haiku, which then has no menu), named "Extra high" for `xhigh` and so on, with a runtime's description as secondary text. Choosing a model keeps the current effort when the new model offers it, otherwise uses the model's default. Both are changed with `daemon.set_cloud_model` for the whole assistant, voice included, and saved (`claude_model`, `claude_effort`, `codex_model`, `codex_reasoning_effort`); the other cloud mode's choice is untouched. It is refused while a query runs.

Choosing a local model (`jarvis.daemon.set_local_chat_model`) changes it for the whole assistant, voice included, and saves `ollama_chat_model`, because one model is loaded and shared. It is refused while a query runs, on a model Jarvis does not offer, on one that is not installed and on a provider other than Ollama. The daemon's settings, the voice listener's and the reply-mode registry's are replaced, the new model is warmed and the old one released unless the fast tier, the tool model or embeddings share it.

Each chat **remembers** the mode and model that last answered it (`last_mode`, `last_model`). Opening a chat never switches anything: when they differ from the current ones the page shows "This chat last used X" with a **Switch back** button, so opening an old Claude chat can never send voice or typing to the cloud on its own.

## HTTP API

All JSON. Errors are `{"error": "<code>"}`.

| Method and path | Body | Result |
|-----------------|------|--------|
| `GET /api/state` | | `ready`, `state`, `busy`, `active_chat_id`, `mode`, `model`, `cloud` |
| `GET /api/models` | | `mode`, `current`, `switchable`, `models`, and `cloud` (the active cloud mode's model, effort, `ready` and `models` with their `efforts`, or `null` in local mode) |
| `GET /api/library` | | `projects`, `chats` (newest first) and `active_chat_id` |
| `POST /api/projects` | `{"name"}` | `201` the project; `400` for an empty name |
| `PATCH /api/projects/<id>` / `DELETE` | `{"name"}` | Rename / delete (chats are unfiled); `404` unknown |
| `POST /api/chats` | `{"project_id"?}` | `201 {"chat"}` and opens it; `404` unknown project, `409` busy, `503` not ready |
| `GET /api/chats/<id>` | | `{"chat", "messages"}`; `404` unknown |
| `PATCH /api/chats/<id>` | `{"title"?, "project_id"?}` | Rename, move (`null` unfiles); `404` unknown chat or project |
| `DELETE /api/chats/<id>` | | Delete; `409` when it is the open chat and Jarvis is busy |
| `POST /api/chats/<id>/open` | | `{"chat"}`; `404` unknown, `409` busy |
| `POST /api/chat` | `{"text"}` | Send to the open chat (starting one when none is open): `202 {"query_id"}`, `409` busy, `503` not ready, `400` empty or over 4000 characters |
| `POST /api/stop` | | `cancel_active_chat_query` |
| `POST /api/model` | `{"kind": "mode"\|"local"\|"cloud", "value", "effort"?}` | Switch a reply mode, a local model, or the active cloud mode's model (and optionally its effort); `409` with the reason (`not_enabled`, `busy`, `not_installed`, `not_ready`, `not_offered`, `effort_unsupported`, ...), `400` malformed |
| `POST /api/clear` | `{"confirm": true}` | Delete every project, chat and message and empty the conversation |
| `GET /api/poll?rev=<rev>&after=<id>` | | Long poll (25 s): the changed snapshot |

The poll snapshot carries `rev`, `library_rev` (bumped when a title, project or chat list changes), `ready`, `state` (`thinking` while a typed request runs), `busy`, `busy_query`, `active_chat_id`, `chat`, the open chat's `messages` after `after` (`id`, `role`, `text`, `ts`, `source`; never the private flag), `notices`, `mode`, `model` and `cloud`.

## The page

The page follows the server. An assistant-ui `ExternalStoreRuntime` holds the open chat's messages, which the server owns; the thread list comes from the library. A long poll waits on `rev`, reloads the open chat when `active_chat_id` changes and the library when `library_rev` changes, and backs off (1 s doubling to 15 s) while Jarvis cannot be reached. Sending shows the message and a thinking placeholder at once; the stored reply replaces them. A busy, stopped or failed request leaves a local line in the thread. Spoken turns use assistant-ui's voice message style. Replies render Markdown as text only.

**Source of the chat components.** The thread, composer, thread list, Markdown text and model selector are assistant-ui's own source, copied into `webchat-ui/src/components` with the `assistant-ui` CLI **0.0.121** (`add thread thread-list markdown-text model-selector`) against `@assistant-ui/react` **0.15.25** and `@assistant-ui/react-markdown` **0.14.19**, then edited: trimmed of attachments, tool and reasoning groups, feedback, editing and branching, restyled with the HUD palette, with a notice line, a slot for the picker and a "Move to project" menu item. The pristine copy is the git commit `a1b2540`; `git diff a1b2540 -- webchat-ui/src/components` is the local change set to compare with upstream when pulling improvements. Written for Jarvis: the API client, the runtime wiring, the projects sidebar and the model picker. `webchat-ui/README.md` lists every file's origin and the update steps.

**Projects.** A sidebar groups the chats under projects (collapsible, remembered in the browser's storage when it allows) with the unfiled chats below. Projects are created, renamed (inline) and deleted (a second tap confirms; chats stay) from the sidebar; a chat moves with its menu ("Move to ..."); each project has a button for a new chat inside it. A search box filters chats by title.

**Look.** The palettes are generated from `HUD_COLORS` and `HUD_COLORS_LIGHT` in `themes.py` (`generated/theme.css`); the page maps assistant-ui's colour tokens onto them, so the chat and the Qt windows share one source of colour. Owner messages use the orb's cyan-to-blue gradient. Icons are bundled (`lucide-react`), fonts are the system's, and nothing animates under reduced motion.

**Light and dark.** A switch in the header changes between the dark HUD look (the default) and a light variant of the same palette. The choice is remembered in the browser's storage when it allows, and applied before the first paint. Text, accents and status colours meet WCAG AA (4.5:1) on every surface in both palettes, which `tests/test_webchat_theme.py` checks.

**No outside calls.** The page calls only its own origin. `tests/test_webchat_bundle.py` fails if the build or the copied source names an outside address, a hosted service (assistant-ui Cloud included) or any analytics, beacon, socket or event-stream use.

Desktop confirmations still appear as the desktop dialog, as in the Qt chat, and exist only when the daemon is bundled in the desktop app. The reply reads "Please confirm on your desktop" and the outcome arrives as a normal assistant message.

## Desktop window

`WebChatWindow` is a themed `QMainWindow` (`desktop_app.spec.md`, Theme System) with a `QWebEngineView` on `http://127.0.0.1:<web_chat_port>/`. While the daemon is starting, stopping, stopped or crashed it shows a status page instead of the chat, and when the daemon is up but the page is not served yet it says so and retries every two seconds until it loads. It never leaves the chat: any other address, and any link that asks for a new window, opens in the default browser. When Qt WebEngine is unavailable the tray opens the page in the default browser. The tray entry reads the setting once at start-up, so a change needs a restart, as the server does.

## Privacy and logging

- Stored text is already redacted. No message text, title, project name or path appears in logs or issue reports; `webchat` debug logs record start, stop, bind failures, refused requests and counts.
- The chat history and the diary are separate stores; deleting a chat does not edit the diary.

## Not in version 1

- **Rewind** (re-asking a message from the past), which the Qt chat has, until the owner decides the web chat should replace it.
- Streaming: the engine returns whole replies.
- File or image attachments, voice input in the page, sharing and search inside message text.
- Phone access: the phone app keeps its own conversation view. A request from a paired phone joins the open chat like any other turn and shows as a spoken one, because the dialogue memory does not record where a turn came from.

## Known limits

- The server has no sign-in. Anything else running as the same user on this PC can read and send chats, as with the Memory Viewer; the Host, Origin and fetch-site checks stop web pages, not local programs.
- A chat switch during the periodic diary pass can summarise the turns of that pass a second time, because the pass saves its progress only when its summary is done.
- A confirmed action's outcome that lands in the instant a chat is switched can miss the new chat's history.

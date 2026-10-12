# Web Chat Specification

A second chat interface for Jarvis, built on assistant-ui (React, MIT licence) and shown inside the desktop app. It adds a model selector and projects (named groups of chats). The Qt chat window (`src/desktop_app/chat_window.spec.md`) stays; the tray's `Chat` entry opens the web chat while `web_chat_enabled` is on and the Qt chat otherwise.

## Principles

- **Off by default.** `web_chat_enabled` is `false`. When it is off, no socket is opened and nothing in this package runs.
- **Offline and local.** The React app is built once into static files shipped with Jarvis. Running Jarvis needs no Node, no CDN and no hosted service (no assistant-ui Cloud). The page loads nothing from the network, and there is no telemetry or analytics.
- **Loopback only.** The server listens on `127.0.0.1` and applies the Memory Viewer's Host and Origin checks to every request. It adds no network exposure.
- **Same text path.** Every message runs through `jarvis.daemon.submit_text_query`, so redaction, the fast path, the one-query-at-a-time lock, central safety and desktop confirmations apply unchanged. The web chat never runs tools itself.
- **Privacy.** Stored messages are the redacted text the dialogue memory holds, never what was typed. The owner can delete a chat or everything, each only after a confirmation. Nothing is sent anywhere.
- **Voice shares the open chat.** See **Chats and the shared memory**.

## Where it runs

The server runs in the daemon process, like Phone Access (`remote/remote.spec.md`), because the dialogue memory, the query lock, the reply mode and the local model all live there. It works the same when the daemon is bundled in the desktop app and when it is a subprocess. The desktop app only embeds the page in a `QWebEngineView`.

| Module | Responsibility |
|--------|----------------|
| `jarvis/memory/chat_store.py` | `ChatStore`: projects, chats and messages in the Jarvis database file. Own connection and schema, following `ActivityStore` (`memory/db.spec.md`) |
| `jarvis/utils/local_guard.py` | `refusal(host, origin)`: the Host and Origin checks shared with the Memory Viewer |
| `jarvis/tools/step_log.py` | `StepLog`: the in-memory record of tool steps that `run_tool_with_retries` fills and the hub reads (see **Tool steps**) |
| `jarvis/webchat/backend.py` | `DaemonBackend`: the only module that touches `jarvis.daemon`, `bridge.modes` and the LLM backend. Tests replace it with a fake |
| `jarvis/webchat/hub.py` | `ChatHub`: the open chat, mirroring of the dialogue memory into it, query tracking, notices, change notification for long polls, the uploads waiting for a request |
| `jarvis/webchat/suggestions.py` | The start screen's cards: `build_cards`, the generic examples, the card wording read from the fast path's locale file. Pure |
| `jarvis/webchat/mic.py` | `MicSessions`: the page's one microphone recording, its session id, heartbeat watch and release. Records through the dictation engine |
| `jarvis/webchat/uploads.py` | Attachments: telling an upload apart by its content, preparing it for a model, `UploadStore` (in memory only) |
| `jarvis/reply/attachments.py` | `RequestAttachment`: what a request carries, the fence around file text, the note a model reads, the kind-and-size summary that is stored |
| `jarvis/webchat/server.py` | Threaded HTTP server (standard library), routing, guards, static files |
| `jarvis/webchat/identity.py` | The server's per-run identity token and the HMAC proof the desktop window checks |
| `jarvis/webchat/runtime.py` | `start(cfg)` / `stop()`: called by the daemon; starts nothing when disabled |
| `jarvis/webchat/static/` | The built React app. Committed, so a source checkout needs no Node |
| `webchat-ui/` (repository root) | The JavaScript project: sources, `package.json`, `package-lock.json`, `README.md` with the build steps. Python never imports it and Python tests never need Node |
| `desktop_app/web_chat_theme.py` | Writes `webchat-ui/src/generated/theme.css` from `HUD_COLORS`; a test fails when the committed file is stale |
| `desktop_app/web_chat_window.py` | `WebChatWindow`: the window holding the `QWebEngineView`, status page while the daemon is not running |

`jarvis` knows nothing about `desktop_app`.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `web_chat_enabled` | `false` | Serve the web chat and let the tray open it. Takes effect at the next daemon start (the tray's "Restart Jarvis now" after saving Settings) |
| `web_chat_port` | `8766` | Loopback TCP port, 1024 to 65535 |

A port that cannot be bound prints one warning at start-up and the web chat stays off; the Qt chat and the rest of Jarvis are unaffected.

## Server guards

Applied to every request before routing:

1. `Host` must be a loopback name (`localhost`, `127.0.0.1`, `::1`), or the request gets `403` (stops DNS rebinding).
2. A request carrying `Origin` is served only when that origin is the server itself, and a request marked `Sec-Fetch-Site: cross-site` is refused (stops cross-site requests).
3. A request with a body must be `application/json` (`415` otherwise) and at most 64 KiB (`413`). The one exception is the upload endpoint, which takes `application/octet-stream` up to 10 MiB (**Attachments**) and is checked by exactly the same Host, Origin and fetch-site rules first.

On Windows the socket is bound with `SO_EXCLUSIVEADDRUSE`, so no other program can bind the same port while Jarvis serves it.

Responses carry `Content-Security-Policy: default-src 'self'` (images may also be `data:` and styles inline), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Permissions-Policy: microphone=(), camera=(), geolocation=()` (see **Voice input**), and `Cache-Control: no-store` for the API. Static files are served only from the built `index.html` and the `assets/` folder; nothing else on disk is reachable.

## Data

Tables in the Jarvis database file (`db_path`), created with `CREATE TABLE IF NOT EXISTS` by `ChatStore`, with deleted rows overwritten (`secure_delete`):

- `chat_projects(id, name, position, created_at)`
- `chats(id, project_id NULL, title, created_at, updated_at, last_mode, last_model, pinned)` where `pinned` is `0` or `1`; a database written before pinning gains the column when `ChatStore` opens it (`ALTER TABLE ... ADD COLUMN`, default `0`), keeping every chat
- `chat_messages(id, chat_id, role, content, ts, source, private, steps, attachments)` where `source` is `typed`, `voice` or `confirmed`, `private` marks a turn that quotes a private source (the activity log, the user's documents, a calendar or email), `steps` holds the reply's tool steps (see **Tool steps**) and `attachments` is a JSON list of `{"kind", "size"}` for what the message carried (kind `image`, `text` or `screenshot`, size in bytes), never a name, a path or any content. A database from before this column gains it when `ChatStore` opens it
- `chat_state(key, value)`: holds `active_chat_id`

Pinning a chat is not activity: it never changes `updated_at`, so the chat keeps its place in the age groups. A chat with no project is unfiled. Deleting a project keeps its chats and unfiles them. Deleting a chat deletes its messages. A chat's title is the first 60 characters of its first message until the owner renames it (no model call, so `docs/llm_contexts.md` is unaffected). A turn marked `private` keeps its flag, so reopening the chat keeps it out of the diary and out of any context sent off the PC. Identifiers are random hex strings.

## Chats and the shared memory

Jarvis has one dialogue memory, so exactly one chat is *open*: the one whose turns the memory holds. **Voice goes into the open chat**, so a spoken request and a typed follow-up stay one conversation. The page says so ("Voice joins this chat"). With the window closed, voice still joins the chat that was open last. A first run opens a chat called "Voice and quick questions".

Opening another chat, or creating one, calls `jarvis.daemon.switch_chat_conversation` and is refused with `busy` while a query runs:

1. The outgoing turns the diary has not seen get a diary pass of their own on a worker thread, so nothing is lost, nothing is summarised twice and the switch does not wait for the summary.
2. The memory is replaced by the chosen chat's last 20 turns, redacted again and marked as already summarised. Caches and tool carryover are cleared.
3. `active_chat_id` is stored and the hub mirrors from the cursor the swap returns, so a turn that lands right after the swap (a confirmed action's outcome, say) still reaches the new chat.

At start-up an empty memory gets the open chat's recent turns back the same way, so the model keeps the thread across a restart.

**The model keeps the thread.** While the web chat runs, the open chat's latest 20 turns stay in the reply context however old they are (`DialogueMemory.keep_last_turns`), on the local path and in what the Claude and Codex modes are sent (still within their own `*_recent_dialogue_messages` cap). Opening yesterday's chat, reading it for ten minutes and asking a follow-up therefore reaches the model with the thread the page shows. Turns inside the dialogue window (`dialogue_memory_timeout`) are always in context too, and the kept turns stay in memory after a diary pass. Only the text thread is kept: a private turn (one that quotes the activity log, the user's documents, a calendar or email) follows the dialogue window, so one private reply does not make the rest of the chat private, and after an idle window a request still starts a new conversation (no tool carryover, fresh caches, `reply/reply.spec.md`). Voice shares the open chat, so a spoken request sees the same thread. When the web chat is off, or stops, the dialogue window alone applies.

The hub mirrors every turn the memory gains into the open chat (`DialogueMemory.messages_after`), exactly as Phone Access does. Typed replies, voice turns and the outcome of a confirmed action therefore all land the same way, once. A turn mirrored while a typed request is in flight is `typed`; a lone assistant turn after an assistant turn is `confirmed`; otherwise a turn is `voice` unless it follows a user turn, whose source it shares. Local notices (busy, no reply, stopped, not ready) are shown but never stored. A notice belongs to the moment it happened: it is dropped when another chat opens and once a turn newer than it is stored in the open chat, so a stale one never stays pinned below later turns.

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
| `GET /api/suggestions` | | `cards`: at most 6 of `{kind, text, action}` for the start screen (see Start screen); never fails, a backend that cannot say gives the generic examples |
| `GET /api/library` | | `projects`, `chats` (newest first, each with `pinned`) and `active_chat_id` |
| `GET /api/search?q=<text>` | | `{"results": [{"chat_id", "message_id", "snippet", "ts"}]}`: chats with a message containing the text (see **Search**); `400` for a blank query or one over 200 characters |
| `POST /api/projects` | `{"name"}` | `201` the project; `400` for an empty name |
| `PATCH /api/projects/<id>` / `DELETE` | `{"name"}` | Rename / delete (chats are unfiled); `404` unknown |
| `POST /api/chats` | `{"project_id"?}` | `201 {"chat"}` and opens it; `404` unknown project, `409` busy, `503` not ready |
| `GET /api/chats/<id>` | | `{"chat", "messages"}`, each message with `id`, `role`, `text`, `ts`, `source` and `steps`; `404` unknown |
| `PATCH /api/chats/<id>` | `{"title"?, "project_id"?, "pinned"?}` | Rename, move (`null` unfiles), pin or unpin (`pinned` must be a boolean, `400` otherwise); `404` unknown chat or project |
| `DELETE /api/chats/<id>` | | Delete; `409` when it is the open chat and Jarvis is busy |
| `POST /api/chats/<id>/open` | | `{"chat"}`; `404` unknown, `409` busy |
| `POST /api/chat` | `{"text", "attachments"?}` | Send to the open chat (starting one when none is open): `202 {"query_id"}`, `409` busy, `503` not ready, `400` empty or over 4000 characters or `bad_attachments`; attachment refusals are listed under **Attachments** |
| `POST /api/attachments` | raw bytes, `application/octet-stream` | Check and hold an upload for the next message: `201 {"id", "kind", "size"}` (never the content) or a refusal |
| `POST /api/attachments/screenshot` | `{}` | Capture the screen through the screenshot tool and hold it the same way |
| `DELETE /api/attachments/<id>` | | Drop a waiting upload (always `200`) |
| `GET /api/attachments/support` | | What attaching can do now: `images` and `screenshot` (each `{"ok", "reason"}`) and the `limits` |
| `POST /api/stop` | | `cancel_active_chat_query` |
| `POST /api/model` | `{"kind": "mode"\|"local"\|"cloud", "value", "effort"?}` | Switch a reply mode, a local model, or the active cloud mode's model (and optionally its effort); `409` with the reason (`not_enabled`, `busy`, `not_installed`, `not_ready`, `not_offered`, `effort_unsupported`, ...), `400` malformed |
| `POST /api/clear` | `{"confirm": true}` | Delete every project, chat and message and empty the conversation |
| `GET /api/mic` | | `available`, `reason` (`null`, or one of the refusals under **Voice input**) and `max_seconds` |
| `POST /api/mic/start` | | `{"session", "max_seconds"}`; `409 busy`, or `503` with the reason (`disabled`, `unavailable`, `not_ready`, `microphone`) |
| `POST /api/mic/heartbeat` | `{"session"}` | `{"state"}`: `recording`, or `full` once the length cap is reached; `404 unknown` when the recording is gone |
| `POST /api/mic/stop` | `{"session"}` | `{"text"}` after the audio is transcribed (empty when nothing was heard); `404 unknown` |
| `POST /api/mic/cancel` | `{"session"}` | `{"ok"}`; releases the microphone, safe to repeat |
| `GET /api/poll?rev=<rev>&after=<id>` | | Long poll (25 s): the changed snapshot |
| `GET /api/identity?nonce=<hex>` | | `{"proof"}`: HMAC-SHA256 of the nonce (32 to 128 lowercase hex characters, `400` otherwise) keyed with the server's identity token |

A `session` is the 32-character lowercase hex id `start` returned (`400 bad_session` otherwise).

The poll snapshot carries `rev`, `library_rev` (bumped when a title, project or chat list changes), `ready`, `state` (`thinking` while a typed request runs), `busy`, `busy_query`, `active_chat_id`, `chat`, the open chat's `messages` after `after` (`id`, `role`, `text`, `ts`, `source`, `steps`, `attachments`; never the private flag), `notices` (the ones not yet overtaken by a newer stored turn, each with its `ts`), `mode`, `model` and `cloud`.

## The page

The page follows the server. An assistant-ui `ExternalStoreRuntime` holds the open chat's messages, which the server owns; the thread list comes from the library. A long poll waits on `rev`, reloads the open chat when `active_chat_id` changes and the library when `library_rev` changes, and backs off (1 s doubling to 15 s) while Jarvis cannot be reached. Sending shows the message and a thinking placeholder at once; the stored reply replaces them. A busy, stopped or failed request leaves a local line in the thread, placed by its time among the stored messages. Spoken turns use assistant-ui's voice message style. Replies render Markdown as text only.

**Staying in step.** The owner acts while the poll is in flight, so the page never trusts a snapshot over something it has just done (`webchat-ui/src/sync.ts`, no React, covered by `sync.test.ts`):

- Only the latest chat load applies, so clicking chat A and then quickly B cannot leave A on screen while the server has B open (and a typed message goes to the chat shown). While the owner's own switch is in flight, and for polls sent before it finished, the snapshot's open chat is ignored and a fresh poll follows; a switch the owner did not make (voice, tray) is followed as before.
- The library revision is recorded only after the library was fetched, so a failed fetch is retried and the sidebar cannot stay stale.
- The thinking placeholder is cleared on "not busy" only from a poll made after the send was confirmed, or when a reply newer than the message is stored, so a snapshot taken before the server had the request cannot make the owner's message vanish. A poll that cannot be trusted is replaced by a fresh one.

**Source of the chat components.** The thread, composer, thread list, Markdown text and model selector are assistant-ui's own source, copied into `webchat-ui/src/components` with the `assistant-ui` CLI **0.0.121** (`add thread thread-list markdown-text model-selector`) against `@assistant-ui/react` **0.15.25** and `@assistant-ui/react-markdown` **0.14.19**, then edited: trimmed of tool and reasoning groups (a tool call is drawn as a single `ToolStepRow`; the attachment chips are assistant-ui's, trimmed and restyled, and the Screenshot button is ours), feedback, editing and branching, restyled with the HUD palette, with a notice line, a slot for the picker and a "Move to project" menu item. The pristine copy is the git commit `a1b2540`; `git diff a1b2540 -- webchat-ui/src/components` is the local change set to compare with upstream when pulling improvements. Written for Jarvis: the API client, the sync logic, the tool step row and its wording (`steps.ts`, `components/ToolSteps.tsx`), the runtime wiring, the projects sidebar, the confirmation dialog and the model picker. `webchat-ui/README.md` lists every file's origin and the update steps.

**Projects.** A sidebar groups the chats under projects (collapsible, remembered in the browser's storage when it allows) with the unfiled chats below. Projects are created, renamed (inline) and deleted (a second tap confirms; chats stay) from the sidebar; deleting a chat from its menu opens a dialog that names the chat and says its messages are removed for good, and "Delete everything" at the foot of the sidebar opens a dialog that says how many chats and projects go, that the diary is kept, and enables its button only once the owner has typed the confirmation word (`POST /api/clear`); Esc, Cancel and the close button leave without deleting; a chat moves with its menu ("Move to ..."); each project has a button for a new chat inside it. A search box filters chats by title.

**Narrow windows.** Below 768 px the chat list starts closed and, when opened, is an opaque overlay on top of the chat with a dimmed backdrop; a click on the backdrop, Esc or choosing a chat closes it. At 768 px and wider it is a column beside the chat and starts open. The header keeps only the microphone icon of "Voice joins this chat" below 640 px, so the title never overlaps the list.

**Chat list order.** Pinned chats come first, in a **Pinned** group at the top of the sidebar, listed there and nowhere else. The other chats, inside each project and among the unfiled ones, sit in **Today**, **Yesterday**, **Previous 7 days**, **Previous 30 days** and **Older** groups by `updated_at`, newest first, by the browser's local calendar day (so a chat from 23:50 is "Yesterday" at 00:10). Empty groups are not shown. Pin and Unpin are in each chat's menu (`PATCH /api/chats/<id>`); the project's count includes its pinned chats.

**Timestamps.** Every stored message shows its time, small and subdued, under the message (the browser's locale and 12 or 24 hour convention), with the full date and time as the hover title. A line with the day (the browser's own word for today and yesterday, otherwise the date, with the year only when it is not the current one) separates messages sent on different days, and breaks a run of voice messages. Local notices carry no time.

**Search.** The sidebar search box matches chat titles immediately (from the library) and message text through `GET /api/search` once typing has paused (250 ms, at least two characters). Results are chats, each with a short snippet around the newest matching message and the match marked; a title match with no message match shows the title alone. Opening a result opens the chat and scrolls to that message, marking it for a moment. Server side, `ChatStore.search_messages` compares the text as a plain, case-insensitive substring in any script (the query is a bound parameter, so `%`, `_` and quotes mean themselves), returns one hit per chat, most recently active chats first, at most 30, and logs only the number of chats found, never the text searched. The search sees only what the store holds, which is already redacted.

**Keyboard shortcuts.** `shortcuts.ts` decides, the page performs; no plain letter is ever taken, so typing is never captured. They work with focus in the message box too.

| Keys | Action |
|------|--------|
| Ctrl+K (Cmd+K) | Open the chat list if hidden and focus the search box |
| Ctrl+N (Cmd+N) | New chat |
| Esc | Stop a running reply. Not taken while a dialog, menu or the narrow chat list overlay is open, or while focus is in the search box (where it clears the search, then leaves it) or another text field. The message box stops the reply itself and marks the key as handled |
| ? | Show the list of shortcuts, only when focus is not in a text field. The keyboard button in the header opens it too |

Browsers keep some combinations for themselves: in an ordinary browser tab Ctrl+N opens a new window before the page sees it. In the desktop app's window (`QWebEngineView`) the page receives it.

**Look.** The palettes are generated from `HUD_COLORS` and `HUD_COLORS_LIGHT` in `themes.py` (`generated/theme.css`); the page maps assistant-ui's colour tokens onto them, so the chat and the Qt windows share one source of colour. Owner messages use the orb's cyan-to-blue gradient. Icons are bundled (`lucide-react`), fonts are the system's, and nothing animates under reduced motion.

**Light and dark.** A switch in the header changes between the dark HUD look (the default) and a light variant of the same palette. The choice is remembered in the browser's storage when it allows, and applied before the first paint. Text, accents and status colours meet WCAG AA (4.5:1) on every surface in both palettes, which `tests/test_webchat_theme.py` checks.

**No outside calls.** The page calls only its own origin. `tests/test_webchat_bundle.py` fails if the build or the copied source names an outside address, a hosted service (assistant-ui Cloud included) or any analytics, beacon, socket or event-stream use.

Desktop confirmations still appear as the desktop dialog, as in the Qt chat, whether the daemon is bundled in the desktop app or runs as its subprocess (`desktop_app.spec.md`, Action Confirmation Dialog). A daemon the desktop app did not start has no dialog and refuses those actions. The reply reads "Please confirm on your desktop" and the outcome arrives as a normal assistant message.

## Tool steps

Under a reply the page shows the tool calls Jarvis made for it as compact rows ("Opened Word", "Set the volume to 30%", "Searched the web"), collapsed by default; a row opens to how the call ended ("Done", "Didn't work", "Not allowed", "Waiting for your confirmation") and, when the tool says so, what it found ("3 results"). They appear on every route, because every executed call goes through one place.

**One place sees every call.** `run_tool_with_retries` (`tools/registry.py`), the same function the journal of `routines/routines.spec.md` is fed from, leaves a step in `tools/step_log.py` (`StepLog`, in memory only, bounded) for each call that went through central safety: the fast path, the local loop and planner, tool-model mode, the Claude and Codex bridges, approved confirmations and each step of a routine. Its status is `done`, `failed` (the tool returned an error), `declined` (central safety denied it, or the desktop dialog it needs is not available) or `waiting` (it asked for a yes; once approved it runs through the same function and leaves its own `done`). An unknown tool, `routineControl`, `stop`, `toolSearchTool` and `refreshMCPTools` leave nothing.

**Wording.** A step holds the tool name, the status, a label and an optional summary, never the arguments, the result or the error text of the call.

- The default label is the tool's identifier read as words (`systemVolume` is "System volume"; an MCP tool is named by its own part), so no phrase list or language pattern is involved.
- A tool may word its own step with `Tool.step_note(args, result) -> StepNote(label, summary)`. It is asked only for a call that succeeded; every other status keeps the default label. The built-in notes name what was done in the owner's terms (an app's friendly name, a volume percentage, a result count) and nothing a tool spec keeps out of results: no paths, addresses, window titles, track names, queries or window handles.
- The step log is the last line of defence: a label or summary of more than one line is cut to one (80 and 160 characters), and one that contains a path, an address or an e-mail address is dropped (the label becomes the default one). A note that raises is ignored.

**Belonging to a reply.** Steps are numbered. The hub keeps a cursor, and the assistant turn it mirrors takes every step after the cursor that was recorded no later than the turn, so a typed request, a spoken one and the outcome of a confirmed action each get exactly the steps made for them (the waiting step stays with the reply that asked, the done step arrives with the outcome). The cursor moves when a chat is opened or created and when a request ends without a reply (stopped, failed, not ready), so those steps never turn up under the next reply; a request that is merely busy leaves it alone. Local notices never carry steps.

**Private turns.** A reply that quotes the activity log keeps the tool and status of each step but takes the default label and no summary, so nothing the tool found is stored.

**Storage.** Each message has a `steps` column (JSON, empty when there are none), added to databases made before it with `ALTER TABLE` when the store opens. Steps are stored only on assistant messages, survive reloads, and are deleted with their chat (`secure_delete`).

**Page.** The server sends `steps` (a list of `{tool, status, label, summary}`, empty for the owner's messages) with every message in the poll and in `GET /api/chats/<id>`. The page turns them into assistant-ui tool-call parts ahead of the reply's text (`conversion.ts`), each already finished so the thread never shows one running, and draws each with `ToolStepRow` (`components/ToolSteps.tsx`), in the normal and the spoken message style. The wording of how a step ended lives in `steps.ts`; the label and summary are the server's.

## Start screen

An empty chat shows a greeting for the time of day (the browser's local hour: morning, afternoon, evening) above a grid of up to six cards, in one column on a narrow window. The cards come from `GET /api/suggestions`, built by `suggestions.build_cards` with no model call, in this order:

1. **Routines** (`routines`, the live set in `routines/store.py`), at most three, by name.
2. **Workspaces** (`windows_workspaces`), by name, up to four owner cards in all; offered only while `windows_tools_enabled` is on, as the fast path is.
3. **Generic examples** (what is using the most RAM, set the volume, search the web, ...), filling the remaining places up to six, so there is always room for them.

**Behaviour.** A routine or workspace card **sends** its text at once, because it is a complete command. An example card **fills the composer** and focuses it, because it may need a detail and a click should never change a setting unseen. A card never sends while Jarvis is busy or unreachable. Both go through the normal text path, so redaction, central safety and any confirmation apply to a card exactly as to typing.

**Wording.** An owner card's text is a template from the `suggestions` block of the fast path's locale file (`fastpath/phrases/<language>.json`, `{name}` filled with the name as the owner wrote it, so the fast path runs it as a whole-utterance command and a test checks that it does). The language is the first of `fast_commands_locales` that has the block, else English. The generic examples and the greeting are interface text in the page and `suggestions.py`; nothing in the code matches language patterns.

**What reaches the page.** Names only. Steps, arguments, paths, URLs, aliases and labels never leave `backend.suggestion_sources`, a name over 60 characters, empty or not printable is dropped, and names are not logged (`webchat` debug logs record card counts).

## Voice input

A microphone button beside Send in the message box. One click starts recording, another click (or Esc) stops it, and the words are added after what is already typed, for the owner to read, edit and send. They are never sent on their own. While recording the button turns into a stop button and shows the elapsed time against the cap (`1:05 / 2:00`); while the audio is being written out it shows a spinner.

**Jarvis records, not the page.** The recording and the speech recognition run in the daemon, with the dictation engine and the Whisper model Jarvis already loaded (`dictation/dictation.spec.md`, Capture for a caller). The page never calls `getUserMedia` or a browser speech API and never asks for a permission, and every response carries `Permissions-Policy: microphone=(), camera=(), geolocation=()`, so neither a browser nor the desktop window's embedded Chromium has anything to ask. `tests/test_webchat_mic_page.py` fails if the page source names a browser audio or speech API.

**As hold-to-dictate does.** While recording, the voice listener is paused and the face shows dictation, the start and stop beeps play, the orb follows the microphone's loudness, and the custom dictionary and filler removal settings apply. Nothing is pasted into another program and nothing is added to the dictation history.

**One recording at a time, owned by its page.** `start` returns a random session id that the page must present to heartbeat, stop and cancel; another page, tab or program cannot end or read the recording. The page sends a heartbeat every 2 seconds. With none for 6 seconds (the page closed, crashed or lost the connection) the daemon cancels the recording and releases the microphone without transcribing anything; a page that is closing cancels at once. The length is capped at 120 seconds: the engine then releases the microphone and keeps the audio, the next heartbeat reads `full`, and the page stops the recording, adds the words and says it reached the longest recording. Stopping the web chat or the daemon releases the microphone too.

**Refusals.** The page says why in plain words and starts nothing:

| Reason | When |
|--------|------|
| `busy` | A request is running, Jarvis is listening for one or speaking, or another dictation or recording is open |
| `disabled` | `dictation_enabled` is off in Settings |
| `unavailable` | There is no dictation engine (no audio backend or hotkey library) |
| `not_ready` | The speech model is still loading |
| `microphone` | The input device could not be opened |

`disabled` and `unavailable` are shown before the first click, as a disabled button with the reason; the others are said when the button is used.

**Privacy.** The transcript is returned in the response to the `stop` request of the page that holds the session and goes nowhere else: it is not stored in a chat, not in any poll snapshot, not in the dictation history and not in a log. Debug logs record that a recording started, stopped (with its length in seconds), was cancelled or was refused (with the reason), never text. The audio stays in memory until it is transcribed or discarded.

## Attachments

A message can carry attachments: **drag and drop** onto the page, **paste** from the clipboard, the **paperclip** button, and the **Screenshot** button. At most 4 per message, at most 3 of them with an image.

| Kind | Accepted | What the model gets |
|------|----------|---------------------|
| `image` | PNG, JPEG, GIF (first frame) and WebP, up to 10 MiB and 50 million pixels | One JPEG scaled down (never up) to the screenshot tool's longest edge (`MAX_IMAGE_EDGE`). Re-encoding drops embedded metadata such as location, and transparent areas are flattened onto white |
| `text` | Text in UTF-8, UTF-16 (with a byte order mark) or the Windows code page (plain text, Markdown, code, CSV, JSON and the like), up to 1 MiB; PDF up to 10 MiB, its text read locally with `pypdf` (first 100 pages, 6 seconds) | The text, capped at 20,000 characters (a cut is marked) and fenced as untrusted file data |
| `screenshot` | The screenshot tool's own capture of the monitor the owner is looking at (`screenshot.spec.md`) | That tool's image and its fenced OCR text |

**Upload.** The page sends the bytes of the file to `POST /api/attachments`; no name, path or declared type is read from the request, and the answer carries only an `id`, the kind and the size. What a file is comes from its content: image signatures, then `%PDF-`, then text that decodes cleanly with almost no control characters. A file renamed to look like something else is judged by what it is, and a binary of any kind is refused. Refusals carry a reason the page words: `empty` (400), `too_large` (413, also for a body over the 10 MiB upload limit), `unsupported` (415, not an image, PDF or text), `unreadable` (422, damaged), `no_text` (422, a PDF with no text layer), `encrypted` (422), `too_many` (409, more waiting or on one message than allowed), `unknown_attachment` (404, expired or already used). The page also checks the size before sending.

**Held in memory only.** An upload waits in the hub's `UploadStore` (at most 8, unclaimed ones forgotten after 15 minutes), is handed over once, to the request that names its id, and is forgotten when its chip is removed or the history is cleared. It is never written to disk and never logged: debug logs record only counts and sizes. A request naming an unknown id is refused whole (`404 unknown_attachment`) and takes nothing.

**What is stored.** The user message in the open chat records `attachments` as kind and size, so reopening the chat still shows that something was attached. Neither the content nor the name is kept: the dialogue memory, the diary, the graph, tool carry-over and this chat's history hold only the words the owner typed.

**What the model reads** (`reply/reply.spec.md`, Attachments). The request goes through `submit_text_query` with its attachments, and a request that carries any is never taken by a deterministic fast command, because its words are about what is attached. Attached content counts as outside content, as a tool result does, so routine changes are refused after it (`routines/routines.spec.md`).

- **Local:** a note ("The user attached ...") and the fenced text follow the request in the message the model reads, for that reply only; images are sent as that message's `images` to a model that can see them. When the chat model reports no vision, an **image** is refused with `409 no_vision` and the page says so; a **screenshot** still goes, as its OCR text alone, because the text answers the question without sight. Nothing is silently dropped.
- **Claude and Codex:** the redacted text goes in the request JSON (`attachments`), and images go as image blocks of the user message (Claude) or image inputs of the turn (Codex), exactly as a tool's image reaches them (`bridge/bridge.spec.md`). A mode whose bridge reports no image support refuses a request with an image (`409 cloud_no_images`, nothing is sent), and one still starting says so (`503 cloud_not_ready`). Attaching to a cloud mode sends it to that provider, which the chip says ("Will be sent to Claude"). No per-mode sharing switch gates it, because the owner attaches it deliberately to one message; the switches govern what Jarvis volunteers.
- **Screenshot button:** needs screen awareness on (`screen_awareness_enabled`, else `409 screen_awareness_off`) and the Windows tools; anything else is `503 screenshot_unavailable`. The capture, scaling and OCR are the tool's own (`tools/builtin/screenshot.spec.md`).

A refused send (`no_vision`, `cloud_no_images`, `cloud_not_ready`) keeps the uploads, so changing model and sending again works. The page reads `GET /api/attachments/support` (and again when the mode or model changes) and disables Send, with the reason shown, rather than losing the message.

**The page.** Chips for the waiting attachments sit above the text box: a thumbnail for an image, an icon for a file, the name the owner knows it by, the size, a spinner while it uploads, a remove button. The sent message shows chips with the kind and size only, also after a reload. Dropping a file anywhere on the thread highlights the target.

## Desktop window

`WebChatWindow` is a themed `QMainWindow` (`desktop_app.spec.md`, Theme System) with a `QWebEngineView` on `http://127.0.0.1:<web_chat_port>/`. While the daemon is starting, stopping, stopped or crashed it shows a status page instead of the chat, and when the daemon is up but the page is not served yet it says so and retries every two seconds until it loads.

**Only Jarvis's own server is shown.** Each server run makes a random identity token (`secrets.token_hex(32)`) that only the daemon and the desktop app know: the bundled app reads it from the running server (`runtime.current()`), and a daemon subprocess started by the desktop app announces it once the server is up with `__WEB_CHAT__:{"type": "ready", "data": {"port", "token"}}` on its stdout, which never reaches the log viewer. A daemon started from a console prints no token. Before every load, off the GUI thread and with a two-second limit, the window sends a fresh nonce to `/api/identity` directly (never through a proxy) and loads the page only when the answer is the right proof; the token itself never travels over HTTP. While no token is known, or nothing answers, it shows the waiting page and retries; when something else answers it shows "Another program is using the chat's port" and retries, and never shows that program's page. When the embedded browser is missing, the default browser is opened only after the same check. The token is forgotten whenever the daemon stops, so the next run's server must prove itself again. It never leaves the chat: any other address, and any link that asks for a new window, opens in the default browser. When Qt WebEngine is unavailable the tray opens the page in the default browser. The tray entry reads the setting once at start-up, so a change needs a restart, as the server does.

## Privacy and logging

- Stored text is already redacted. No message text, title, project name or path appears in logs or issue reports; `webchat` debug logs record start, stop, bind failures, refused requests and counts. A microphone transcript is never stored or logged (see **Voice input**).
- Tool steps hold only a tool name, a status and the words described under **Tool steps**; no argument, result, error text, path, address or window title is stored, served or logged, and `debug_log` records only that a step was recorded and its status.
- The chat history and the diary are separate stores; deleting a chat does not edit the diary.

## Not in version 1

- **Rewind** (re-asking a message from the past), which the Qt chat has, until the owner decides the web chat should replace it.
- Streaming: the engine returns whole replies.
- Sharing.
- Attachments that outlast their message: a file or image belongs to the one message that carried it, so a follow-up that needs it attaches it again (**Attachments**).
- Phone access: the phone app keeps its own conversation view. A request from a paired phone joins the open chat like any other turn and shows as a spoken one, because the dialogue memory does not record where a turn came from.

## Known limits

- The server has no sign-in. Anything else running as the same user on this PC can read and send chats, as with the Memory Viewer; the Host, Origin and fetch-site checks stop web pages, not local programs. The identity check protects the window from showing another program's page; it does not authenticate requests to the server.
- A tool call that finishes after its request was stopped leaves its step for the next reply, because the stop already closed the request's steps.
- A chat switch during the periodic diary pass can summarise the turns of that pass a second time, because the pass saves its progress only when its summary is done.

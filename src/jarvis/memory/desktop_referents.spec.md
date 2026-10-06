# Desktop referents

A short-lived, in-memory record of what Jarvis itself recently acted on (windows it opened, focused, placed or changed, a device, the media player, the clipboard), together with the window the user is looking at when a request arrives. It lets a follow-up that does not name its target ("close this", "move it to my other monitor", "pause it", "turn it off", "make this louder", in any language) act on the exact thing meant. The model resolves the reference from this record; Jarvis never parses pronouns or other language.

## Scope

- Module: `src/jarvis/memory/desktop_referents.py`. Pure Python, no OS imports. One process-wide store (`get_desktop_referents()`), shared by voice and text chat because both run in the daemon process.
- Window producers: the `appControl`, `windowControl`, `openWebsite` and `openPath` adapters (`src/jarvis/tools/builtin/windows/desktop_control.py`) after an action (for `openWebsite` and `openPath`, a placed or partially placed window, under the browser's or program's name), `workspaceControl` (`workspace_control.py`) for each window a workspace opened or reused, and `uiControl` (`ui_control.py`) for the application window whose controls it changed.
- Other producers: `tvControl` (`tools/builtin/tv_control.py`) and local extensions (through `api.record_device`, `extensions/extensions.spec.md`) for the device they acted on, `mediaControl` (`tools/builtin/windows/media_control.py`) for the media session it acted on or read, and `inputControl` (`tools/builtin/windows/input_control.py`) for the clipboard.
- Every route that executes those tools records through them: the fast-command path, the local agentic loop and planner direct-exec, background Codex and Claude `jarvis_execute`, and an approved confirmation.
- The window the user is looking at is not recorded: it is read once per request (see Foreground window) by `platform/windows/ui_automation.py` and handed to the presentation functions with the record.
- Consumers: the local reply engine (tool router hint, planner dialogue context, chat system message, allow-list carry-over) and the background Codex and Claude requests (the request JSON, next to any shared dialogue, under the `<mode>_share_*` switches in `bridge/bridge.spec.md`).

## Foreground window

The window the user is looking at when a request arrives, shown to models as distinct from the windows Jarvis acted on.

- Read on demand, once per request that reaches a model: by the local reply engine before tool routing, and by the bridge adapter when it builds a cloud request. Nothing polls, no hook is installed, and the snapshot is never stored: it belongs to that request only. The fast-command path does not read it.
- Only for requests made at the PC: voice and the desktop chat window. A request from a paired phone (origin `phone`, `remote/remote.spec.md`) has no foreground window, because the person is not looking at the PC's screen; nothing is read, presented or shared for it, and the tool router sees no foreground process for it. The records of what Jarvis acted on still apply to phone requests.
- Windows only, and only with `windows_tools_enabled`. The read is bounded (0.5 s); a timeout or error means no foreground window for that request, never a guess.
- Which window: the foreground window, unless it belongs to Jarvis or to the Windows shell. Jarvis's windows are those of its own process and of the desktop app process that started it (the chat window, the face and orb window, the wake overlay, dialogs). Shell windows are the desktop and the taskbars. In those cases the window is the highest application window in the z-order that is neither (typing in the chat window therefore means the window behind it). With no such window there is no foreground window.
- Fields: `application` (the executable's product name, for example "Google Chrome", else its process name), `process`, `hwnd`, `monitor` (display device identifier), `state` (`normal`, `maximised` or `minimised`). Never the title.
- The same read supplies the foreground process the tool router already uses (its context hint and cache key, `reply/reply.spec.md`), so one request reads the foreground once.

## What is recorded

### Windows

One entry per window (or per pending launch), with only these fields:

| Field | Meaning |
|-------|---------|
| `application` | Catalogue name of the application, the name the action was given, the process name, or, for a window a workspace opened, the workspace item's label (the user's own name for it, so two browser windows can be told apart) |
| `process` | Executable stem of the owning process, when known |
| `hwnd` | Decimal window handle, when known |
| `monitor` | Display device identifier, when known |
| `zone` | Zone label the window was placed in, when known |
| `state` | `normal`, `maximised`, `minimised`, `closing`, or empty when unknown |
| `last_action` | `open`, `place`, `focus`, `minimise`, `maximise`, `restore`, `close`, or `control` (`uiControl` clicked, typed, selected, toggled, expanded, scrolled or ran a menu command in it; recorded against the application window that owns any dialog it acted in) |
| age | Seconds since the action (computed when read) |

Recorded outcomes:

- A successful action. `list` and `displays`, and `uiControl` `snapshot` and `read`, change nothing and record nothing; failures record nothing.
- A launch whose placement failed or was not verified records the launch (with `hwnd` when the result carries one), because the application did open.
- `close` records `state: closing`: the application owns completion and may show its own save prompt.

### Devices, media and clipboard

| Kind | Recorded from | Fields |
|------|---------------|--------|
| `device` | A successful `tvControl` action (`key`, `launch`, `type`, `status`), or a device action a local extension records | `device` (`tv`, shown as "TV", or the name the extension gives, shown as given), `tool` (the tool that controls it), `last_action` (the action; for `key`, the key name, for example `key PowerOff`) |
| `media` | A successful `mediaControl` action, including one that found the media already in the requested state, and a `now_playing` that found a session | `application` (the session's application as Windows names it, for example "Apple Music", else a readable form of its identifier), `status` (`playing`, `paused`, `stopped` or empty), `tool` (`mediaControl`) |
| `clipboard` | `inputControl`: `clipboard_write`, `clipboard_read`, and a `hotkey` after which the clipboard changed | `content_type` (`text`, `image`, `files`, `other` or `empty`), `tool` (`inputControl`) |

- Each device, the media session and the clipboard has one entry; a newer outcome replaces it and moves it to the front. Failures record nothing.
- A device acted on by a status read is still the device the conversation is about, so `status` records it (unlike a window `list`, which names no single window).
- The clipboard's type is read from the formats Windows lists for it, never from its data. A hotkey records the clipboard's change counter before the chord; when the record is read and the counter has changed, the entry takes the clipboard's content type at that moment, and until then it is not presented. A hotkey that did not change the clipboard (most do not) therefore never shows a clipboard entry. Nothing waits after the chord.

### Never recorded or presented

Window titles, document names, paths, launch targets, process IDs, rectangles, selected text, clipboard contents, text typed on the TV, track titles and artists, TV app names and conversation text. (The opt-in activity log, `memory/activity_log.spec.md`, is a separate store the user has chosen to enable; it records redacted window titles and shares nothing with this record.)

## Pending launches

A plain `open` returns before the window exists, so it records the application and its process name with no handle. Just before launching, `apps.track_launch` notes the handles of that application's windows that are already open (one window listing; nothing waits for the new window). When the record is read, a pending launch is resolved through a bounded window listing (two seconds at most):

- exactly one window of the application that was not there before the launch is the launched window, recorded with its handle and display;
- anything else (no new window yet, several new windows, a single-instance application that reused its old window) stays unresolved, and the entry says that the window has not been found yet. Nothing is guessed: an older window is never taken for the new one, because a slow application may still be opening its window.

A resolved launch keeps its handle and is not resolved again. An action on a window of the same process replaces a pending launch and keeps its application name.

## Merging and order

- Window entries are newest first. An action on a handle replaces that handle's entry and moves it to the front, keeping the earlier application, monitor and zone when the new outcome does not report them. A maximised or minimised window keeps its display; maximising clears the zone.
- An action on a handle also replaces a pending launch of the same application, and a new pending launch replaces an older pending launch of the same application.
- At most `MAX_ENTRIES` (5) window entries are kept; the oldest is dropped. Device, media and clipboard entries are bounded by their kinds (one per device, one media, one clipboard).

## Lifetime

Readers pass the maximum age, the dialogue memory window (`dialogue_memory_timeout`, default 300 s), so a referent lives about as long as the conversation that produced it. Entries are never written to disk, the diary, the database or logs, and disappear when Jarvis exits. `clear()` empties the store.

## Presentation to models

- Local: `format_for_model()` renders one compact block labelled as reference data and not instructions, with up to three parts, each present only when non-empty:
  - the window the user is looking at, with its handle as the literal `target` value, and, when it is also one of the recent windows, which one;
  - recent windows Jarvis acted on, newest first, each with its handle as the literal `target` value and its age;
  - other things Jarvis acted on, newest first, each with the tool that controls it and its age.
  
  The guidance says: a request about a window that names none means the window the user is looking at when it points at what the user is looking at, and the window Jarvis just acted on when it continues about that window (when they are the same window, it is that one); act on it by passing that target exactly, never an application name (which can match several windows): `appControl` closes or focuses that one window, `windowControl` minimises, maximises, restores or places it. A request that names no device or player most likely continues with the most recent entry it fits; with nothing recent that fits, it is about this computer. Say an action is done only after its tool result confirms it.
- The block is added to the tool router's context hint (in the recent-dialogue section), the planner's dialogue context and the chat system message. It is empty, and nothing is added, when there is no foreground window and no live entry.
- Carry-over: while window entries are live, the engine keeps `windowControl` and `appControl` in the allow-list; while a device, media or clipboard entry is live, it keeps that entry's tool, when that tool is registered (`reply.spec.md`, desktop carry-over). The foreground window alone widens nothing; the router decides from the request and the block.
- Codex and Claude: `foreground_for_request()` returns the foreground window as one bounded object (`application`, `process`, `hwnd`, `monitor`, `state`); `records_for_request()` returns the windows as bounded objects (`application`, `process`, `hwnd`, `monitor`, `zone`, `state`, `last_action`, `age_sec`); `others_for_request()` returns the other entries as bounded objects (`kind`, `tool`, `device` or `application` or `content_type`, `last_action` or `status`, `age_sec`). The request JSON carries them as `foreground_window`, `desktop_referents` and `other_referents`, each only when shared and non-empty, with one note (`desktop_referents_note`, from `request_note()`) that they are reference data and carrying the same guidance as the local block. Text fields are redacted and length-bounded.

## Privacy

The record holds only what Jarvis's own actions returned, minus titles, paths and content. The foreground window is read only when the user makes a request, and only its application, process, handle, display and state leave the OS layer. Local mode sends nothing anywhere. Sharing with a cloud mode is governed per mode: the foreground window by `<mode>_share_foreground_window` (default on; when on it is attached to every request of that mode made at the PC, not only after Jarvis acted on something), and the windows, devices, media and clipboard records by `<mode>_share_desktop_referents` (see `bridge/bridge.spec.md`). Debug logs record counts, kinds and actions, never application names, device names or handles.

## Language

No language patterns are involved. Recording is driven by tool outcomes, the foreground window by the OS, and resolving "it", "this", "that one", "ça" or "das Fenster" is left to the model reading the record.

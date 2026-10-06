# Activity log

An opt-in, local record of which application was in the foreground and what its window was called, so the user can ask "what was I working on yesterday afternoon?". It is off by default, stays in the Jarvis database on this PC, and is the one deliberate exception to the "no window titles" conventions of `platform/windows/windows.spec.md` and `memory/desktop_referents.spec.md`. Those specs describe what Jarvis's own window tools record and return; this spec describes data the user has chosen to have recorded.

## Boundaries

- Nothing is recorded, hooked or stored until `activity_log_enabled` is true. A fresh install, and every user who never opens the setting, runs none of this code: no thread, no hook, no table is created, and `activityLog` is not in the tool catalogue.
- Only the foreground application, its redacted window title and idle time are recorded. Never keystrokes, screen contents, clipboard, files, URLs beyond what the title itself says, or process command lines.
- Core Jarvis never imports `desktop_app`. The tray talks to the daemon through the shared helpers in `memory/activity_runtime.py` (bundled) or stdin lines (subprocess).
- No telemetry and no network use. Activity data never goes into the diary, the knowledge graph, debug logs, the console log, issue reports or a cloud model's context (see Privacy).

## Layout

| Module | Responsibility |
|--------|----------------|
| `memory/activity_log.py` | `ActivityStore` (SQLite table), `ActivityRecorder` (observations to sessions, applying idle, pause, exclusion and redaction), `summarise` and `timeline` (the read side), ISO time parsing, the per-turn privacy mark. No OS imports |
| `memory/activity_exclusions.json` | Locale data: default excluded processes (password managers) and private-browsing title markers per language |
| `memory/activity_runtime.py` | `ActivityService` (watcher events to recorder, daily pruning), the process-wide start, stop, pause and delete entry points |
| `platform/windows/activity.py` | `ForegroundWatcher` (hook thread with its own message loop), `EventGate` (pacing), `foreground_snapshot`, `idle_seconds`. OS functions only |
| `tools/builtin/activity_log.py` | The `activityLog` tool |
| `desktop_app/activity_menu.py` | Tray items and the request path to the daemon |

## What is recorded

One row per session in `activity_sessions`, a table the store creates in the existing SQLite file at `db_path` (it never touches the other tables):

| Column | Meaning |
|--------|---------|
| `start_ts`, `end_ts` | Epoch seconds. An open session's end grows with every observation, so a crash loses at most one observation interval |
| `idle` | 1 for an idle period |
| `process` | Executable stem of the foreground window's process (empty when idle) |
| `app` | Display name from the executable's own file description, falling back to the stem (empty when idle) |
| `title` | The window title after `redact()` (emails, tokens and similar secrets removed), whitespace collapsed, at most 160 characters (empty when idle) |

Paths, process IDs, window handles and command lines are never stored.

## Recording rules

`ActivityRecorder.observe(foreground, idle_sec, now)` keeps at most one open session.

- A new session starts when the application or the redacted title changes. A title change inside one application is a new session.
- **Idle.** When there has been no keyboard or mouse input for `activity_log_idle_after_sec` (default 300), the application session ends at the moment of the last input and an idle session starts there, so a document left open overnight is not counted as work. The first input ends the idle session at that moment. An idle session never starts before the recorder's own start, so a restart does not rewrite earlier time.
- **Exclusions leave a gap, not a trace.** A foreground process listed in `activity_log_excluded_processes` (matched case-insensitively, ignoring spaces and a trailing `.exe`) or a title containing any of `activity_log_private_title_markers` (case-folded substring) ends the open session and records nothing until something recordable is in front. The gap does not appear in the aggregates as an excluded application. Both lists default to the data file (password managers; Incognito, InPrivate, Private Browsing and their translations) and a list in `config.json` replaces the default wholesale. Password manager browser extensions and apps not on the list are not detected.
- **Sleep.** Observations arrive every few seconds. When none has arrived for `activity_log_idle_after_sec` (the PC slept or recording stalled), an open application session ends at the last observation and a new one starts at the next, so waking with a key press does not credit the night to the window left open. An idle session stays idle across the gap.
- No foreground window (a locked screen, for instance) ends the session the same way.
- A session shorter than 2 seconds (an Alt-Tab flick) is not kept.
- **Pause.** While paused nothing is observed or recorded; the open session ends at the pause. `activity_log_paused` is persisted, so a restart does not silently resume recording.
- A row deleted from outside (for example another process clearing history) ends the open session; recording continues with a new one.

## Windows watcher

`ForegroundWatcher` runs one daemon thread with its own message loop. It installs `SetWinEventHook` for `EVENT_SYSTEM_FOREGROUND` and `EVENT_OBJECT_NAMECHANGE` (out of context, all processes) and reports to `ActivityService.on_event`. A name change counts only when it is for the window that is currently in the foreground (`foreground_provider`, replaced in tests so a window that is never activated can stand in).

`EventGate` paces the reports: a foreground change is reported at once; title changes within 2 seconds collapse into one report when the window ends; a 5-second tick also fires so idle is detected and a missed event is caught. Every report makes the service read the current foreground window (bounded to 3 seconds) and the idle time, so events only decide when to look, never what was seen. If the hooks cannot be installed the tick alone keeps recording. The watcher injects no input, changes no focus and touches no window.

## Retention and deletion

- `activity_log_retention_days` (default 30, at least 1). Sessions that ended before the cutoff are deleted at start and once a day while running.
- **Delete all** removes every row, including the open session (recording then carries on from the next observation). The connection uses `secure_delete`, and the write-ahead log is truncated afterwards, so deleted titles do not remain readable in the database file. It works whether or not recording is running.
- Turning the log off stops recording at the next start. Existing history stays until deleted or pruned.

## Tray

- `Pause Activity Log`: a checkable item that follows `activity_log_paused`. Disabled, with a pointer to Settings, while the log is off.
- `Delete Activity History…`: asks for confirmation in a dialog that says what is deleted and that it cannot be undone, with No as the default. Available even while the log is off, so old history can still be removed.
- Both only request the change: bundled mode calls `jarvis.daemon.set_activity_paused` and `delete_activity_history` on a worker thread; subprocess mode writes `__ACTIVITY__:{"action": "pause" | "resume" | "delete" | "stop_sharing"}` to the daemon's stdin; with no daemon running the stored setting or the database is changed directly. The menu reads the stored state each time it opens.
- Settings applies its privacy switches through the same request path when Save changes them while Jarvis runs, whether or not the offered restart is accepted: `Paused` sends `pause` or `resume`, and switching `Share With Codex And Claude` off sends `stop_sharing` (`jarvis.daemon.stop_activity_cloud_sharing` in bundled mode). Switching sharing on takes effect at the next start.
- Voice and text cannot pause, resume or delete: the `activityLog` tool is read only.

## activityLog tool

| Argument | Meaning |
|----------|---------|
| `action` | `summary` or `timeline` |
| `start` | ISO 8601 date or date-time. A time without an offset is local time |
| `end` | ISO 8601; defaults to now. Must be after `start` |
| `limit` | Timeline only: at most this many sessions (default 100, at most 300) |

- `summary` returns the range, `active_seconds`, `idle_seconds`, and `apps` (at most 12, longest first), each with its seconds and its longest `titles` (at most 5) with seconds, plus `recording_since` (the earliest recorded start, or null) so "nothing recorded" can be told apart from "recording began later". Sessions are clipped to the range.
- `timeline` returns the clipped sessions in order (`start`, `end`, `seconds`, `app`, `title`, `idle`), `truncated`, `total_sessions` and `recording_since`.
- Raw aggregates only: the reply model writes the answer. Titles are redacted again on output. Bad input (missing or unparseable times, end before start, unknown action) fails with a message and reads nothing.
- Registered only when `activity_log_enabled` is true on Windows (`registry.configure_activity_log_tool`), so the default catalogue is unchanged. The first 120 characters of the description say what it is for and a "NOT for" clause points `windowControl` (what is open now), `systemInfo` (CPU and RAM) and memory (conversation) elsewhere.
- It is a routine, read-only action (`SAFE`).

## Privacy

- **Cloud boundary.** In Codex and Claude reply modes the tool is withheld from the request's tool snapshot unless `activity_log_share_with_cloud` is true (default false; only a real `true` counts) and sharing has not been stopped since start (`activity_runtime.shared_with_cloud`). The gate lives in the shared `bridge.tools.build_tool_snapshot`, so it applies to both modes, and a call for a withheld tool is refused by the broker like any name outside the snapshot. The same switch governs the shared recent dialogue: turns that quoted the log (kept out of the diary) stay in the local conversation but are left out of the dialogue sent to Codex or Claude unless sharing is on, so switching to a cloud mode right after a local answer about activity sends none of it. When sharing is on, the redacted aggregates the model asks for are sent to the provider like any other tool result; Settings says so. The local model always has the tool when the log is on.
- **Diary and graph.** A turn that used `activityLog` is kept out of the diary: the tool marks the turn on the thread that owns the request, and `_deliver_reply` stores that turn's user and assistant messages with `diary=False`. They stay in the hot window so follow-ups work, but never reach the diary summariser, and therefore never the knowledge graph that is built from diary summaries. A follow-up whose context still holds activity data (a recent message kept out of the diary, or a carried-over `activityLog` result recorded by a private turn) is private too, even when the tool does not run again, so "how long was I in that spreadsheet?" answered from the earlier result is kept out of the diary and the console like the first answer. On a private turn the reply engine's debug logs (`voice_debug`) give the sizes of messages and model output, never their text. A chat session restored from the chat window's archive keeps this: private message texts are remembered in memory (whitespace-normalised) and matched when a session is restored.
- **Console log and issue reports.** A private turn's reply is not printed to the console (the log viewer and the "Report Issue" body are built from it); a placeholder line is shown instead. The reply is still spoken or shown in the chat window. Tool results are never printed.
- **Debug logs** record counts, kinds and reason codes (sessions pruned or deleted, events by kind, entries returned), never application names, titles, paths or time ranges. The recorder and the tool log through `debug_log` with the `activity` tag.
- The tool-result carryover in dialogue memory is in memory only and disappears when Jarvis exits.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `activity_log_enabled` | `false` | Master switch; only a real `true` opts in. Takes effect when Jarvis starts |
| `activity_log_paused` | `false` | Recording paused; set by the tray or Settings |
| `activity_log_retention_days` | 30 | Days of history kept (at least 1) |
| `activity_log_idle_after_sec` | 300 | Seconds without input before time counts as idle (at least 30) |
| `activity_log_excluded_processes` | password managers | Executable names never recorded |
| `activity_log_private_title_markers` | private-browsing markers | Title substrings never recorded |
| `activity_log_share_with_cloud` | `false` | Whether Codex and Claude reply modes are offered `activityLog` |

Each has a Settings row on the Activity Log page. The enabling row states what is recorded and where it stays.

## Language

No language patterns decide anything at run time. Private-browsing markers are locale data in `activity_exclusions.json` (one list per language, flattened for matching, editable in `config.json`); everything else is structural. Resolving "yesterday afternoon" to an ISO range is the reply model's job, using the date and time Jarvis already gives it.

## Limits

- Time spent watching a video or reading without touching the keyboard or mouse counts as idle after `activity_log_idle_after_sec`.
- UWP apps hosted by `ApplicationFrameHost` are recorded under that host's name.
- Only one foreground window is tracked; several monitors do not add a second stream.

## Verification contract

Automated tests (screen-free) assert:

- Recording rules with synthetic observations: sessions, title changes, idle start at the last input and end at the first, exclusions as gaps with no trace, private-browsing markers in several languages, configurable lists, redaction and length bound, pause and resume, flicker filtering, deleting while recording, growth before close, a sleep gap credited to nothing.
- Storage: own table beside existing tables, range queries, pruning, delete-all leaving no readable titles in the file, use from another thread.
- Aggregates: totals per application and title, clipping, idle separate, bounds, `recording_since`, redaction on output.
- Service and daemon: opt-in only, a failed reading does not end a session, pause persistence, delete with and without a running service, daily pruning, shutdown, IPC lines (malformed ones consumed and ignored, delete never on the stdin reader), logs without titles.
- Real window events against a test-owned off-screen window that is never activated: title changes reach the watcher, events for a non-foreground window are ignored, the whole pipeline records title changes as sessions.
- Tool: registration only when enabled on Windows, results, redaction, input errors, the turn mark, no sensitive text in logs.
- Cloud boundary: withheld from the Codex and Claude snapshots by default and for any value other than a real `true`; offered to both when on; a call for the withheld tool is refused; turns that quoted it are left out of the shared recent dialogue unless sharing is on.
- Diary and console: private messages stay in the hot window but out of pending diary chunks, across restores; a private reply is not printed; the mark covers one turn and a stale mark is cleared at the start of the next request; a follow-up answered from carried-over activity data is private and its debug logs carry sizes only.
- Desktop: tray items, confirmation default, request paths, Settings rows, menu wiring.
- Evals (`evals/test_activity_log.py`, live model): "What was I working on yesterday afternoon?" against a seeded database chooses the tool, builds the right range, answers from the aggregates only and stays out of the diary; unrelated requests never touch the log.

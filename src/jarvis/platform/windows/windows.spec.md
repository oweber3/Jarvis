# Windows control spec

Native Windows control is two layers. `jarvis.platform.windows` holds OS functions and knows nothing about tools, the reply engine or the LLM. `jarvis.tools.builtin.windows` holds thin `Tool` adapters that turn those functions into the strings the model sees. This spec covers audio, media, system information, Windows Settings pages, display brightness, power plans, the default audio output and keyboard and clipboard input. Applications, windows, virtual desktops and paths are in `apps_paths.spec.md`.

## Boundaries

- `platform.windows` imports nothing from `jarvis.tools`, `jarvis.reply` or `jarvis.llm`.
- OS libraries (`pycaw`, `comtypes`, `winrt`, `pyvda`, `pywin32`) are imported lazily inside functions, so both layers import on any platform. Tools are added to `BUILTIN_TOOLS` only when `sys.platform == "win32"`.
- Every blocking OS call runs on a daemon worker thread with a hard timeout (`_bounded.run_bounded`). A hung COM or WinRT call surfaces as an error; it never blocks the listener thread. The clipboard actions are bounded to 3 seconds (a hung owner can block `GetClipboardData`). The one exception is sending a hotkey: `SendInput` returns at once, and abandoning a worker part way through a chord could leave a modifier held, so it runs on the calling thread.
- COM is initialised on the worker thread that uses it. Temporary COM interface and device objects are explicitly closed and dropped before garbage collection and `CoUninitialize()`, guaranteeing valid teardown ordering even on exceptions.
- Tools return raw facts. Wording, tone and spoken formatting belong to the unified system prompt.
- All tools in this spec are routine local actions and need no confirmation, with two exceptions: an `inputControl` hotkey that closes or deletes (see below) needs voice confirmation, and `uiControl` (`ui_automation.spec.md`) needs voice confirmation to act on a control named for an irreversible action and denies typing into or reading a password field.
- `windows_tools_enabled` (default `true`) disables every tool in this spec. A disabled tool returns a failure naming the key and touches nothing.
- Process data is limited to executable names, counts, memory and CPU share. Command lines and window titles are never read by these tools. The one exception is the user's explicit, opt-in activity log, which records the foreground application and a redacted window title (`memory/activity_log.spec.md`).

## systemVolume

Controls the master level and mute state of the default playback device through `IAudioEndpointVolume`.

| Action | Behaviour |
|--------|-----------|
| `get` | Current percentage and mute state |
| `set` | Requires `percent`. Accepts a number, a numeric string or `"30%"`. Rounds to a whole percent. Values outside 0 to 100, or non-numeric values, are rejected without changing the device |
| `up` / `down` | Relative change by `amount` percentage points (default 10), clamped to 0 through 100 |
| `mute` / `unmute` | Changes mute only; the level is preserved |

Every successful action reports the resulting state read back from the device.

## mediaControl

Transport controls and now-playing through the System Media Transport Controls (SMTC) session manager, the same source the Windows volume flyout reads.

| Action | Behaviour |
|--------|-----------|
| `pause` | Sends an explicit pause. If media is not playing, reports that it is already paused and sends nothing, so it can never start playback |
| `play` | Sends an explicit play. If media is already playing, reports so and sends nothing |
| `play_pause` | Toggles through the session |
| `next` / `previous` | Sends the session command. Only if a session exists and rejects it does the layer fall back to the media virtual key |
| `now_playing` | Title, artist and playback state of the current session |

- With no active media session, transport actions fail with a message saying there is nothing to control, and no key is sent. `now_playing` succeeds and says nothing is playing.
- A session that rejects `play`, `pause` or `play_pause` produces an "unsupported" failure. The toggle media key is never used as a substitute.
- Only apps that publish SMTC sessions are visible. Apps that do not appear as "nothing is playing".
- A successful action, including one that finds the media already in the requested state, and a `now_playing` that finds a session record the session's application (the name Windows shows for its application identifier, else a readable form of the identifier) and its playback state as a desktop referent (`memory/desktop_referents.spec.md`), so "pause it" reaches the player. Track titles and artists are never recorded.

## systemInfo

Each system-information request runs on a daemon worker with a five-second deadline. A stalled resource read returns an error without blocking the listener. Read-only work already in progress can finish after the deadline.

Read-only resource reporting. `psutil` supplies CPU, RAM, disk and per-process CPU data. Process memory comes from one system-wide process snapshot (`NtQuerySystemInformation`), because querying each process separately takes seconds on a busy machine (a few protected services take hundreds of milliseconds each); per-process `psutil` queries are the fallback when the snapshot is unavailable. `nvidia-smi` supplies GPU data.

| Action | Behaviour |
|--------|-----------|
| `cpu` | Utilisation over a short sampling window, plus logical core count |
| `ram` | Percentage, used, total and free |
| `disk` | Fixed drives only (removable, optical and unreadable volumes are skipped): percentage, used, total and free |
| `gpu` | Per NVIDIA GPU: utilisation, VRAM used and total, temperature. A field the driver reports as unavailable is omitted rather than guessed. With no readable NVIDIA GPU the result says GPU statistics are not available |
| `processes` | Total process count and the largest executables by memory |
| `top_memory` | Executables ranked by combined working-set memory |
| `top_cpu` | Executables ranked by CPU share of total capacity (per-process figures divided by logical cores, so a total never exceeds 100%) |

- Processes sharing an executable name (case-insensitive) are grouped and shown with their process count, so a browser appears as one entry.
- The `System Idle Process` placeholder is excluded. On the per-process fallback, processes that deny access are skipped.
- `limit` applies to listings. Missing, non-numeric or non-positive values use the default; values above the cap are clamped.

## systemSettings

One tool for settings that sit behind the Windows Settings app and a few OS controls. `action` selects the area; `operation` (optional) selects what to do inside it. Every other argument is rejected.

| Action | Behaviour |
|--------|-----------|
| `open_page` | Opens a Windows Settings page through its `ms-settings:` deep link. `page` is a slug from `platform/windows/settings_pages.json` (bluetooth, display, sound, wifi, power, updates, apps, privacy and more). Raw URIs, paths and unknown slugs are rejected without launching anything, and the error lists the known slugs |
| `brightness` | `get`, `set` (needs `percent`, 0 to 100), `up` and `down` (by `amount`, default 10, clamped). Defaults to `set` when `percent` is given and to `get` otherwise. `monitor` (a display number, alias or device, resolved like window placement) limits it to one display; without it every display is addressed |
| `power_plan` | `get`, `list` and `set`. `name` is a language-neutral key (`balanced`, `power_saver`, `high_performance`, `ultimate_performance`), a plan GUID or the plan's name. Defaults to `set` when `name` is given and to `get` otherwise |
| `audio_output` | `list` and `set` the default playback device. `name` is a configured alias, an exact device name or whole words of exactly one device name. Defaults to `set` when `name` is given and to `list` otherwise |
| `night_light`, `do_not_disturb` | Windows offers no supported way to switch these from outside Settings (they live in an undocumented, versioned binary registry blob). Both open their settings page (`night_light`, `focus`) and report `changed: false` with a note saying so |

- **Brightness** uses DDC/CI through the Monitor Configuration API (`dxva2`, VCP code 0x10) for external monitors, because this PC's displays are external, and `WmiMonitorBrightness` for an internal panel. A monitor that does not answer DDC/CI is reported under `unsupported` with the reason; it is never skipped silently and no other monitor stands in for it. If no display supports brightness the call fails. After a write the level is read back and reported with `verified`, which is false when the monitor still reports another value. Physical-monitor handles are always released. The whole operation runs on one bounded worker.
- **Power plans** use `powercfg` as a fixed executable with fixed arguments (plan identifiers are GUIDs taken from its own listing). Parsing keys on the GUID and the parenthesised name, so it does not depend on the Windows display language. A plan that is not installed is reported with the plans that are; another plan is never substituted. Success is reported only after `powercfg` lists the requested plan as active.
- **Audio output** lists active render endpoints and sets the default through `IPolicyConfig` (`pycaw`) for the console and multimedia roles, which is what Windows' own default-device setting changes. The communications role is left alone so call audio does not move. Success is reported only after the endpoint list shows the device as the default. `windows_audio_aliases` (label to device name) names devices that no spoken word matches; it has no settings screen and is edited in `config.json`.
- Tool results are raw JSON. Device and plan names are redacted like other tool output.

## inputControl

| Action | Behaviour |
|--------|-----------|
| `hotkey` | Presses `keys` (modifiers plus one other key joined with `+`, for example `ctrl+shift+esc`) on whichever window already has keyboard focus, using `SendInput` |
| `clipboard_read` | Returns the clipboard text (at most 4000 characters, with a `truncated` flag) with the result redacted |
| `clipboard_write` | Replaces the clipboard text and reports only its length |

- Key names, virtual-key codes, aliases (`control`, `windows`, `escape` and so on), the chords Windows reserves and the chords that close or delete live in `platform/windows/input_keys.json`. This is tool vocabulary, not language matching; spoken names are locale data in the fast path.
- A hotkey is any of `ctrl`, `shift`, `alt`, `win` plus at most one other key, with no repeats. `ctrl+alt+delete` is reserved by Windows and is rejected.
- Nothing activates, moves or searches for a window; if Windows refuses the injected keys (for example, a higher-privilege window has focus) the call fails honestly. Held keys are always released, even when injection fails part way.
- **Safety.** A chord that contains any destructive pattern (`alt+f4`, `ctrl+w`, `ctrl+f4`, `ctrl+q`, `ctrl+d` (moves the selection to the Recycle Bin in File Explorer, so the browser bookmark shortcut also asks first), `shift+delete`, `delete`, `ctrl+shift+delete`, `win+ctrl+f4`), whatever the key order, is classed `CONFIRM_VOICE` with the chord named in the confirmation's action (a key name is not a target path), whatever casing or padding the `action` argument has, so it is never fast-routed and runs only after confirmation. Other hotkeys and both clipboard actions are routine.
- The clipboard sits behind a backend interface so tests never touch the real clipboard. Reading it can expose whatever the user last copied to the reply model, which is why the result is redacted.
- The clipboard's content type (`files`, `text`, `image`, `other` or `empty`) is recorded as a desktop referent (`memory/desktop_referents.spec.md`) after `clipboard_write` and `clipboard_read`, and after a hotkey once the clipboard's change counter shows it changed. The type comes from the formats Windows lists (`IsClipboardFormatAvailable`, `GetClipboardSequenceNumber`): the clipboard is not opened and its data is not read for this, and nothing waits after the chord.

## Dependencies

`pycaw` and `comtypes` for volume and audio outputs, `pyvda` for virtual desktops, `pywin32` for the clipboard and the Windows Search index, `winrt-runtime` and the `winrt-Windows.Foundation`, `winrt-Windows.Foundation.Collections` and `winrt-Windows.Media.Control` projections for SMTC. All are Windows-only requirements. `psutil` is already a dependency; GPU data needs no Python package.

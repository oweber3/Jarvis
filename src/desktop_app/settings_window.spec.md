# Settings Window Specification

Auto-generated settings UI that dynamically builds its interface from config field metadata.

## Overview

The Settings Window provides a graphical interface for editing `config.json` without requiring users to manually edit JSON. It reads the current config, presents categorised fields with appropriate input widgets, and saves changes back.

## Design Principles

1. **Metadata-driven**: All fields are defined in a `FIELD_METADATA` registry. Adding a new config parameter to the settings UI requires only adding a `FieldMeta` entry — no widget code changes. The exceptions are structured lists with their own pages or page headers: MCP servers and the connected accounts on Calendar & Email.
2. **Minimal config files**: Only non-default values are written to `config.json`. Removing a field from the config reverts it to the default.
3. **Preserves unknown keys**: Keys not managed by the UI (e.g. `mcps`, `_config_version`, future additions) are preserved when saving.
   A `choice` field whose stored value is not one of its choices (a model set by hand in `config.json`) shows that value as an extra entry, selected, so it is displayed truthfully and saved unchanged rather than replaced by the first choice.
4. **Plain and explained**: labels name what a setting does in everyday words, and each field's description is shown as a hint under it (and as its tooltip), saying what it does and when to change it.
5. **Basic first**: settings most people never change are marked advanced and shown only while **Show advanced settings** is ticked (see Advanced settings).
6. **Rows follow their choices**: a field that only matters for a particular value of another field (an engine's options, a provider's connection, an option of a switch that is off) is shown only while that value is chosen, updating live before saving. Hidden fields keep their values and are saved unchanged.
7. **Theme-consistent**: Uses the shared orb HUD theme from `themes.py` (`apply_theme`): the HUD header (`JARVIS / SETTINGS` eyebrow, title, subtitle, hairline divider), the category sidebar as `QListWidget#nav`, and roles for every label, notice and status. The window module holds no colours or stylesheets. Category names, field labels, choices and buttons are plain text, never emoji.

## Architecture

```
FieldMeta (dataclass)
  ├── key: str           # config.json key name
  ├── label: str         # Human-readable label
  ├── description: str   # Hint under the field, and its tooltip
  ├── category: str      # Tab grouping key
  ├── field_type: str    # "bool" | "int" | "float" | "str" | "text" | "secret" | "choice" | "device" | "list"
  ├── choices            # For "choice"/"device": [(value, display), ...]
  ├── min_val / max_val  # Numeric bounds
  ├── step               # Increment step
  ├── suffix             # Unit label (e.g. "s", "ms", "WPM")
  ├── nullable           # Whether None is valid (shows placeholder)
  ├── section            # Heading the field sits under on its page
  ├── advanced           # Shown only while "Show advanced settings" is ticked
  ├── shown_when         # Optional condition on other fields' current values
  ├── column             # On a page with columns, the column the field sits in
  └── required           # A text field Save refuses to leave blank
```

Every built-in field has a section. Numbers show as many decimal places as their step (a step of 10 shows `45 s`, a step of 0.05 shows `0.25`).

## Widget Mapping

| field_type | Widget | Notes |
|-----------|--------|-------|
| `bool` | QCheckBox | |
| `int` | QSpinBox | With bounds, step, suffix |
| `int` (nullable) | QCheckBox + QSpinBox | Checkbox enables/disables the spinbox |
| `float` | QDoubleSpinBox | With bounds, step, suffix |
| `str` | QLineEdit | Placeholder if nullable |
| `text` | QPlainTextEdit (multi-line, fixed height) | Saved trimmed, line breaks kept (the custom personality) |
| `secret` | Status label + QLineEdit (EchoMode.Password) + Remove | API keys. Shows only whether a key is stored ("Stored" in the success colour, "Removed on save" in the warning colour, "Not set" muted), never the key. A typed key is stored, or a Remove takes effect, on Save, in the OS credential store (`jarvis.credentials`, Windows Credential Manager). Never written to `config.json`; Reset clears typed input only |
| `choice` | QComboBox | Pre-defined options. An offered empty choice ("Model default", "Automatic", "Off", "Same as chat provider") is saved as `""`, a value of its own, never as `null` |
| `device` | QComboBox | Dynamically populated from sounddevice |
| `list` | QListWidget + Add/Edit/Remove buttons | Stores as JSON array in config |

## Layout

The settings window uses a sidebar navigation pattern: a fixed-width `QListWidget` on the left lists categories, and a `QStackedWidget` on the right shows the selected category's page. A page is a form whose fields are grouped under section headings; a heading is hidden while every field under it is hidden. A page whose fields have a `column` (Cloud Modes) is a grid instead: one row per setting with each column's field side by side, the shared description under the row, and section headings between rows.

The button row holds **Reset to Defaults**, the **Show advanced settings** checkbox, **Cancel** and **Save**.

### Advanced settings

While **Show advanced settings** is unticked, advanced fields are hidden on every page and the Advanced page is not listed in the sidebar; ticking it shows both at once. The choice is saved with the other settings as `settings_show_advanced` (written only while ticked) and is restored when the window opens. Every field on the Advanced page is advanced.

## Categories (Sidebar Order)

1. **General**: Reply mode (the mode Jarvis starts in: local, ChatGPT through Codex, Claude through Claude Code; a cloud mode must also be allowed on Cloud Modes, see `bridge/bridge.spec.md`); Personality (Butler, Plain, Friendly or Custom, and the custom description as a text box shown only for Custom, see `jarvis/personality.spec.md`); Windows control (quick commands, Windows control tools, FancyZones layouts); Web search (web search; Wikipedia fallback and the Brave key are advanced); Performance (low-power mode). Footer: Windows status and safety summary (see Windows Control).
2. **Listening**: Microphone (device; sample rate is advanced); Wake word (word, strictness, wait after the wake word); Conversation (pause before sending, follow-up listening and its time, wake screen effect); Your voice (speaker verification; talk over Jarvis while verification is on). Footer: Voice enrolment (see Voice Input).
3. **Speaking**: Voice (speak replies, voice on this PC, voice engine); Piper voice (speed and sentence pause; expressiveness, rhythm, custom voice file and speaker number are advanced) shown while Piper is chosen; Chatterbox voice (voice to clone, emotion; pacing and device are advanced) shown while Chatterbox is chosen.
4. **AI Models**: Provider (provider; the Ollama address is advanced; server address and API key while the OpenAI-compatible server is chosen); Models (chat model, fast model, tool model, think before answering, answer time limit); Embeddings (see AI Models).
5. **Cloud Modes**: Codex and Claude side by side. Access (allow); Model (model and effort); What it may see (recent conversation, and messages shared while it is on; the window you're looking at, recent desktop actions, your memories, timers, reminders, lists and notes); Limits (answer time limit, queued requests, tool calls per request and program, all advanced). A mode's rows other than Allow are shown while it is allowed. See the Codex and Claude bridge specs.
6. **Privacy**: Screen and files (screen awareness, allow file deletion); Activity log (the off-by-default switch that states what is recorded and where it stays, and while it is on: pause, retention, the two exclusion lists, sharing with Codex and Claude; the idle threshold is advanced; see `memory/activity_log.spec.md`); Location (use location, and while on: detect automatically, address to use; recheck interval and public address look-up are advanced).
7. **Calendar & Email**: the connected accounts (see Calendar & Email below), then Reading (read my calendar, read my email; reuse calendar link for is advanced) and Sharing (share calendar, share email with Codex and Claude, each while its switch is on).
8. **Memory & Dictation**: Memory (conversation ends after, memories per reply; memory source is advanced); Dictation (the switch, and while on: hotkey, custom words, filler word removal, and thinking while cleaning up while filler removal is on).
9. **Extras**: Phone access (the switch, and while on: approve from the phone, quick actions; listen address and port are advanced); Web chat (the switch; port is advanced); TV (Roku TV address); Gesture control (the switch that offers gesture control, and while on: camera, hand, pointer ring, reach, scroll speed; the finger thresholds, hold time, steadiness and fast-move response are advanced; see `gestures/gestures.spec.md`, Gesture snapping).
10. **MCP Servers**
11. **Advanced** (listed only while advanced settings are shown): Speech recognition (model, engine, device, precision, minimum confidence, silence filter); When Jarvis stops listening (speech detection, noise filtering, minimum speech loudness for when speech detection is off or fails, silence that ends a phrase, longest phrase, longest phrase while speaking, longest request, recent speech kept); Echo detection (timing tolerance, estimated speaking rate); Wake check (thinking, time limit); Time limits (routing, time and weather, memory search); Local model (context size and keep-loaded time, while Ollama is chosen); Tool use (planning multi-step requests, how tools are chosen, most steps per request, tool results remembered and their length).

Each enabled local extension that declares settings adds one page after these, titled with the label it gives (`jarvis/extensions/extensions.spec.md`, Settings). Its fields behave like built-in fields: defaults come from the extension, only non-default values are written and unknown keys are preserved. An extension that fails to load adds no page and the window opens as usual.

`tts_rate` ("Estimated Speaking Rate") does not change the voice's speed; echo detection uses it to estimate which words Jarvis was saying. Piper's speed is `tts_piper_length_scale` ("Speaking Speed").

### Speech Recognition

`whisper_backend` offers Auto and Faster Whisper, plus MLX (Apple Silicon) only on macOS. Config validation accepts `mlx` everywhere; a value set by hand that the list does not offer is shown and kept, as for any `choice` field.

### AI Models

The Chat Model and Fast Model lists offer the wizard's catalogue (`SUPPORTED_CHAT_MODELS`) followed by the tested tool-use models (`TOOL_USE_CHAT_MODELS`); the Tool Model list puts the tool-use models first. Both come from `jarvis/config.py`, whose `OFFERED_CHAT_MODELS` is also what the desktop start-up check accepts (`desktop_app.spec.md`, Fallbacks), so no model these lists offer is ever reported as untested.

The page shows the fields of the provider in use, mirroring how the backend resolves them (`llm/llm.spec.md`): with Ollama, `ollama_chat_model` ("Chat Model", a choice from the catalogue) and `ollama_base_url`; with an OpenAI-compatible server, `llm_chat_model` ("Chat Model", free text, since a server's model name is not in the catalogue), `llm_base_url` and `llm_api_key` (secret). The embedding fields follow the provider embeddings actually use (`embedding_provider`, or the chat provider when it is empty): `ollama_embed_model` for Ollama; `embedding_model`, `embedding_base_url` and `embedding_api_key` (secret) for an OpenAI-compatible server. So no two rows with the same label are shown together, and the "Ollama chat + remote embeddings" split (`llm_provider: ollama` with `embedding_provider: openai_compatible`) shows both connections.

The OpenAI-compatible connection fields are nullable: leaving one empty falls back to the Ollama value, and the minimal-config save keeps them out of `config.json` until set. Switching the provider back to Ollama does not clear them (unlike the setup wizard's provider page): the window has no cross-field clearing, and stale values are harmless because the backend resolves per provider. To drop a leftover value, choose that provider, clear the field and save (an API key: Remove, then save).

### Voice Input

The Listening page's Your voice section holds `speaker_verification` ("Speaker Verification": off, soft, strict; default off) and `barge_in_enabled` ("Talk Over Jarvis", default on, shown while verification is soft or strict). The page has a footer built by `_build_voice_footer`, a **Voice enrolment** group with the enrolment status ("Voice enrolled" or "Voice not enrolled"), a hint to run `scripts/enrol_voice.py`, and a **Delete my voiceprint** button. The button is enabled only while a voiceprint exists, asks for confirmation and deletes the file through `jarvis.listening.voiceprint`. Deleting takes effect at once and is not part of Save; the thresholds are config-only. The voiceprint is never displayed or exported.

### Calendar & Email

The page's form has a header built by `_build_accounts_page` (`desktop_app/accounts_page.py`, `_build_category_tab(..., header=...)`) above its fields. It lists the connected calendars and mailboxes ("Calendar: Microsoft (Microsoft account, address)") with **Test** and **Remove** (Remove asks first), and offers: **Sign in with Microsoft** with Calendar and Email ticks, **Sign in with Google** (calendar only), **Use Outlook on this PC** (shown only when classic Outlook is installed), and **Add Gmail...**, **Other email (IMAP)...** and **Paste a calendar link...** dialogs. A sign-in button whose OAuth client ID is empty is not shown, with its ticks and hint. The Gmail dialog has short steps and a button that opens myaccount.google.com/apppasswords; every dialog tests before it saves and stays open with the reason when the test fails. Passwords and links are typed into hidden fields.

Account actions are not part of Save: they run on a worker thread through `jarvis.accounts.manage.AccountStore` (`accounts/accounts.spec.md`) and write `config.json` and Windows Credential Manager at once. While one runs, the page's buttons are disabled; a sign-in shows a Cancel button, and closing the window cancels it. After a change the window sets `accounts_changed` and shows the Read My Calendar and Read My Email switches as the file now has them (an add turns its switch on, removing the last turns it off), so Save neither reverts nor rewrites them. The page notes that changes take effect after a restart.

### Windows Control

The General page's Windows control section holds `fast_commands_enabled` ("Quick Commands", default on), `windows_tools_enabled` and `windows_fancyzones_enabled` ("Use FancyZones Layouts", default on; lets window placement use PowerToys FancyZones zones). The page has a read-only footer built by `_build_windows_footer`:

- **Status**: quick-command state plus an Available/Unavailable row for each
  capability group (applications and windows, volume and media, system
  information, files and folders). Availability is `windows_tools_enabled` on
  `win32`; the rows follow the two checkboxes live, before saving. Computed by
  the pure helpers `windows_capabilities` and `windows_status_lines`.
- **Safety and confirmation**: the fixed policy in `SAFETY_SUMMARY_LINES`
  (routine runs directly, destructive may need a spoken yes or no, higher-risk
  needs desktop confirmation). It is informational only; no control weakens
  or bypasses `tools/confirmation.py`.
- **Config-only note**: one hint line naming the Windows features set only in `config.json` (`windows_workspaces`, `routines`, `windows_app_aliases`, `windows_path_aliases`, `windows_monitor_aliases`, `windows_audio_aliases`, `windows_window_zones`) and the specs that describe them (`platform/windows/workspaces.spec.md`, `routines/routines.spec.md`, `platform/windows/apps_paths.spec.md`, under `src/jarvis/`). It is text only; the page has no rows for these.

`low_power_mode` ("Low Power Mode", General) is a boolean toggle. When enabled, the voice listener skips LLM startup warmup and every Ollama request uses a short keep-alive (1m) instead of `llm_keep_alive`.

## Hardware Device Selection

The Claude Model and Codex Model dropdowns list the models that runtime last reported (`cloud_models.json`, `bridge/bridge.spec.md`, Cloud models), named with their versions such as "Sonnet 5.5", read when the window opens. Before a mode has ever been checked they offer a built-in list. A saved model missing from the list is shown and kept.

The Listening page includes a device dropdown populated at window open time via `sounddevice.query_devices()`. It lists all input-capable devices with their index and name. The stored value is the device index as a string, or empty string for system default.

## Save Behaviour

- A `required` text field left blank (the Wake Word) stops the save: the window shows that field's page and the message "<label> cannot be empty. Enter a value, then save again." and writes nothing, so a blank wake word never reaches `config.json`.
- Only keys that differ from `get_default_config()` are written.
- Save re-reads `config.json` and applies only the fields the user changed in the window, so values written while it was open (a voice "go local", the tray's activity-log pause) are kept rather than reverted to what the window showed on opening.
- API keys (`secret` fields) never reach `config.json`. Opening the window first moves any API key still in the file into the credential store; a typed key the store cannot take is reported and not saved. A key already in the file that could not move (no credential store) stays in the file on Save, because it is the only copy; it moves on a later open once a store is available.
- Existing keys not managed by the UI are preserved (e.g. `mcps`, `wake_aliases`, `stop_commands`).
- `settings_show_advanced` is written as `true` while **Show advanced settings** is ticked and removed otherwise.
- After save, a dialog confirms success and reminds the user to restart.
- If the daemon is running when save completes, or when the window closes in any way after an account was added or removed (`accounts_changed`), the tray app offers to restart it.

## Reset to Defaults

- Prompts for confirmation.
- Resets all widget values to `get_default_config()` values.
- Does NOT immediately save — user must still click Save.

## Integration

- Accessed via "Settings" in the system tray menu.
- Opens as a modal QDialog.
- Lazy-imported to avoid loading sounddevice at startup.

## MCP Servers Section

The MCP Servers category is **not** metadata-driven — it uses a custom page because `mcps` is a complex dict structure.

### Layout

- Description label explaining what MCP servers are
- List widget showing configured servers (display name from catalogue if recognised, otherwise the configured name)
- Buttons: **Add from Catalogue**, **Add Custom**, **Edit**, **Remove**
- Detail panel showing the selected server's name, command, args, and env vars

### Add from Catalogue

Opens `_MCPCatalogueDialog` showing all entries from `mcp_catalogue.CATALOGUE`. Already-configured servers appear checked and disabled. Servers that require an API key show a "Requires <KEY_NAME>" line in the accent colour. When the user confirms, they're prompted for any needed API keys.

### Add Custom

Opens `_MCPEditDialog` with fields for name, command, args (space-separated), and env vars (KEY=VALUE pairs). Validates that name and command are non-empty.

### Edit

Opens `_MCPEditDialog` pre-filled with the selected server's config. Name is read-only during edit.

### Remove

Prompts for confirmation, then removes the server from the in-memory dict.

### Save Behaviour

On save, the `mcps` dict is written to config.json if non-empty, or removed entirely if empty. On reset, all MCPs are cleared.

## Fields NOT Exposed in UI

These fields are managed elsewhere or are too complex for a simple form:

- `db_path` / `sqlite_vss_path`: internal storage paths
- `wake_aliases`: list of strings (complex editing)
- `stop_commands`: list of strings
- `use_stdin`: developer/CLI flag
- `voice_debug`: environment variable only
- `whisper_min_audio_duration`: rarely changed
- `echo_energy_threshold`: read only by the after-reply echo check, which the listener does not use
- `vad_frame_ms` / `vad_pre_roll_ms`: low-level VAD timing
- `planner_timeout_sec`, `tool_search_max_calls`, `memory_digest_enabled`, `tool_result_digest_enabled`, `llm_digest_timeout_sec`: internal tuning of the reply engine
- `speaker_reject_threshold`, `speaker_accept_threshold`, `speaker_barge_in_threshold`, `barge_in_verify_ms`: speaker verification tuning (`listening/listening.spec.md`)
- `fast_commands_locales`, `extensions_enabled`, `extensions_dir`, `tts_voice`: set in `config.json`
- The Windows features named in the General page's config-only note

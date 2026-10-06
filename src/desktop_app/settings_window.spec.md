# Settings Window Specification

Auto-generated settings UI that dynamically builds its interface from config field metadata.

## Overview

The Settings Window provides a graphical interface for editing `config.json` without requiring users to manually edit JSON. It reads the current config, presents categorised fields with appropriate input widgets, and saves changes back.

## Design Principles

1. **Metadata-driven**: All fields are defined in a `FIELD_METADATA` registry. Adding a new config parameter to the settings UI requires only adding a `FieldMeta` entry — no widget code changes.
2. **Minimal config files**: Only non-default values are written to `config.json`. Removing a field from the config reverts it to the default.
3. **Preserves unknown keys**: Keys not managed by the UI (e.g. `mcps`, `_config_version`, future additions) are preserved when saving.
   A `choice` field whose stored value is not one of its choices (a model set by hand in `config.json`) shows that value as an extra entry, selected, so it is displayed truthfully and saved unchanged rather than replaced by the first choice.
4. **Theme-consistent**: Uses the shared orb HUD theme from `themes.py` (`apply_theme`): the HUD header (`JARVIS / SETTINGS` eyebrow, title, subtitle, hairline divider), the category sidebar as `QListWidget#nav`, and roles for every label, notice and status. The window module holds no colours or stylesheets. Category names, field labels, choices and buttons are plain text, never emoji.

## Architecture

```
FieldMeta (dataclass)
  ├── key: str           # config.json key name
  ├── label: str         # Human-readable label
  ├── description: str   # Tooltip text
  ├── category: str      # Tab grouping key
  ├── field_type: str    # "bool" | "int" | "float" | "str" | "choice" | "device" | "list"
  ├── choices            # For "choice"/"device": [(value, display), ...]
  ├── min_val / max_val  # Numeric bounds
  ├── step               # Increment step
  ├── suffix             # Unit label (e.g. "s", "ms", "WPM")
  └── nullable           # Whether None is valid (shows placeholder)
```

## Widget Mapping

| field_type | Widget | Notes |
|-----------|--------|-------|
| `bool` | QCheckBox | |
| `int` | QSpinBox | With bounds, step, suffix |
| `int` (nullable) | QCheckBox + QSpinBox | Checkbox enables/disables the spinbox |
| `float` | QDoubleSpinBox | With bounds, step, suffix |
| `str` | QLineEdit | Placeholder if nullable |
| `secret` | Status label + QLineEdit (EchoMode.Password) + Remove | API keys. Shows only whether a key is stored ("Stored" in the success colour, "Removed on save" in the warning colour, "Not set" muted), never the key. A typed key is stored, or a Remove takes effect, on Save, in the OS credential store (`jarvis.credentials`, Windows Credential Manager). Never written to `config.json`; Reset clears typed input only |
| `choice` | QComboBox | Pre-defined options |
| `device` | QComboBox | Dynamically populated from sounddevice |
| `list` | QListWidget + Add/Edit/Remove buttons | Stores as JSON array in config |

## Layout

The settings window uses a sidebar navigation pattern: a fixed-width `QListWidget` on the left lists categories, and a `QStackedWidget` on the right shows the selected category's form. This avoids horizontal overflow from too many tabs.

## Categories (Sidebar Order)

1. Reply Mode: the mode Jarvis starts in (local, ChatGPT through Codex, Claude through Claude Code) and the "Allow Codex Mode" and "Allow Claude Mode" switches, which state what each cloud mode sends to OpenAI or Anthropic and with which sign-in. A cloud mode that is not allowed cannot be switched to at runtime (see `bridge/bridge.spec.md`)
2. Codex (optional): the model, reasoning effort, executable, deadline, queue and tool-call bounds, and the conversation and personal-data sharing switches (see the Codex bridge spec)
3. Claude (optional): the same settings for Claude: model, effort (with "Model default"), executable, bounds and sharing switches (see the Claude bridge spec)
4. LLM & AI Models
5. LLM Provider
6. Text-to-Speech
7. Piper TTS
8. Chatterbox TTS
9. Voice Input (includes microphone device selection)
10. Wake Word
11. Speech Recognition (Whisper)
12. Voice Activity Detection
13. Timing & Windows
14. Memory & Dialogue
15. Location
16. Windows Control (quick commands, Windows control tools, status and safety summary)
17. Activity Log (optional): the off-by-default switch that states what is recorded (foreground application, redacted window title, idle time) and where it stays, pause, retention, the idle threshold, the two exclusion lists (editable lists whose defaults come from the activity log's data file) and the separate switch that offers the log to the Codex and Claude reply modes (see `memory/activity_log.spec.md`)
18. Phone Access (optional)
19. Features (includes web search, Wikipedia fallback, low-power mode and dictation toggles)
20. MCP Servers
21. Advanced

Each enabled local extension that declares settings adds one page after these, titled with the label it gives (`jarvis/extensions/extensions.spec.md`, Settings). Its fields behave like built-in fields: defaults come from the extension, only non-default values are written and unknown keys are preserved. An extension that fails to load adds no page and the window opens as usual.

### Speech Recognition

`whisper_backend` offers Auto and Faster Whisper, plus MLX (Apple Silicon) only on macOS. Config validation accepts `mlx` everywhere; a value set by hand that the list does not offer is shown and kept, as for any `choice` field.

### LLM Provider

Selects the local runtime that serves the LLM and holds the provider-aware
connection fields: `llm_provider` (Ollama / OpenAI-compatible), `llm_base_url`,
`llm_api_key` (secret), `llm_chat_model`, and the four `embedding_*` fields
(`embedding_provider`, `embedding_base_url`, `embedding_api_key`,
`embedding_model`). The model fields are free-text `str` — an OpenAI-compatible
server's model name is not in the Ollama `SUPPORTED_CHAT_MODELS` catalogue.

Every connection/credential/model field is nullable: leaving it empty falls
back to the Ollama settings on the "LLM & AI Models" page. A default Ollama
install therefore never needs to open this page, and the minimal-config save
behaviour keeps these keys out of `config.json` until the user sets them.

Unlike the setup wizard's provider page, the settings window does **not**
clear the OpenAI-compatible fields when the user switches `llm_provider` back
to Ollama: it is metadata-driven with no cross-field logic, and a blanket
clear would wipe the supported "Ollama chat + remote embeddings" split
(`llm_provider: ollama` with `embedding_provider: openai_compatible`). Stale
values are harmless because the backend resolves per-provider: the Ollama path
uses `ollama_base_url` / `ollama_chat_model` and `OllamaBackend` ignores any
API key. To drop a leftover value, clear that field and save (an API key: Remove, then save).

### Voice Input

Beside the microphone and energy fields, `speaker_verification` ("Speaker Verification": off, soft, strict; default off) and `barge_in_enabled` ("Talk Over Jarvis", default on). The page has a footer built by `_build_voice_footer`, a **Your voice** group with the enrolment status ("Voice enrolled" or "Voice not enrolled"), a hint to run `scripts/enrol_voice.py`, and a **Delete my voiceprint** button. The button is enabled only while a voiceprint exists, asks for confirmation and deletes the file through `jarvis.listening.voiceprint`. Deleting takes effect at once and is not part of Save; the thresholds are config-only. The voiceprint is never displayed or exported.

### Windows Control

Metadata fields: `fast_commands_enabled` ("Quick Commands", default on),
`windows_tools_enabled` and `windows_fancyzones_enabled` ("FancyZones Zones",
default on; lets window placement use PowerToys FancyZones zones). The page has a read-only footer built by
`_build_windows_footer`:

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

The active chat model, fast model, `llm_num_ctx`, `llm_keep_alive` ("Model
Residency"), `llm_routing_timeout_sec` and `llm_chat_timeout_sec` are ordinary
fields on the "LLM & AI Models" page.

### Features

The Features category exposes user-facing runtime toggles that do not need a
dedicated page:

- `web_search_enabled`
- `brave_search_api_key` (secret)
- `wikipedia_fallback_enabled`
- `low_power_mode`
- `dictation_enabled`
- `dictation_hotkey`
- `dictation_filler_removal`
- `dictation_custom_dictionary`

`low_power_mode` is a boolean toggle. When enabled, the voice listener skips
LLM startup warmup and every Ollama request uses a short keep-alive (1m)
instead of `llm_keep_alive`. The setting is saved only when it differs from the
default, like every other metadata-managed field.

## Hardware Device Selection

The Voice Input tab includes a device dropdown populated at window open time via `sounddevice.query_devices()`. It lists all input-capable devices with their index and name. The stored value is the device index as a string, or empty string for system default.

## Save Behaviour

- Only keys that differ from `get_default_config()` are written.
- Save re-reads `config.json` and applies only the fields the user changed in the window, so values written while it was open (a voice "go local", the tray's activity-log pause) are kept rather than reverted to what the window showed on opening.
- API keys (`secret` fields) never reach `config.json`. Opening the window first moves any API key still in the file into the credential store; a typed key the store cannot take is reported and not saved. A key already in the file that could not move (no credential store) stays in the file on Save, because it is the only copy; it moves on a later open once a store is available.
- Existing keys not managed by the UI are preserved (e.g. `mcps`, `active_profiles`, `wake_aliases`, `allowlist_bundles`, `stop_commands`).
- After save, a dialog confirms success and reminds the user to restart.
- If the daemon is running when save completes, the tray app offers to restart it.

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

- `db_path` / `sqlite_vss_path` — internal storage paths
- `active_profiles` — list managed by setup wizard
- `allowlist_bundles` — list of bundle IDs
- `wake_aliases` — list of strings (complex editing)
- `stop_commands` / `stop_command_fuzzy_ratio` — list of strings
- `use_stdin` — developer/CLI flag
- `voice_debug` — environment variable only
- `whisper_min_audio_duration` / `whisper_min_word_length` — rarely changed advanced params
- `vad_frame_ms` / `vad_pre_roll_ms` — low-level VAD timing

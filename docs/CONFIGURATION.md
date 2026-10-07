# Configuration guide

[← Back to Jarvis](../README.md)

Detailed settings, dictation, integrations and troubleshooting. Start with the desktop Settings window; JSON examples are for custom setups.

## Configuration

Most users won't need to change anything. Open **Settings** from the tray menu to configure Jarvis through a graphical interface: no JSON editing required. Settings are saved to `~/.config/jarvis/config.json`.

<details>
<summary><strong>LLM Provider (Ollama or OpenAI-compatible)</strong></summary>

By default Jarvis runs everything locally through [Ollama](https://ollama.com): no API keys, nothing leaves your machine. If you already run an OpenAI-compatible server you can point Jarvis at it instead. Your data still only travels to the servers you control.

Pick the provider after speech recognition in the Setup Wizard, or under **Settings → LLM Provider**. No JSON editing required. On the OpenAI-compatible page the wizard does the legwork for you: it auto-detects running local servers, offers a one-click preset for your app, and when you press **Connect** it loads the server's model list and checks the chosen model for chat, tool calling, and embeddings, so you know it works before you finish setup.

Both provider paths include a model-memory budget. Ollama uses its known model estimates; the OpenAI-compatible page lets you edit estimates because servers usually expose model names but not their memory needs. A server on another machine has a separate budget from Whisper on this machine. Estimates are guidance, not a guarantee: quantisation, context length and concurrent workloads affect actual use.

Tested local servers (all run on your own machine):

| App | Default base URL | Notes |
|-----|------------------|-------|
| LM Studio | `http://localhost:1234/v1` | Chat, tool calling, and embeddings. |
| Ollama (OpenAI API) | `http://localhost:11434/v1` | The native Ollama path is the default; the OpenAI shape works too. |
| Jan | `http://localhost:1337/v1` | Chat and tool calling. |
| llama.cpp (`llama-server`) | `http://localhost:8080/v1` | Tool calling depends on the model. |
| LocalAI | `http://localhost:8080/v1` | Feature support depends on the backend model. |
| vLLM | `http://localhost:8000/v1` | Tool calling depends on the model. |
| oMLX (Apple Silicon) | varies | No embeddings endpoint, so memory uses keyword search unless you route embeddings to Ollama (below). |

For reference, the underlying config keys are:

```json
{
  "llm_provider": "openai_compatible",
  "llm_base_url": "http://localhost:1234/v1",
  "llm_chat_model": "your-served-model-name"
}
```

- `llm_base_url`: your server's OpenAI API base URL.
- API key: only if your server requires one. Enter it in **Settings → LLM Provider** or the setup wizard. API keys (`llm_api_key`, `embedding_api_key`, `brave_search_api_key`) are kept in Windows Credential Manager, never in `config.json`; Settings shows only whether one is stored. A key found in `config.json` (from an older Jarvis or a hand edit) is moved there on the next start.
- `llm_chat_model`: whatever model name your server exposes.
- `fast_model` (optional): the small, quick model used for real-time work (voice intent, tool routing, quick classifications). Leave empty for automatic: `qwen3.5:0.8b` on Ollama, your chat model on an OpenAI-compatible server. Set it to pin a dedicated small model.
- `tool_model` (optional, local mode): a separate model that chooses and calls tools, while the chat model writes the reply. Empty (default) keeps the chat model doing both. `gpt-oss:20b` chose tools best in testing; it needs about 13 GB, so unless it fits in your GPU memory beside the chat model, set the chat and fast models to it as well to avoid reloading models on every request.

**Embeddings** (used for memory search) can run on a different backend. If your chat server has no embeddings endpoint, memory falls back to keyword search. To keep full semantic memory, route embeddings to Ollama (the wizard offers this automatically when it detects a server that cannot embed):

```json
{
  "embedding_provider": "ollama",
  "embedding_model": "nomic-embed-text"
}
```

Leave `embedding_provider` empty to use the same provider as chat. With no working embeddings, memory search degrades gracefully to keyword search.

</details>

<details>
<summary><strong>Power and Startup</strong></summary>

Jarvis favours fast first responses by default: it warms Whisper, the chat
model, and the intent judge before announcing that it is listening. On Macs or
laptops where heat and battery matter more than instant first-token latency,
enable **Settings → Features → Low Power Mode**.

```json
{
  "low_power_mode": true
}
```

Low Power Mode skips LLM startup warmup and shortens Ollama model residency for
all inference requests from `llm_keep_alive` (30 minutes by default) to 1 minute. Whisper still warms so voice input
is ready. The first LLM-backed request after startup or idle may be slower.

</details>

<details>
<summary><strong>Speech Recognition (Whisper)</strong></summary>

#### Language Modes
- **Multilingual** (default, 99 languages): `"whisper_model": "medium"`
- **English Only** (slightly better English accuracy): `"whisper_model": "medium.en"`

#### Model Sizes
| Model | English | Multilingual | Download | VRAM | Speed |
|-------|---------|--------------|----------|------|-------|
| Tiny | `tiny.en` | `tiny` | ~75 MB | ~1 GB | ~10x |
| Base | `base.en` | `base` | ~140 MB | ~1 GB | ~7x |
| Small | `small.en` | `small` | ~465 MB | ~2 GB | ~4x |
| **Medium** | `medium.en` | `medium` | ~1.5 GB | ~5 GB | ~2x |
| Large V3 Turbo | - | `large-v3-turbo` | ~1.5 GB | ~6 GB | ~8x |

Speed is relative to the original large model. [Source](https://github.com/openai/whisper)

`large-v3-turbo` is available when MLX Whisper is usable on Apple Silicon or
faster-whisper 1.1.0 or newer is installed. If the selected faster-whisper
cannot load turbo, the wizard hides that option and an existing turbo setting
loads `medium` instead. Choose a different model in Settings → Whisper.

With the faster-whisper backend, `whisper_model` also accepts a Hugging Face
model ID such as `deepdml/faster-whisper-large-v3-turbo-ct2`, or a local
converted model directory.

#### GPU Acceleration (Windows)
If you have an NVIDIA GPU, Jarvis can use CUDA for much faster speech recognition. The Windows installer offers an optional CUDA download during setup. For development:
```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```
CUDA is detected automatically: no configuration needed.

#### Hallucination Filters
Whisper sometimes produces confident but false transcriptions during silence or background noise (e.g. news-show intros, music). Two thresholds filter these out before they reach the intent judge:

- `"whisper_min_confidence": 0.3`: drops segments whose `avg_logprob`-derived confidence falls below this value. Raise if you see low-confidence noise leaking through; lower if real speech is being dropped.
- `"whisper_no_speech_threshold": 0.5`: drops any segment whose `no_speech_prob` is at or above this value, regardless of `avg_logprob`. Catches the case where Whisper is confident about a hallucinated phrase but its own no-speech signal says the audio was silent. Applies to both the faster-whisper and MLX backends.

Both thresholds are exposed in the Settings window under *Whisper*.

</details>

<details>
<summary><strong>Voice Interface (Advanced)</strong></summary>

**LLM Intent Judge** - Jarvis uses a small LLM for intelligent voice intent classification (echo detection, query extraction, stop commands). On the default Ollama setup this is `qwen3.5:0.8b`, installed automatically alongside your chosen chat model during setup. On an OpenAI-compatible provider the judge uses your served chat model instead, so there is nothing extra to install. The intent judge cannot be disabled but gracefully falls back to simpler text matching if the LLM server is unavailable.

**Tool Router** - When `"tool_selection_strategy": "llm"` (the default), Jarvis asks the fast model to pick which tools are relevant for each query, shrinking the tool catalogue the chat model sees. It's already warm and small enough not to stall the turn. Other strategies: `"keyword"` (fast, no LLM), `"embedding"` (nomic-embed-text), `"all"` (no filtering).

**Task-list Planner** - Before the agentic loop, Jarvis runs a short planning pass that decomposes multi-step queries into an ordered list of sub-tasks. For small models (`gemma4:e2b` class), each planned step is directly resolved to a concrete tool call without relying on the chat model to re-plan turn-by-turn. This significantly improves multi-step reliability. Config options:

```json
{
  "planner_enabled": true,          // set to false to disable the planner entirely
  "planner_timeout_sec": 6.0        // per-call timeout for plan and step-resolver LLM calls
}
```

</details>

<details>
<summary><strong>Small-Model Digest Passes (Advanced)</strong></summary>

Small chat models (~2B, e.g. `gemma4:e2b`) degrade sharply as their prompt grows. Jarvis runs two cheap distil passes to keep the prompt tight:

- **Memory digest**: boils diary + graph recall into a short relevance-filtered note before injecting it as background context.
- **Tool-result digest**: boils a raw tool payload (especially webSearch UNTRUSTED WEB EXTRACT blocks) into a short attributed fact note before it reaches the main reply model.

Both digest passes auto-enable for small models (≤7B) and stay off for large models. For small models, tool-result digest also prevents large fetch_web_page payloads from blowing the context window. Override in `~/.config/jarvis/config.json`:

```json
{
  "memory_digest_enabled": null,          // null = auto-on for SMALL, false to force off, true to force on
  "tool_result_digest_enabled": null,     // null = auto-on for SMALL, false to force off, true to force on
  "llm_digest_timeout_sec": 8.0           // tight ceiling shared by both passes
}
```

Field logs show `🧩 Memory digest: …` and `🧩 Tool digest: …` lines when a pass ran, so you can see when the substrate was replaced.

</details>

## Windows Control Tools

Applications and windows use the existing tool path: “Open Word”, “Open Chrome”, “Open MATLAB”, “Switch to Spotify” and “Minimise Chrome”. “Open my Downloads folder” uses the Windows Known Folder API, including redirected folders. Files open in their associated application; HTTP(S) URLs open in the default browser. Ambiguous application names or multiple matching windows require a more specific name or a window handle from the listing. Closing requests a normal application close, allowing save prompts.

`windows_app_aliases` maps your preferred names to discovered installed application names or launch targets, for example `{"editor": "Microsoft Word"}`. The application catalogue uses Windows shortcuts, App Paths and packaged application entries. These tools need no additional Python dependencies.

Natural commands use the configured model and existing tool router. Very small models can choose the wrong tool or omit an argument. The local Windows command eval covers routing and arguments for the example commands; `qwen3:4b` passes this eval. Window focus can also be refused by Windows foreground restrictions or elevated applications.

On Windows, Jarvis can control the system volume, media playback and report system information. Everything runs locally; nothing is sent anywhere.

| Tool | What it does | Location |
|------|--------------|----------|
| `appControl` | Open installed apps (optionally placed on a monitor and zone), switch/focus windows, graceful close, list | `src/jarvis/tools/builtin/windows/desktop_control.py` |
| `windowControl` | Minimise, maximise, restore window size, list windows and displays, place a window on a monitor and zone, switch, create and close virtual desktops and move a window to another desktop | `src/jarvis/tools/builtin/windows/desktop_control.py` |
| `openWebsite` | Open a website or web page, optionally in a new browser window placed on a monitor and zone (the main monitor when none is named) | `src/jarvis/tools/builtin/windows/desktop_control.py` |
| `openPath` | Open files or known folders (Downloads, Documents, Desktop), optionally placed on a monitor and zone, or find a file or folder by name (Everything when it is running, otherwise the Windows Search index) | `src/jarvis/tools/builtin/windows/desktop_control.py` |
| `workspaceControl` | Open a named workspace (a set of browser windows and apps placed on your displays) or list the configured ones. Only offered when `windows_workspaces` is set | `src/jarvis/tools/builtin/windows/workspace_control.py` |
| `systemVolume` | Get or set volume percentage, step up/down, mute/unmute | `src/jarvis/tools/builtin/windows/system_volume.py` |
| `systemSettings` | Open a Windows Settings page (Bluetooth, display, sound, Wi-Fi, power, updates and more), get or set brightness (DDC/CI for external monitors), switch the power plan or the default audio output | `src/jarvis/tools/builtin/windows/system_settings.py` |
| `inputControl` | Press a keyboard shortcut in the active window (shortcuts that close or delete ask for confirmation), read or replace the clipboard text | `src/jarvis/tools/builtin/windows/input_control.py` |
| `mediaControl` | Play, true pause (SMTC), next/previous, now playing | `src/jarvis/tools/builtin/windows/media_control.py` |
| `systemInfo` | CPU, RAM, disk, GPU load/VRAM, top memory/CPU processes | `src/jarvis/tools/builtin/windows/system_info.py` |

The underlying OS interactions live cleanly in `src/jarvis/platform/windows/` (`apps.py`, `windows_mgmt.py`, `audio.py`, `media.py`, `telemetry.py`, `files.py`, `file_search.py`, `steam.py`, `brightness.py`, `power.py`, `audio_outputs.py`, `virtual_desktops.py`, `input_control.py`, `settings_pages.py`), with no dependencies on LLMs or tools.

```json
{
  "windows_tools_enabled": true,
  "windows_app_aliases": {
    "editor": "Microsoft Word"
  }
}
```

`windows_tools_enabled` (default `true`) toggles all Windows tools. Both it and `fast_commands_enabled` can be adjusted on the Settings window's **Windows Control** page.

### System Controls

On Windows Jarvis can open Settings pages, change brightness, the power plan and the default audio output, switch virtual desktops, press keyboard shortcuts and open a file or folder by name. Simple whole-sentence requests ("open Bluetooth settings", "set brightness to 40%", "switch to desktop 2", "press control shift escape") run instantly without the model; anything else goes through the usual model path.

- **Brightness** works on external monitors that support DDC/CI (switch it on in the monitor's own menu if it is off) and on laptop panels. A monitor without DDC/CI is reported as unsupported rather than skipped silently.
- **Audio output.** `windows_audio_aliases` gives your playback devices friendly names, for example `{ "headphones": "Headset Earphone (USB Audio)" }`. Ask Jarvis "list my audio outputs" to see the exact device names. Without an alias, a spoken name works when it matches whole words of exactly one device name.
- **Night Light and Do Not Disturb** have no supported switch outside Windows Settings, so Jarvis opens their settings page instead.
- **Keyboard shortcuts** are sent to whichever window has focus. Shortcuts that close or delete (Alt+F4, Ctrl+W, Delete and similar) ask for confirmation first.
- **Open by name.** "Open my report" searches your files with [Everything](https://www.voidtools.com/) when it is running and its SDK library (`Everything64.dll`) is installed, otherwise with the Windows Search index. Several matches are listed for you to choose from; Jarvis never guesses. Installed Steam games open by name too.

### Monitor and Zone Placement

Jarvis can open an app in a chosen place, or move a window that is already open, so "open Word on my left monitor" or "put Chrome on the right half of the second screen" work through the usual model path. Two optional settings in `config.json` give your displays and layout friendly names. Neither is required: without them you can still use the display identifiers Jarvis reports (for example `\\.\DISPLAY2`), and a window can be placed on a display or maximised there.

```json
{
  "windows_monitor_aliases": { "left": "\\\\.\\DISPLAY2", "main": "\\\\.\\DISPLAY1" },
  "windows_window_zones": {
    "\\\\.\\DISPLAY1": { "left half": [0, 0, 0.5, 1], "right half": [0.5, 0, 0.5, 1] }
  }
}
```

- `windows_monitor_aliases` maps your label to a display identifier. Ask Jarvis to list your displays to see the identifiers; they can change after reconnecting a monitor, which is what the aliases are for.
- `windows_window_zones` maps a display identifier to named zones. A zone is `[x, y, width, height]` as fractions of that display's work area (the screen minus the taskbar), so `[0.5, 0, 0.5, 1]` is the right half. Zones must fit inside the work area. Invalid entries are ignored.
- A disconnected display is never replaced by another one, and a window that cannot reach the requested size or place is reported as such rather than as a success.
- If an app launches but its window cannot be found or placed, Jarvis says so. It does not launch the app a second time.

**Naming displays.** Without any configuration you can say "the second monitor" (displays are numbered in Windows display-number order, as in Windows Settings), "the primary monitor", or "the left" or "right monitor" (by where the displays sit on your desk). A label you define in `windows_monitor_aliases` always takes priority over these.

#### Using your PowerToys FancyZones layouts

If you use [PowerToys FancyZones](https://learn.microsoft.com/windows/powertoys/fancyzones), you do not need to write zones into `config.json`. Jarvis reads the layout you already applied to each connected display on your current virtual desktop and offers its zones, so "open Apple Music in the left zone of my second monitor" or "put Chrome in zone 2" work out of the box.

- Zones are numbered from 1 in the order FancyZones numbers them, and always answer to their number. Where it is unambiguous they also answer to `left`, `right`, `middle`, `top`, `bottom` and the four corners (a two-column layout has a left and a right zone; a four-column layout has only numbers). Ask Jarvis to list your displays to see every zone and its name.
- Built-in layouts (columns, rows, grid, priority grid, focus) and custom grid and canvas layouts are supported, including spacing between zones. A window placed this way lands where FancyZones would snap it. It is not registered with FancyZones, so FancyZones does not remember it as snapped.
- A zone you define in `windows_window_zones` takes priority over a FancyZones zone with the same name.
- This is read-only: Jarvis never changes PowerToys' files, and the layouts never leave your PC. If PowerToys is not installed, or its files cannot be read, placement works exactly as before with your own zones.
- FancyZones' option to span zones across monitors is not supported.
- `windows_fancyzones_enabled` (default `true`, also on the Settings window's **Windows Control** page) turns this off.

### Workspaces

A workspace is a named desk set-up that Jarvis opens in one go: "open my study workspace" (by voice or text, in any reply mode) opens each window you listed and puts it where you said. Workspaces are plain configuration, so adding another one means editing `config.json`, with no code change.

```json
{
  "windows_workspaces": {
    "study": {
      "aliases": ["revision"],
      "items": [
        {"kind": "browser_window", "label": "notes",
         "urls": ["C:\\Users\\you\\Documents\\notes.pdf", "C:\\Users\\you\\Documents\\worksheet.pdf"],
         "monitor": "primary", "zone": "left"},
        {"kind": "browser_window", "label": "ChatGPT",
         "urls": ["https://chatgpt.com"],
         "monitor": "primary", "zone": "right"}
      ]
    }
  }
}
```

- `items` run in order. Each has a `kind`, an optional `label` (how Jarvis names it in results and when you say "move the ChatGPT one"), and a placement:
  - `monitor` (required): anything you can say for placement: `primary`, `left`, `right`, a number such as `2`, an alias from `windows_monitor_aliases` or a display identifier.
  - `zone` (optional): a zone name or number from `windowControl displays` (your own zones or your FancyZones layout), or `[x, y, width, height]` as fractions of the display's work area when no named zone fits, for example `[0, 0, 0.5, 1]` for the left half.
  - `state` (optional): `restore` (the default) or `maximise`. `maximise` cannot be combined with a zone.
- `browser_window` opens one **new** browser window with every entry of `urls` as a tab. Entries are `http(s)` URLs or local file paths (`%ENVVARS%` and `~` work). Other schemes and executable or script files are refused. `browser` is optional: without it Jarvis uses your default browser if it is Chrome, Edge, Brave or Vivaldi, and Chrome otherwise.
- `app` (`{"kind": "app", "target": "Word", "monitor": "2"}`) uses an installed application by the name you would give `open`. If exactly one window of it is already open, that window is placed; if none, it is launched once and placed; if several, that item is reported as ambiguous.
- `aliases` are other names for the same workspace. A name or alias shared by two workspaces is ignored.
- Everything is checked before anything opens: every file must exist, every URL must be `http(s)`, and every monitor and zone must exist on your current displays. If one item is wrong, nothing opens and the error names that item.
- Each item is launched once and never launched again if placing it fails. The result lists, per item, `opened_and_placed`, `placed_existing` or `failed` with a reason, and one failed item does not stop the others. A workspace that has not finished within about 45 seconds reports the remaining items as not started. Only one workspace opens at a time.
- Your paths and URLs stay in `config.json`. They are never written to the debug log, and results shown to the assistant (including a cloud model's, in Codex or Claude mode) carry only labels and outcomes. Opening a workspace is a routine action and does not ask for confirmation.
- "Open my study workspace", "set up study" and similar phrases are recognised without a model (see Fast local commands); anything less clear goes to the assistant, which can list your workspaces and open one. The `workspaceControl` tool is only offered when `windows_workspaces` has at least one valid entry. Invalid workspaces are ignored, and the file is never rewritten.
- There is no Settings screen for workspaces. Restart Jarvis after editing them.

### Roku TV

`tvControl` drives a Roku TV over your home network (Roku's own local control protocol; no account and no cloud). It is registered only when `roku_host` is set, so nothing changes for anyone without a TV.

```json
{ "roku_host": "192.168.1.50" }
```

- The value must be the TV's IP address on your home network: `10.x.x.x`, `172.16.x.x` to `172.31.x.x`, `192.168.x.x` or a link-local address. Hostnames, ports and public addresses are rejected, so the tool cannot be pointed at the internet. Restart Jarvis after changing it.
- On the TV: **Settings → System → Advanced system settings → Control by mobile apps → Network access** must be Default or Permissive. When it is Limited or Disabled the TV answers 403 and Jarvis says where the setting is.
- Reserve the TV's address in your router. If it changes anyway, Jarvis finds the same TV again by its serial number (an SSDP search on your network) and asks you to update `roku_host`. `python -m jarvis.devices.roku --discover` lists the Rokus it can see and never presses a button.
- Actions: `key` (remote keys, with `repeat`), `launch` (an app or input by name, resolved against the apps the TV reports), `type` (text for on-screen keyboards) and `status`. Power off needs no confirmation.
- Fast phrases (English): "turn on/off the TV", "TV volume up/down", "mute the TV", "pause the TV", "TV home", "put on Netflix", "open Hulu on the TV". They always say "TV"; without it the PC's volume and media tools answer.

Spec: `src/jarvis/devices/roku.spec.md`.

### Safety and Confirmation Policy

Tool actions follow a centralised safety policy in `src/jarvis/tools/confirmation.py`:

- **`SAFE`**: Routine local actions execute immediately without prompting (opening apps, setting volume, querying system usage, listing windows/files).
- **`CONFIRM_VOICE`**: Normal destructive actions require spoken or typed confirmation ("yes"/"no"), such as deleting a user file, overwriting a file, or terminating a standard user process. Deleting a file additionally needs `file_delete_enabled` (Settings → Windows Control → Allow File Deletion, off by default); while it is off a delete is refused outright, without a confirmation question.
- **`CONFIRM_DIALOG`**: High-risk operations (system shutdown/reboot, uninstalling software, killing critical system processes, bulk deletions, or actions targeting system directories) require explicit confirmation via a desktop dialog. Voice cannot authorise dialog actions.
- **`DENY`**: Prohibited operations (e.g. formatting a drive or wiping filesystem root) are rejected unconditionally.

Fast-command routing is strictly gated to `SAFE` routine actions and cannot bypass the safety policy.

### Adding a Deterministic Command Family

Fast commands are matched purely in `src/jarvis/fastpath/matcher.py` using locale phrase definitions in `src/jarvis/fastpath/phrases/<lang>.json` (e.g. `en.json`):

1. Define the command patterns and slots in `src/jarvis/fastpath/phrases/en.json` under your command family key.
2. In `src/jarvis/fastpath/matcher.py`, add the slot parser and validate the arguments to target a registered `SAFE` tool.
3. Provide the acknowledgment template or direct tool result formatting.
4. If the request contains extra tokens or ambiguous intent, return `None` to safely fall through to the LLM path.

### Running Developer Tests

Run the test suite using the project environment:

```powershell
# Core Windows tools, safety, fast routing, and settings
.\.mamba_env\python.exe -m pytest -o cache_dir=.tmp/pytest_cache tests/test_fastpath.py tests/test_fastpath_integration.py tests/test_fastpath_workspaces.py tests/test_action_safety.py tests/windows_control/ tests/test_windows_workspaces.py tests/test_windows_workspace_tool.py tests/test_config_windows_workspaces.py tests/test_llm_request_shape.py tests/test_settings_window.py

# Full custom regression test group
.\.mamba_env\python.exe -m pytest -o cache_dir=.tmp/pytest_cache tests/test_tools.py tests/test_action_safety.py tests/windows_control/ tests/test_fastpath.py tests/test_fastpath_integration.py tests/test_llm_request_shape.py tests/test_settings_window.py tests/test_intent_judge.py tests/test_dialogue_memory.py tests/test_windows_control.py tests/test_chat_submission.py tests/test_chat_window.py tests/test_desktop_app.py tests/test_planner.py tests/test_system_prompt.py tests/test_voice_listener.py
```

## Voice recognition and talking over Jarvis

Jarvis can learn your voice so that, after a wake, it ignores speech that is clearly someone else (a game, a video, another person) and lowers its own voice when you talk over it. Everything runs locally on the CPU.

1. Enrol once: `python scripts/enrol_voice.py` and read the six prompts aloud in a quiet room. The first run downloads a 26 MB speaker model (WeSpeaker ResNet34, CC-BY-4.0). To enrol from recordings instead: `python scripts/enrol_voice.py --wav a.wav b.wav c.wav`. `--status` shows whether a voice is enrolled and `--delete` removes it.
2. Turn it on in Settings, Voice Input, **Speaker Verification**: `soft` ignores voices that are confidently someone else, `strict` responds to your voice only.

| Setting | Default | Meaning |
|---------|---------|---------|
| `speaker_verification` | `off` | `off`, `soft` or `strict` |
| `barge_in_enabled` | `true` | Lower Jarvis's voice when your enrolled voice is heard over it |
| `speaker_reject_threshold` | `0.25` | Soft mode: similarity below this is treated as another speaker |
| `speaker_accept_threshold` | `0.45` | Strict mode: similarity needed to count as you |
| `barge_in_verify_ms` | `300` | Speech scored before Jarvis is lowered |
| `speaker_barge_in_threshold` | `0.2` | Similarity, with Jarvis's own voice mixed in, that counts as you |

Your voiceprint is a file called `voiceprint.npz` next to `config.json`. It is never logged, never sent to a model or reply mode (including Codex), and can be deleted from Settings or with `--delete`. If the voiceprint or model is missing, Jarvis logs one warning and carries on as if verification were off. With `voice_debug` on, the log shows each verdict with its similarity score, which helps if you need to adjust the thresholds for your microphone.

Barge-in works best when you speak louder than Jarvis's voice reaches the microphone, for example with a headset microphone, or with the speakers angled away from the microphone. It lowers the voice first and then lets speech recognition decide: a request or stop ends the reply, anything else lets it carry on.

## Dictation Mode

Hold a hotkey to record speech, release to paste the transcription into your editor, browser or other app. Transcription runs locally.

> Dictation is unavailable on macOS 26+ because of a pynput incompatibility. Linux requires X11; Wayland support is limited.

| Platform | Default hotkey |
|----------|---------------|
| **Windows** | Ctrl + Win |
| **macOS** | Ctrl + Option |
| **Linux** | Ctrl + Alt |

- 🔒 **100% offline**: your speech never leaves your machine (unlike cloud dictation services)
- 🧠 **Shared Whisper model**: uses the same speech recognition as voice input, no extra memory
- ⚡ **Local transcription**: no remote transcription service required
- 📋 **Universal paste**: works in any app that accepts `Ctrl+V` / `Cmd+V`
- 🔇 **Non-intrusive**: main voice listener pauses automatically during dictation
- ✋ **Hands-free mode**: double-tap the hotkey to keep recording without holding; press again or hit Escape to stop
- 🧹 **Filler word removal**: optional LLM-powered cleanup removes "um", "uh", "like", "you know" while preserving meaning
- 📖 **Custom dictionary**: define `"wrong -> right"` replacements for jargon, names, and technical terms
- 📜 **History window**: browse, copy, or delete past dictations from the system tray
- 🎛️ **Easy setup**: configure dictation during the setup wizard or anytime in Settings (hotkey dropdown, filler removal toggle, custom dictionary editor)

Customise the hotkey in Settings or `config.json`:
```json
{
  "dictation_hotkey": "ctrl+alt",
  "dictation_filler_removal": true,
  "dictation_custom_dictionary": [
    "jarvis -> Jarvis",
    "pytorch -> PyTorch"
  ]
}
```

> **Note:** macOS requires Accessibility permissions for the global hotkey. Linux requires X11 (limited Wayland support).

<details>
<summary><strong>Text-to-Speech</strong></summary>

**Piper TTS (default)** - Neural TTS that auto-downloads on first use (~60MB):
- Works out of the box - no setup required
- High-quality British English male voice (en_GB-alan-medium)
- Fast local synthesis with exact duration tracking

To use different Piper voices, download from [HuggingFace](https://huggingface.co/rhasspy/piper-voices) and set:
```json
{
  "tts_piper_model_path": "~/.local/share/jarvis/models/piper/en_GB-alan-medium.onnx"
}
```

**Chatterbox** - AI voice with emotion control (requires running from source):
```json
{ "tts_engine": "chatterbox" }
```

Voice cloning with Chatterbox - add a 3-10 second .wav sample:
```json
{
  "tts_engine": "chatterbox",
  "tts_chatterbox_audio_prompt": "/path/to/voice.wav"
}
```

</details>

<details>
<summary><strong>Location Detection</strong></summary>

Jarvis can provide location-aware responses (weather, local time, etc.) using a local GeoLite2 database: no cloud geolocation services are used.

**IP detection chain** (in order of preference):
1. **Manual IP**: configure `location_ip_address` in settings
2. **UPnP**: queries your local router (no traffic leaves LAN)
3. **Socket heuristic**: determines which interface routes externally (no data sent)
4. **OpenDNS DNS query**: single `myip.opendns.com` lookup to `208.67.222.222` (only external query)

If your ISP uses carrier-grade NAT (CGNAT), Jarvis automatically resolves your true public IP via the same OpenDNS DNS query. This can be disabled:

```json
{
  "location_cgnat_resolve_public_ip": false
}
```

**Setup:** Register for a free [MaxMind GeoLite2](https://www.maxmind.com/en/geolite2/signup) account, download the City database (MMDB format), and save it to `~/.local/share/jarvis/geoip/GeoLite2-City.mmdb`. The setup wizard will guide you through this.

</details>

<details>
<summary><strong>MCP Tool Integration</strong></summary>

Connect Jarvis to external tools via [MCP servers](https://github.com/topics/mcp-server):

```json
{
  "mcps": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "your-token" }
    }
  }
}
```

**Popular integrations:**
- **Home Assistant** - Voice control for smart home
- **Google Workspace** - Gmail, Calendar, Drive, Docs
- **GitHub** - Issues, PRs, workflows
- **Notion** - Knowledge management
- **Slack/Discord** - Team communication
- **Databases** - MySQL, PostgreSQL, MongoDB
- **Composio** - 500+ apps in one integration

See [full MCP setup guide](#mcp-integrations) below.

</details>

## MCP Integrations

> **Session persistence:** each MCP server is launched once and its stdio session is kept open across tool calls. Stateful servers (e.g. browser automation, where the server owns a long-running Chrome process) work correctly. If you have a server you'd rather not keep resident, set `"idle_timeout_sec": 300` on its config entry and Jarvis will free it after that long without activity. If a server's tools legitimately run long (e.g. delegating a task to an external CLI agent), set `"timeout_sec": 600` to raise its 120-second default call timeout.

<details>
<summary><strong>Home Assistant</strong> - Smart home voice control</summary>

1. Add MCP Server integration in Home Assistant (Settings → Devices & services)
2. Expose entities you want to control (Settings → Voice assistants → Exposed entities)
3. Create Long-lived Access Token (Profile → Security → Create token)
4. Install proxy: `uv tool install git+https://github.com/sparfenyuk/mcp-proxy`
5. Add to config:
```json
{
  "mcps": {
    "home_assistant": {
      "command": "mcp-proxy",
      "args": ["http://localhost:8123/mcp_server/sse"],
      "env": { "API_ACCESS_TOKEN": "YOUR_TOKEN" }
    }
  }
}
```

"Jarvis, turn on the living room lights" / "set bedroom to 72°" / "run good night scene"

</details>

<details>
<summary><strong>Google Workspace</strong> - Gmail, Calendar, Drive, Docs, Sheets</summary>

```json
{
  "mcps": {
    "google_workspace": {
      "command": "npx",
      "args": ["-y", "google-workspace-mcp"],
      "env": {
        "GOOGLE_CLIENT_ID": "your-client-id",
        "GOOGLE_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```
Setup: [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)

</details>

<details>
<summary><strong>GitHub</strong> - Repos, issues, PRs, workflows</summary>

```json
{
  "mcps": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "your-token" }
    }
  }
}
```

</details>

<details>
<summary><strong>Notion, Slack, Discord, Databases</strong></summary>

**Notion:**
```json
{ "mcps": { "notion": { "command": "npx", "args": ["-y", "@makenotion/mcp-server-notion"], "env": { "NOTION_API_KEY": "your-token" } } } }
```

**Slack:**
```json
{ "mcps": { "slack": { "command": "npx", "args": ["-y", "slack-mcp-server"], "env": { "SLACK_BOT_TOKEN": "xoxb-...", "SLACK_USER_TOKEN": "xoxp-..." } } } }
```

**Discord:**
```json
{ "mcps": { "discord": { "command": "npx", "args": ["-y", "discord-mcp-server"], "env": { "DISCORD_BOT_TOKEN": "your-token" } } } }
```

**Databases:** [bytebase/dbhub](https://github.com/bytebase/dbhub) (SQL), [mongodb-mcp-server](https://github.com/mongodb-js/mongodb-mcp-server) (MongoDB)

</details>

<details>
<summary><strong>Composio</strong> - 500+ apps in one integration</summary>

```json
{
  "mcps": {
    "composio": {
      "command": "npx",
      "args": ["-y", "@composiohq/rube"],
      "env": { "COMPOSIO_API_KEY": "your-key" }
    }
  }
}
```
Get API key at [composio.dev](https://composio.dev)

</details>

## Troubleshooting

**Warmup passes but intent detection times out?** Warmup checks model loading with a small request, not a full intent decision. Intent detection has its own `intent_judge_timeout_sec` (6 seconds by default), separate from chat. A timeout does not necessarily mean your server is offline; the log shows the configured limit.

**Model downloads look paused?** Open **Logs** from the tray. The download card shows real transferred bytes, percentage, speed and estimated time remaining when available, without flooding the activity timeline. If updates pause, it shows how long it has been waiting. Whisper's first download can be large; loading and warming up are shown separately after its files are ready.

<details>
<summary><strong>Common issues</strong></summary>

**First startup takes a bit** - Initial model downloads can take several minutes, followed by loading and warmup. Watch the Logs window for transfer progress and readiness. Enable **Low Power Mode** in Settings to skip LLM startup warmup.

**Jarvis doesn't hear me** - Check microphone permissions and the selected input in Settings. Say "Jarvis" anywhere in your sentence.

**Linux says Listening but nothing is transcribed** - Open Logs and check for capture warnings. Missing callbacks mean the recording stream is not delivering blocks; silent samples mean blocks arrive but contain no usable signal. Check input mute and the recording source in PipeWire/PulseAudio (`pavucontrol` or `wpctl status`), and choose a microphone rather than an output monitor. Try the system default or the `pipewire` input where available. Enable `voice_debug` in Settings for periodic callback counts, signal peak, speech-frame counts and capture rate. These diagnostics do not save microphone audio. Native 44.1/48 kHz inputs are resampled for speech detection and transcription.

**Not sure what is running** - Open the tray menu and click **Runtime Status**. It shows whether Jarvis is listening, whether Low Power Mode is active, whether Ollama is needed/running, which models are configured, and how many MCP servers are enabled.

**Responses are slow** - Try a smaller model and check available memory. See [hardware and model choices](../README.md#quick-install); requirements depend on model size, quantisation and context length.

**Mac gets warm while Jarvis is active** - Enable **Settings → Features → Low Power Mode**. This keeps voice recognition ready while avoiding background LLM warmup and shortening Ollama's idle residency window.

**Mac is still warm after quitting** - If Jarvis starts Ollama for you, quitting Jarvis also stops that owned Ollama runtime. If Ollama was already running before Jarvis opened, Jarvis leaves it running so it does not interrupt your other local AI tools.

**Windows: App won't start** - Extract full zip first, check Windows Defender

**macOS: "App can't be opened"** - Right-click → Open, or System Settings → Privacy & Security → Allow

**Linux: No tray icon** - `sudo apt install libayatana-appindicator3-1`

**Jarvis keeps deflecting on questions it answered before** - small models can record their own past failures into the diary, which then primes future sessions to repeat them. New writes are scrubbed automatically; to clean historical entries, open the Memory Viewer, switch to the Diary tab, and click **Clean up deflection narration** in the sidebar Maintenance section. Only sentences that narrate the assistant's failures are removed; the rest of each entry stays.

</details>

## Routines

A routine is a named chain of things Jarvis can already do, run in one request: "movie mode" might switch the TV to its console input, set the PC volume to 30 % and open Apple Music. It works by voice or text, in every reply mode.

- **Create one by doing it once.** Do the steps, then say "save that as movie mode". Jarvis reads every step back and saves nothing until you say yes. To save only some of them, ask what it did recently (`routineControl recent` lists them by number) and name the ones you want, in the order to run them.
- **Run it** with "start movie mode", "run my movie mode routine" or, after the wake word, just "Jarvis, movie mode". These are recognised without a model when every step is a routine action. A routine with a step that needs confirmation (deleting a file, for example) asks once, naming that step, before anything runs; approving it allows that step for that one run only.
- **Change it** with "delete my movie mode routine" or "rename movie mode to cinema". Every change asks first, and a change is refused when the same request has just read a web page, a file, the screen or another app (ask again on its own).
- Steps run in order, each through the same safety checks as when you ask for it alone. A failed step is reported and the next one still runs. One routine runs at a time, for at most two minutes, and a stop (spoken, the chat Stop button or a click on the orb) ends it between steps.
- In Codex or Claude mode a routine may use only the tools that mode offers, so a step such as switching the reply mode stops the routine before anything runs. Results sent to the model carry step labels and outcomes, never step arguments.
- Routines live in `config.json` under `routines` and can also be edited there. Each step names a tool and its arguments (at most 20 steps); `label` is optional and is what results and read-backs show. A routine that is not well formed is ignored and left untouched in the file. Changes made through Jarvis take effect at once; restart Jarvis after editing the file by hand.

```json
{
  "routines": {
    "movie mode": {
      "aliases": ["film night"],
      "steps": [
        {"tool": "tvControl", "args": {"action": "launch", "app": "HDMI 1"}, "label": "TV to the console"},
        {"tool": "systemVolume", "args": {"action": "set", "percent": 30}},
        {"tool": "appControl", "args": {"action": "open", "target": "Apple Music"}}
      ]
    }
  }
}
```

Spec: `src/jarvis/routines/routines.spec.md`.

## Local extensions

Your own code can extend Jarvis from `extensions/<name>/` without changing Jarvis's files (see [extensions/README.md](../extensions/README.md)). Nothing loads until you name it:

```json
{ "extensions_enabled": ["my_extension"], "extensions_dir": "" }
```

- `extensions_enabled` lists the folders to load, in order. `extensions_dir` is the folder holding them; empty means `extensions/` in the Jarvis checkout.
- Start-up prints `🧩 Extensions: ...` for those that loaded. One that fails prints a single `🧩 Extension <name> not loaded: <reason>` line and Jarvis carries on without it.
- An extension's settings are ordinary `config.json` keys, shown on its own page at the end of **Settings** while it is enabled. Restart Jarvis after changing them.
- Its tools go through the same safety checks and confirmations as built-in tools, and are offered to Codex and Claude like built-in tools (except ones it marks as personal data, which follow the long-term memory sharing switch).

Spec: `src/jarvis/extensions/extensions.spec.md`.

## Fast local commands

`fast_commands_enabled` (default `true`) lets confident English time/date and
Windows app, window, volume, media, system-usage and common-folder commands run
without a model round trip. Voice also skips the intent judge and collection
wait. Ambiguous and compound requests keep the conversational path.

`fast_commands_locales` defaults to `["en"]`; English is the supplied phrase
table. Other detected languages fall through. Set `fast_commands_enabled` to
`false` to use conversational routing for every request. These keys can be set
in `config.json` without changing model settings.

## Reply modes

Jarvis answers with the local model unless you allow a cloud mode. `codex_enabled` and `claude_enabled` (both `false`) allow ChatGPT through Codex and Claude through Claude Code; `reply_mode` (`local`, `codex` or `claude`) is the mode Jarvis starts in, and a cloud mode that is not allowed starts as local.

```json
{
  "claude_enabled": true,
  "reply_mode": "claude",
  "claude_model": "sonnet",
  "claude_effort": "low"
}
```

Switch at runtime by saying "use Claude", "use ChatGPT" or "go local", or from the tray's **Reply Mode** menu; the choice is written back to `reply_mode`. Each mode has its own sharing switches and bounds (`codex_*`, `claude_*`: recent conversation, Jarvis's records of what it acted on (`_share_desktop_referents`), the window you're looking at (`_share_foreground_window`, never sent for phone requests), personal-data tools, deadline, queue and tool calls), all on its Settings page. Each page picks the mode's model (`codex_model`, `claude_model`) and effort from a dropdown; a model ID set by hand in `config.json` is shown there and kept. See the README for what each mode sends and how it signs in.

## Activity log

An opt-in record of the foreground application, a redacted window title and idle time, so you can ask "what was I working on yesterday afternoon?". It is off by default and stays in the Jarvis database. Nothing is recorded, hooked or stored until you turn it on; restart Jarvis afterwards.

```json
{
  "activity_log_enabled": true,
  "activity_log_retention_days": 30,
  "activity_log_idle_after_sec": 300,
  "activity_log_excluded_processes": ["1Password", "Bitwarden", "KeePass", "Notepad"],
  "activity_log_share_with_cloud": false
}
```

- `activity_log_excluded_processes` and `activity_log_private_title_markers` replace the built-in lists (password managers; Incognito, InPrivate and Private Browsing in many languages). An excluded application is not recorded at all, not even that it was used.
- `activity_log_paused` is set by the tray's **Pause Activity Log**.
- `activity_log_share_with_cloud` (default `false`) decides whether the Codex and Claude reply modes are offered the `activityLog` tool. The local model always has it while the log is on.
- Delete the history from the tray (**Delete Activity History…**). Sessions older than `activity_log_retention_days` are pruned once a day.

Details: [`src/jarvis/memory/activity_log.spec.md`](../src/jarvis/memory/activity_log.spec.md).

## Phone access

An opt-in web app the daemon serves to paired phones: the orb, the shared conversation, quick actions and desktop confirmations. It is off by default; restart Jarvis after turning it on.

```json
{
  "remote_access_enabled": true,
  "remote_access_host": "0.0.0.0",
  "remote_access_port": 8765,
  "remote_access_allow_confirm": true,
  "remote_access_quick_actions": ["Pause the music", "Next song", "Volume up", "Volume down"]
}
```

- `remote_access_host` is the address to listen on. `0.0.0.0` listens on every network; only loopback, private home ranges, link-local, the VPN shared range (`100.64.0.0/10`, used by Tailscale) and IPv6 unique-local addresses are ever answered. Set this PC's Tailscale address to answer on the VPN only.
- `remote_access_port` must be from 1024 to 65535. If it is taken, Jarvis prints a warning and runs without phone access.
- `remote_access_allow_confirm` lets a paired phone approve or deny the desktop confirmation dialog.
- `remote_access_quick_actions` are sent as typed requests; an empty list hides the buttons.
- Pair phones from the tray (**Phone Access**) or with `python -m jarvis.remote pair`. Paired phones are kept in `remote_devices.json` next to the database, as hashed keys.

Details: [`src/jarvis/remote/remote.spec.md`](../src/jarvis/remote/remote.spec.md).

## Web chat

An opt-in chat window with projects and a model picker, served by the daemon on the loopback interface only. It is off by default; restart Jarvis after turning it on. The tray's **Chat** opens it instead of the classic chat while it is on.

```json
{
  "web_chat_enabled": true,
  "web_chat_port": 8766
}
```

- `web_chat_port` must be from 1024 to 65535 and differ from `remote_access_port`. If it is taken, Jarvis prints a warning and runs without the web chat.
- Chats, projects and messages (redacted text) live in the Jarvis database file, in the tables `chat_projects`, `chats`, `chat_messages` and `chat_state`. Delete one chat from its menu in the chat.
- The model picker lists the reply modes allowed by `codex_enabled` and `claude_enabled`, and the chat models Jarvis offers that Ollama has installed. Choosing a local model saves `ollama_chat_model`; choosing a reply mode saves `reply_mode`, as the tray does.
- Voice goes into whichever chat is open.

Details: [`src/jarvis/webchat/webchat.spec.md`](../src/jarvis/webchat/webchat.spec.md).

## Model context and timeouts

`llm_num_ctx` sets a stable Ollama context window for all inference requests,
including startup probes. Its default is 8192. Increase it if your model needs
more room for long conversations or tool results, allowing for extra memory use.

`llm_keep_alive` sets how long Ollama keeps a model loaded after each request
(**Settings → LLM & AI Models → Model Residency**). Use a duration with a unit
such as `"30m"` (the default) or `"2h"`, or whole seconds: `-1` keeps models
loaded until Ollama stops, `0` unloads them straight away. Longer residency
avoids a reload delay on the next request and holds GPU memory meanwhile.
Low Power Mode overrides it with 1 minute.

| Setting | Default | Applies to |
|---------|---------|------------|
| `llm_routing_timeout_sec` | 8 s | Tool routing, memory search extraction, tool search and inference warmup |
| `llm_chat_timeout_sec` | 45 s | Chat generation |
| `llm_tools_timeout_sec` | 300 s | Actual tool execution, independently of routing |

Explicit overrides are honoured. For existing configurations with an explicit
`llm_tools_timeout_sec` and no routing setting, the routing budget inherits that
value. Set `llm_routing_timeout_sec` separately to choose a shorter routing limit.
Intent judgement, planning and digest passes have their own timeout settings.

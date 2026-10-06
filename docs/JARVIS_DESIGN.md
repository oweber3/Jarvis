# Jarvis Architecture and Design (Windows Desktop)

Design for turning this fork into a fast, native Windows desktop assistant while keeping the existing local-first Jarvis behaviour intact. This is a guide for implementation, not a line-level specification. Behaviour that is already specified lives in the `*.spec.md` files next to the code; this document only says where new work attaches and in what order to build it.

Findings below come from reading the code at commit `83143fe` and from timing the real Ollama calls on a Windows 11 desktop with an NVIDIA GPU (Ollama 0.32.15, `qwen3.5:0.8b` for both the fast and chat tiers, `medium.en` Whisper on CUDA). Probe scripts were run out of tree; nothing in `src/` was changed to produce them.

---

## 1. Summary

| # | Question | Answer |
|---|----------|--------|
| 1 | Why does "What time is it?" reach the 6 s intent timeout? | The intent judge is not slow, it is **cold**. Ollama reloads a model whenever the request's `num_ctx` differs from the loaded runner's. The warm-up uses Ollama's default context, the judge asks for 8192, the tool router asks for 4096, the planner asks for 8192. Each switch costs about 3 s here, and more when Whisper is competing for the same GPU. A warm judge call takes 0.2 to 0.3 s. See section 3. |
| 2 | Where do native Windows tools attach? | As ordinary builtin `Tool` subclasses registered in `BUILTIN_TOOLS` (`tools/registry.py`), on top of a small Windows-only platform layer (`src/jarvis/platform/windows/`) that knows nothing about tools or the LLM. See section 5.2. |
| 3 | How should the deterministic fast path work? | A pure matcher (`src/jarvis/fastpath/`) that either maps the whole utterance to exactly one routine tool call or returns `None`. It is called from two places: the top of `run_reply_engine` (covers text chat and every other caller) and the listener before the intent judge (skips the judge and the collection window). It executes through `run_tool_with_retries`, so it shares validation and safety with the LLM path. See section 5.1. |
| 4 | How does a provider abstraction fit? | Most of it already exists (`jarvis/llm`: `LLMBackend`, `get_llm_backend`, `Tier`). The remaining gaps are Ollama-specific options leaking into call sites and a single global provider. Both are fixed by making the backend own its load-affecting options (needed for the latency fix anyway) and then adding per-tier targets. No rewrite. See section 5.4. |

The decisions that shaped the design (templated fast-command replies, judge and collection bypass, timeout split, `llm_num_ctx`, central confirmation, phasing) are recorded in section 9. One architectural question is still open there.

---

## 2. Current request flow

Voice path, with the module that owns each stage and the budget that applies. Capture, VAD and intent handling run on the **listener thread** (`VoiceListener._run`), Whisper on its FIFO worker, and reply generation plus TTS hand-off on a serial **reply worker**, so the microphone loop keeps consuming frames (and hears "stop") while a reply is being produced.

```
 mic ──► VAD/utterance assembly ──► Whisper worker (FIFO) ──► _process_transcript
                                                                   │
                       echo checks, wake-word text match, early face state
                                                                   ▼
                                                        IntentJudge.judge()          FAST tier
                                                                   │ directed + query
                                                                   ▼
                                       state_manager.start_collection(query)
                             wait voice_collect_seconds of silence after end of speech
                                                                   ▼
                                                        _dispatch_query()
                                                                   ▼
                                                     run_reply_engine()   (query_lock)
                          redact ► recent dialogue ► MCP cache ► tool router ► planner
                          ► memory enrichment (gated) ► warm profile ► system prompt
                          ► agentic loop: direct-exec plan step │ chat(tools)
                                                                   │              │
                                                                   ▼              ▼
                                             run_tool_with_retries ──► Tool.run / MCP
                                                                   ▼
                                                  reply text returned to listener
                                                                   ▼
                                       tts.speak(reply) ──► hot window on completion
```

| Stage | Module | Model and options | Budget |
|-------|--------|-------------------|--------|
| Capture, VAD | `listening/listener.py` (`_on_audio`, `_run`) | none | n/a |
| Transcription | `listener.py` (`_transcribe_audio`, FIFO worker) | faster-whisper, `medium.en`, CUDA | none |
| Wake and echo | `listener.py:595` (`_process_transcript`), `wake_detection.py`, `echo_detection.py` | none (text, rapidfuzz) | instant |
| Intent judge | `listening/intent_judge.py` | FAST tier, `num_ctx` 8192, `think` false, `max_tokens` 1500, about 2.2k prompt tokens | `intent_judge_timeout_sec` 6 s |
| Collection window | `listening/state_manager.py` | none | `voice_collect_seconds` (default 1.0 s of silence after end of speech; `voice_wake_wait_seconds` 4.5 s after a bare wake word; held open while speech is pending) |
| Dispatch | `listener.py:1276` (`_dispatch_query`) | none | n/a |
| Tool router | `tools/selection.py` (`_select_llm`), called at `reply/engine.py:924` | FAST tier via `direct()`, `num_ctx` 4096 (default), `max_tokens` 50 | `llm_tools_timeout_sec` (config default 300 s) |
| Planner | `reply/planner.py:469` | CHAT tier via `direct()`, `num_ctx` 8192, `max_tokens` 150 | `planner_timeout_sec` 3 s |
| Memory extractor (gated) | `reply/enrichment.py` | FAST tier, `direct()`, `num_ctx` 4096 | `llm_tools_timeout_sec` |
| Agentic loop | `reply/engine.py:1824` | CHAT tier via `chat()`, `num_ctx` 8192 | `llm_chat_timeout_sec` (config default 180 s), up to `agentic_max_turns` |
| Tool execution | `tools/registry.py:309` (`run_tool_with_retries`) | none | per tool |
| TTS | `output/tts.py` (Piper or Chatterbox), driven from `_dispatch_query` | n/a | n/a |

Notes that matter for the design:

- **TTS is the listener's job.** The voice path calls `run_reply_engine(..., tts=None, ...)` (`listener.py:1308`); the engine returns text and `_dispatch_query` speaks it and arms the hot window (`listener.py:1328-1344`). The text chat path (`daemon.submit_text_query`) calls the same engine with `tts=None` and never speaks.
- **Voice and text share one entry point and one lock.** `run_reply_engine` plus `daemon.query_lock()` is the only place a reply is produced, which makes it the natural home for anything that must apply to both.
- **Tools are synchronous and return raw text.** `Tool.run(args, ctx) -> ToolExecutionResult(success, reply_text, error_message)` (`tools/base.py`, `tools/types.py`). Built-ins live in the static `BUILTIN_TOOLS` dict; MCP tools are dispatched by `server__tool` name through `MCPClient` and the persistent runtime (`tools/external/mcp_runtime.py`).
- **Tool selection is itself an LLM call.** The router (FAST tier) narrows the catalogue, the planner (CHAT tier) turns that into steps, and `resolve_next_tool_call` direct-executes tool steps for small models. The chat model is only reached after the plan's tool steps finish.
- **Config path.** `load_settings()` (`config.py:695`) reads `~/.config/jarvis/config.json` (or `JARVIS_CONFIG_PATH`), merges over `get_default_config()` (`config.py:503`), and builds the frozen `Settings` dataclass. Only non-default values are written back by the settings window (`desktop_app/settings_window.py`, metadata-driven via `FieldMeta` / `FIELD_METADATA`).
- **Desktop app.** The tray app is a separate package; the daemon runs either inside the app (bundled `QThread`) or as a subprocess. `jarvis` core only reaches into `desktop_app.face_widget` through guarded imports for face state. New core code must keep that direction of dependency.
- **Existing Windows-specific code is minimal.** `ctypes` clipboard paste in `dictation/dictation_engine.py`, VRAM probing in `utils/vram.py`, CUDA DLL path setup in `listener.py`, and a `pynput` global hotkey. Installed libraries relevant to the new work: `psutil`, `pynput`, `rapidfuzz`. Not installed: any COM, UI Automation or audio-endpoint library.

---

## 3. Latency problem

### 3.1 What was measured

Timings below are wall-clock for single requests built with the project's own prompt builders against the local Ollama.

Cold start and context switching (judge-shaped request):

| Request | Wall | Load time | Note |
|---------|------|-----------|------|
| `OllamaBackend.warm_up` equivalent | 5.0 s | n/a | First load. Leaves the runner at Ollama's default context (32768 here). |
| Judge, `num_ctx` 8192 | **3.45 s** | 3.07 s | Reload because 8192 differs from the warmed 32768. |
| Judge again | 0.23 s | 0.004 s | This is what a warm judge costs. |
| Same request, `num_ctx` 4096 | **3.44 s** | 3.06 s | Reload. |
| Same request, `num_ctx` 8192 again | **3.44 s** | 3.07 s | Reload again. |
| Judge with `think` true | 2.98 s | 0.004 s | 800 generated tokens. Off by default; stays off. |

Front half of one turn for "what time is it", using the real judge, router and planner code:

| Stage | As shipped, turn 1 | As shipped, turn 2 | One shared `num_ctx`, turn 1 | One shared `num_ctx`, turn 2 |
|-------|-------------------|--------------------|------------------------------|------------------------------|
| Intent judge (8192) | 3.45 s | 0.34 s | 3.58 s | 0.31 s |
| Tool router (4096) | 3.18 s | 2.37 s | 0.09 s | 0.06 s |
| Planner (8192, timeout 3.0 s) | 3.01 s, returned `[]` | 3.03 s, returned `[]` | 0.24 s, produced a plan | 0.16 s, produced a plan |
| Main chat (8192) | 0.24 s | 0.17 s | 0.16 s | 0.20 s |

The "one shared `num_ctx`" columns come from forcing every `direct()` call to 8192 inside the probe only. Everything after the first load drops to 0.3 s or less.

### 3.2 Causes, in order of cost

1. **`num_ctx` differs between contexts that share one model.** The judge sends 8192 (`intent_judge.py:468`), the router and enrichment use `direct()`'s default of 4096 (`llm/ollama.py:98`), the planner sends 8192 (`planner.py:479,767`), `chat()` hard-codes 8192 (`llm/ollama.py:256`). On this machine both tiers resolve to the same Ollama model, so every alternation reloads it (about 3 s). A single "what time is it" turn can pay two or three reloads before the chat model runs.
2. **The warm-up does not warm the model the way it is used.** `OllamaBackend.warm_up` (`llm/ollama.py:360`) sends no `num_ctx`, no `think` flag and a one-token reply. The first real judge call therefore always reloads. The listener's startup message ("Intent judge ready") reports that probe, not readiness for a real request.
3. **The planner timeout equals the reload cost.** `planner_timeout_sec` is 3.0 s and a reload takes about 3.0 s, so after a router call at a different context the planner always times out, returns `[]`, and the engine falls open to the legacy path. The planner currently contributes 3 s of latency and no plan.
4. **The judge sits on the critical path for every wake-word utterance, and is followed by a fixed collection window.** After the judge accepts, `start_collection` waits `voice_collect_seconds` (4.5 s default) of silence before `_dispatch_query` runs (`state_manager.py:149`). That floor applies to every command regardless of how simple it is.
5. **`keep_alive` is only set by the judge and warm-up.** Other calls omit it, so each router, planner or chat request resets Ollama's residency to its own default instead of the 30 minute (or low-power 1 minute) policy.
6. **Router and chat timeouts are effectively unbounded.** `llm_tools_timeout_sec` defaults to 300 s and `llm_chat_timeout_sec` to 180 s in `get_default_config`, while the `getattr` fallbacks in the engine and the figures in `docs/llm_contexts.md` assume 8 s and 45 s. A stuck router blocks the listener thread, and with it VAD, Whisper handoff and stop-command handling, for minutes.
7. **Small-model quality compounds the cost.** With `qwen3.5:0.8b` the router picked `fetchMeals`, `getWeather`, `getTime` for "what time is it" on a cold turn, and the planner emitted memory and weather steps for it. The chat model can answer the question from the `[Context: ...]` time line with no tool at all. This is the strongest argument for a deterministic path for obvious commands.

I did not reproduce a full 6 s judge timeout in isolation: without Whisper loaded, the reload measured 3.1 to 3.5 s. The 6 s budget is reachable when the reload overlaps Whisper's CUDA use (both share the 12 GB GPU) or when the judge call arrives while an earlier request is still loading, because Ollama serialises loads per model. The mechanism (reload on option change) is confirmed; the exact overshoot on a loaded system is not measured.

### 3.3 Fix (Phase 3A)

- **One request shape per model.** The Ollama backend owns `num_ctx`, `keep_alive` and the `think` default. Call sites stop passing `num_ctx` (judge, planner x2, `direct()` default, `chat()` hard-code, `streaming()` hard-code). The value comes from one config key, `llm_num_ctx` (default 8192, large enough for the judge and planner prompts). Ownership sits with the backend and, once model targets exist (section 5.4), with the target, so a future FAST model and CHAT model can each have a different but stable context size without reload thrashing. Because this removes a parameter from the `LLMBackend` ABC and all its callers, it is done as one complete change, not a shim.
- **Warm-up uses the same options as live requests**, so "ready" means ready.
- **`keep_alive` applied centrally** by the backend from the power-mode setting, not per call site.
- **Separate the two timeout concepts.** `llm_tools_timeout_sec` is shared today by LLM routing calls (router in `engine.py:931`, memory extractor `engine.py:1200`, `toolSearchTool`, listener warm-up) and by tool execution (HTTP timeouts in `weather.py` and `time_tool.py`). Imposing 8 s on it would cut legitimate tool execution. Split it:
  - `llm_routing_timeout_sec` (new, default 8 s): router, memory extractor, tool search, warm-up budget.
  - `llm_tools_timeout_sec` (unchanged meaning): tool execution, with its existing default.
  - `llm_chat_timeout_sec`: default 45 s.
  - Explicit user values are preserved. A config migration seeds `llm_routing_timeout_sec` from an explicit `llm_tools_timeout_sec` in the user's file, so anyone who tuned the old key keeps their routing behaviour; only unset keys take the new defaults.
- **Rejected alternative:** omitting `num_ctx` everywhere and relying on the server default. It stops the thrash but lets Ollama pick 32768 on this machine, which inflates KV cache memory next to Whisper and a second model, and does nothing for OpenAI-compatible backends.

Expected result for a non-fast-path turn on this hardware: judge about 0.3 s, router about 0.1 s, planner about 0.2 s, chat about 0.2 s, once the model is resident.

---

## 4. Proposed component boundaries

```
src/jarvis/
  fastpath/                      NEW  deterministic command matcher + dispatcher (no OS imports)
    matcher.py                        text -> FastMatch | None, pure and side-effect free
    dispatcher.py                     execute a FastMatch through run_tool_with_retries
    phrases/en.json                   locale phrase tables (data, not code)
    fastpath.spec.md
  platform/                      NEW
    windows/                          OS functions only; no Tool, no LLM, lazy OS imports
      apps.py  windows_mgmt.py  audio.py  media.py  telemetry.py  files.py  input.py
      windows.spec.md
  tools/builtin/windows/         NEW  thin Tool adapters over platform.windows (win32 only)
  tools/registry.py              EDIT conditional registration, risk gate in run_tool_with_retries
  tools/base.py                  EDIT optional `risk` on Tool (default routine)
  tools/confirmation.py          NEW  pending-confirmation store for destructive actions
  reply/engine.py                EDIT fast-path hook after redact()
  listening/listener.py          EDIT fast-path hook before the intent judge
  llm/                           EDIT backend-owned options, then ModelTarget and per-tier backends
  config.py                      EDIT new settings
  desktop_app/settings_window.py EDIT FieldMeta entries
```

Rules of the boundaries:

- `fastpath` imports tools only through `run_tool_with_retries`; it never imports `platform.windows`.
- `platform.windows` imports nothing from `jarvis.tools`, `jarvis.reply` or `jarvis.llm`. It exposes plain functions and small Protocols so tests inject fakes and run on any OS.
- `tools/builtin/windows` is the only layer that knows both the OS functions and the `Tool` schema. It is the only place that turns OS results into the strings the LLM sees.
- Nothing in `jarvis` imports `desktop_app` for the new work. Settings are exposed through `FieldMeta` only.

---

## 5. Integration design

### 5.1 Deterministic fast-command path

**Contract.** `match(text, language) -> FastMatch | None`. A match is `(command_id, tool_name, args, reply_template)`. Matching is pure; execution is separate, so the listener can ask "is this a fast command?" without side effects.

**Matching rules (what makes it safe to fall through):**

1. Normalise: case-fold, strip punctuation, strip the wake word and the politeness fillers listed in the locale table.
2. The remaining text must be consumed **entirely** by exactly one command template (`open {app}`, `set volume to {percent}`, `pause the music`). Extra words, unknown verbs, or two commands in one utterance produce `None`. Compound requests, questions about the command, and anything conversational therefore go to the LLM unchanged.
3. Slot values are validated, not guessed: percentages parse to 0 to 100; app names resolve against the app index with `rapidfuzz` and a minimum score plus a margin over the runner-up. A low score or a close second candidate is `None`.
4. Only tools whose risk is routine are eligible (section 5.3). Destructive commands never match.
5. The target tool must be registered and available on this machine; otherwise `None`.
6. A disabled setting, an unsupported detected language (no phrase table), or an app index that is not ready yet all yield `None`.

**Where it runs.**

- **Engine hook (universal).** In `run_reply_engine`, immediately after `redact()` and before the dialogue and router work (`reply/engine.py`, near line 800). If it matches, execute, build the reply from the template, record the user and assistant turns in dialogue memory, and return. This covers text chat, evals and any future caller, and keeps voice and text behaviour identical. The existing tail of the function (print, TTS if any, memory update) should be reached through one shared helper rather than duplicated.
- **Listener hook (latency).** In `_process_transcript`, after the hot-window expiry check and before the judge gating (`listener.py` around line 794): when there is an engagement signal, take the text after the wake word (`extract_query_after_wake`, or the whole non-echo utterance in a hot window) and call `match`. On a match, skip the judge **and** the collection window and call `_dispatch_query(query)` directly. That reuses the existing face state, TTS and hot-window arming, so there is exactly one reply path. Anything that does not match continues unchanged into the judge.

**Replies.** High-confidence fast commands return a short deterministic acknowledgement or result with no LLM call: the locale template (for example "Opening Word.", "Volume set to 30%.") or, for read-only queries such as "what time is it", the tool's own result text. `reply.spec.md` keeps LLM-generated personality for conversational replies; deterministic system actions are explicitly exempt.

**Fall-through.** Ambiguous, compound or low-confidence utterances return `None` and continue through the unchanged judge, collection window and reply engine. A matched command on a finalised utterance skips both the intent judge and the collection window; `listening.spec.md` no longer describes the judge as the only decision-maker for finalised utterances.

**Language.** Command phrases are data in `fastpath/phrases/<lang>.json`, selected by Whisper's detected language, with only `en` shipped. No language patterns are embedded in code, and unsupported languages fall through to the LLM path. An embedding-based intent classifier (reusing `nomic-embed-text`) is a possible later replacement for the verb tables but still needs slot extraction, so it is not part of the first build.

**Dialogue state.** Fast-path turns are written to `DialogueMemory` like any other turn so the next utterance has context. Tool-carryover recording is skipped for routine OS actions.

### 5.2 Native Windows tools

**Granularity.** The router sees a one-line description per tool and a 0.8B router already misroutes, so the catalogue should stay small. Few coarse tools with an `action` parameter beat many narrow ones:

| Tool | Actions | Backing mechanism |
|------|---------|-------------------|
| `appControl` | open, close (graceful), focus, list | App index built from Start Menu shortcuts, App Paths, and UWP entries (`shell:AppsFolder`); launched with `os.startfile`. User aliases in config (for example "word", "matlab"). Graceful close via `WM_CLOSE`. |
| `windowControl` | minimise, maximise, restore, snap left or right, move to monitor, list, virtual desktops (switch, new, close, move a window) | `user32` through `ctypes` (`EnumWindows`, `ShowWindow`, `SetWindowPos`). Focus uses the `AttachThreadInput` workaround for foreground-lock restrictions. |
| `systemVolume` | get, set, up, down, mute, unmute | `pycaw` / `IAudioEndpointVolume` (COM). |
| `mediaControl` | play, pause, play_pause, next, previous, now_playing | Media virtual keys via `SendInput` for next and previous; the SMTC session manager for true play and pause and for "now playing". |
| `systemInfo` | cpu, ram, top_processes (by memory or CPU), disk, battery, gpu, network, summary | `psutil`; GPU through the existing `utils/vram.py` approach or `nvidia-smi`. |
| `openWebsite` | open one web page, optionally in a new browser window placed on a monitor and zone | `os.startfile` for the default browser; a Chromium browser with `--new-window`, then the workspace launcher's new-window detection and verified `SetWindowPos` placement. |
| `openPath` | open, reveal in Explorer, known folders (Documents, Downloads, Desktop), find a file or folder by name | `os.startfile`, `explorer /select`, `SHGetKnownFolderPath`. |
| `inputControl` | hotkey (key chord), clipboard_read, clipboard_write | `SendInput` for chords to the focused window, with destructive chords gated by confirmation; clipboard through `pywin32` behind a backend interface. Controlling other apps' controls by name is a separate UI Automation tool. |
| `systemSettings` | open_page, brightness, power_plan, audio_output, night_light, do_not_disturb | `ms-settings:` deep links; DDC/CI through `dxva2` (WMI for internal panels); `powercfg`; `IPolicyConfig` through `pycaw`. Night Light and Do Not Disturb open their settings page because Windows has no supported switch. |

File operations beyond opening paths stay with the existing `localFiles` tool; it is brought under the confirmation gate (section 5.3) rather than duplicated.

**Registration.** `BUILTIN_TOOLS` is a static dict in `tools/registry.py`. Windows tools are added to it only when `sys.platform == "win32"` and the setting is on; their adapter modules import `platform.windows` lazily so the test suite imports cleanly on any OS. They automatically join the router catalogue, planner, `toolSearchTool` and the LLM loop with no engine changes, and the fast path calls the same objects.

**Dependencies.** `pycaw` and `comtypes` for volume are the only new third-party packages proposed (marked `sys_platform == "win32"` in `requirements.txt`). Everything else uses `ctypes`, `psutil` or `pynput`, which are already present or in the standard library; the repo already prefers raw `ctypes` for Windows APIs (`utils/vram.py`). They are added in phase 2B, installed additively into `.mamba_env` (section 9).

**Latency of the app index.** Enumerating Start Menu shortcuts takes milliseconds; the UWP list via PowerShell takes about a second. The index is built on a background thread at daemon start and cached in memory. Until it is ready, `open {app}` falls through to the LLM path rather than blocking.

### 5.3 Confirmation and safety

AGENTS.md requires confirmation for destructive actions and none for routine local ones. There is currently no confirmation mechanism in the tool layer: `localFiles` can delete or overwrite files with no prompt and is reachable from the LLM path today. The design uses one central policy, not per-tool checks.

**Classification.** Every tool call is classified by a central safety layer into one of four outcomes:

| Outcome | Meaning | Examples |
|---------|---------|----------|
| `SAFE` | Execute directly. | Open an app, set volume, read system info, list files. |
| `CONFIRM_VOICE` | Short spoken yes or no. | Delete a normal file, overwrite an existing file, terminate a normal user process. |
| `CONFIRM_DIALOG` | Desktop confirmation dialog; voice alone cannot authorise. | Shutdown or restart, uninstall software, terminate critical or system processes, destructive operations on important or system locations, broad or bulk deletion. |
| `DENY` | Refused; never exposed through assistant control. | Actions that must not be reachable by voice or the LLM at all (for example formatting a drive). |

**Metadata, not logic, in tools.** A tool declares a classification hook on `Tool` (default `SAFE`, so every existing tool is unaffected) that receives the validated arguments and returns a `ConfirmationRequest`: the outcome, a plain-language description of the exact action, the target resource and any important consequence. The tool contains no prompting code. The policy layer owns thresholds such as what counts as a system location, a critical process or a bulk operation, so those definitions live in one place.

**Enforcement.** The gate sits in `run_tool_with_retries`, the single choke point for the LLM loop and the fast path. A non-`SAFE` call does not execute; it registers a pending action and returns a result telling the caller what is awaiting confirmation. `DENY` returns a refusal. Central heuristics (shutdown, uninstall, process termination, important locations) only raise a tool's declared tier, never lower it. Every mutating filesystem action (create, write, overwrite, append, delete) on a system, startup or sensitive user location needs the desktop dialog; reads and listings stay `SAFE`. The fast path never matches non-`SAFE` actions.

**Voice confirmation (`CONFIRM_VOICE`).**
- Jarvis states the exact action: "Delete report.pdf? Say yes or no."
- Only a reply addressed to Jarvis is a candidate answer: an utterance containing the wake word, or speech inside the hot window that opens when Jarvis finishes asking. Background speech (including a Whisper hallucination such as "okay") neither authorises nor cancels the request, which still expires on its own. Affirmative and negative phrases come from the locale phrase table (data per language, no language patterns in code); a language without a table cannot use voice confirmation and falls back to the dialog.
- A recognised yes claims the pending action atomically and runs it on a worker thread, reporting through the normal TTS path, so the listener never waits on the tool; a recognised no cancels it. Any unrelated utterance, or expiry after a short timeout, abandons it. Silence or an unclear reply is never approval.
- The confirmation phrase is handled by the confirmation layer before the intent judge and fast-command matcher, and only while a pending action exists.

**Dialog confirmation (`CONFIRM_DIALOG`).** The desktop app shows a non-blocking dialog (the requesting thread never waits for it; expiry, replacement or shutdown closes it) with the action, target, any important consequence, and Confirm and Cancel buttons. Voice cannot answer it. `jarvis` core stays independent of `desktop_app`: core publishes pending confirmations through a small callback registered by the desktop app (the same pattern as the existing diary and chat callbacks), and with no UI registered (headless) a `CONFIRM_DIALOG` action is refused rather than downgraded to voice. The dialog uses the shared theme in `themes.py`.

**Binding.** A pending confirmation is bound to a single exact action: tool name, normalised target or resource, the relevant parameters and an expiry time, identified by an unguessable id. Approval applies only to that action; a generic "yes" cannot approve a different or queued action, and changed parameters invalidate it. At most one voice confirmation is pending at a time; a new request replaces the old one. Confirmations originate only from the user (voice phrase or dialog button), never from tool arguments or model output, so the model cannot approve its own request. Executed and cancelled confirmations are consumed and cannot be replayed.

**Other safety notes.**
- Graceful window close is `SAFE` because the application can prompt to save; force-kill is not.
- `inputControl` is high risk by nature: off by default, foreground window only, cannot reach elevated windows (UIPI), typing requires confirmation.
- Privacy: window titles and process lists can contain personal text. The fast path never sends them off the machine. On the LLM path they pass through `redact()`, and `systemInfo` omits window titles unless asked.

---

### 5.4 Provider abstraction

What exists (`llm/llm.spec.md`): the `LLMBackend` ABC with `OllamaBackend` and `OpenAICompatibleBackend`, `get_llm_backend(cfg)` dispatching on `llm_provider`, `get_embedding_backend`, and the two-tier `Tier.FAST` / `Tier.CHAT` model resolution. Call sites already say which tier they run on, which is the hard part.

What is missing:

1. **Ollama knobs leak through the interface.** `direct(num_ctx=...)`, `extra_options={"keep_alive", "num_ctx", ...}` and `think` are Ollama concepts. Phase 3A removes the load-affecting ones from call sites; a typed options object (`max_tokens`, `temperature`, `thinking`) replaces the free-form `extra_options` dict.
2. **One provider for everything.** `resolve_model(cfg, tier)` returns only a name, and the provider is global. A user cannot run FAST on Ollama and CHAT on a remote OpenAI-compatible endpoint.
3. **No fallback or routing policy.** A failed request returns `None`; nothing can try a second target.

Proposed shape, in order, each step usable on its own:

- `resolve_target(cfg, tier) -> ModelTarget(provider, base_url, api_key, model)` next to `resolve_model`. Defaults come from the existing global settings; optional per-tier overrides (`fast_provider`, `fast_base_url`, and the same for chat) are unset by default so current behaviour is identical.
- `get_llm_backend(cfg, tier=None)` returns the backend for that tier's target. The sixteen `resolve_model` call sites across eleven modules (judge, listener warm-up, router and engine, planner, enrichment, evaluator, graph ops, tool search, weather, memory viewer, daemon) move together in one mechanical change; no half-converted state.
- A `RoutingBackend(LLMBackend)` that wraps an ordered list of targets with an explicit user-configured policy (for example local first, remote on timeout). It is only constructed when the user configures it, so the offline path stays the baseline, per AGENTS.md. Anthropic-compatible servers would be one more `LLMBackend` in the factory, not a change to callers.

Privacy rule for any remote target: tool results and dialogue go to a remote provider only when the user has explicitly configured that provider, and Windows tool output passes through `redact()` first.

### 5.5 Configuration and settings

New keys go through the existing path: `get_default_config()` and `Settings` / `load_settings()` in `config.py`, a `FieldMeta` in `desktop_app/settings_window.py` (new "Windows Control" category), and `docs/CONFIGURATION.md`. Only non-default values are written to the user's file, and unknown keys (including `mcps`) are preserved, so the existing `config.json` is untouched.

Initial keys: `llm_num_ctx`, `llm_routing_timeout_sec`, `fast_commands_enabled`, `fast_commands_locales`, `windows_tools_enabled`, `windows_app_aliases`, `windows_tools_confirm_destructive`. Per-tier provider keys arrive with provider abstraction (section 6, "Later").

---

## 6. Implementation order

Every phase follows the repo rules: failing test first, behaviour-level assertions (mechanisms, not hard-coded values), spec updated in the same change, a manual run, and a Conventional Commit. Evals run after any change that can affect agent accuracy. Phases 2A and 2B use the existing builtin-tool architecture only (new `Tool` subclasses plus conditional registration in `tools/registry.py`) and touch no routing, engine or listener code, so they can proceed in parallel.

| Phase | Scope | Tests and evidence |
|-------|-------|--------------------|
| **2A. Applications, windows, paths** | `platform/windows` apps, window management, path opening; `appControl`, `windowControl`, `openPath` tools (files, folders, URLs); win32-only registration; config keys (`windows_tools_enabled`, `windows_app_aliases`). | OS layer behind fakes so tests run on any OS, plus `win32`-only integration tests. Manual check on this machine ("open Word", "open MATLAB", snap a window, open Downloads). |
| **2B. Audio, media, telemetry** | `systemVolume` (adds `pycaw` and `comtypes`, Windows only, installed additively into `.mamba_env`), `mediaControl` (SMTC for true pause, media keys for next and previous), `systemInfo` (CPU, RAM, top processes, disk, battery, GPU). | Fakes for COM and `psutil`; real-device manual checks; pause must not start playback. |
| **2C. Central confirmation** | `SAFE` / `CONFIRM_VOICE` / `CONFIRM_DIALOG` / `DENY` policy and classification hook on `Tool`; gate in `run_tool_with_retries`; `tools/confirmation.py` (pending store, binding, expiry); voice yes/no handling from the locale phrase table; dialog callback and desktop modal; retrofit `localFiles` delete and overwrite; classify force-kill, shutdown, uninstall, bulk and system-location operations. | Destructive calls never execute without matching confirmation; a generic yes cannot approve another action; changed parameters invalidate; unrelated utterance and expiry cancel; voice cannot satisfy a dialog action; headless refuses dialog actions; tool arguments cannot confirm; `SAFE` tools unaffected. Desktop dialog covered in `test_desktop_app` style tests. |
| **3A. Context ownership and timeouts** | Backend (later target) owns `num_ctx`, `keep_alive`, `think` default; `llm_num_ctx` 8192; warm-up mirrors live requests; call sites drop `num_ctx`; `llm_routing_timeout_sec` split with migration; chat default 45 s; explicit overrides preserved. | Stub Ollama server recording request options across a simulated turn: every request to one model has identical load-affecting options and warm-up matches. Migration tests for explicit and unset values. Re-run the section 3 stage timings. Router, planner and intent-judge evals, because the planner now returns plans. Update `docs/llm_contexts.md`. |
| **3B. Fast-command router** | `fastpath` matcher, dispatcher and `en` phrase table; engine hook; listener hook that skips judge and collection window; templated replies. | Matcher properties: whole-utterance consumption; extra token, compound and ambiguity all fall through; destructive never matches. Listener tests: matched command dispatches with no judge call, unmatched reaches the judge, echo and hot-window handling unchanged. Before and after wake-to-action benchmark on this machine. Fast-versus-fall-through eval. New `fastpath.spec.md`. |
| **Later** | Settings window integration (new "Windows Control" category, `FieldMeta` entries); provider abstraction (`ModelTarget`, per-tier backends, typed generation options, optional `RoutingBackend`); `inputControl` on UI Automation, off by default; regression pass; documentation (README, `CONFIGURATION.md`); final review. | As in sections 5.4 and 7. |

Ordering rationale: 2A and 2B give the fast path real tools to target and are low risk (routine, local, read-mostly). 2C lands before anything destructive is added beyond what exists. 3A removes the measured latency for every path, and 3B then benchmarks against a warm baseline.

---

## 7. Risks and compatibility

- **Spec edits.** `reply.spec.md`, `listening.spec.md` and `llm.spec.md` carry the deterministic-action exemption, the judge and collection bypass, and backend-owned request options. The code in phases 3A and 3B must match them exactly.
- **Language rule.** AGENTS.md says to avoid hard-coded language patterns. Phrase tables as data plus fall-through for unsupported languages is the compromise; the fast path will not work in other languages until someone adds a table. Your Whisper model is `medium.en`, so this has no practical effect today.
- **Fast-path false positives.** The whole-utterance rule and the app-resolution margin are the defences. The listener hook must run only with an engagement signal (wake word or hot window) and after the existing echo checks, so TTS echo cannot trigger an action.
- **Media keys toggle.** `play_pause` is a toggle, so "pause the music" could start playback. True pause needs the SMTC session state, which is why `mediaControl` in phase 2B uses it rather than assuming.
- **Windows API limits.** Foreground-lock rules can make `focus` fail; non-elevated Jarvis cannot control elevated windows or send them input (UIPI); COM objects need per-thread initialisation because tools run on the listener thread and worker threads.
- **Reply worker blocking.** Reply generation and tool execution run on the serial reply worker, not the listener thread, so a stuck tool or COM call no longer freezes the microphone loop. It still delays later voice replies queued behind it; an engaged stop discards them.
- **Model quality.** `qwen3.5:0.8b` choosing `fetchMeals` and `getWeather` for "what time is it" shows it cannot be the sole semantic router for obvious desktop commands. The deterministic fast path handles those; ambiguous requests keep using the LLM path. It also misroutes and produces poor plans even when fast. The fast path removes the obvious commands from its hands, but the ambiguous remainder still depends on it. `qwen3:4b` is already installed and fits alongside Whisper in 12 GB; trying it as the CHAT tier is a config-only experiment worth running after phase 3A.
- **Ollama behaviour is version-dependent.** The reload-on-context-change behaviour was observed on 0.32.15. The stub-server test pins the intended request shape rather than the server's behaviour.
- **Packaging.** `jarvis_desktop.spec` will need hidden imports for `pycaw` and `comtypes` if the bundled app is rebuilt.
- **Cross-platform tests.** The upstream suite runs on other operating systems; Windows-only code stays behind lazy imports and `skipif` markers.
- **Existing exposure.** `localFiles` delete has no confirmation today. Phase 2C fixes that for the LLM path; until then the risk is unchanged.
- **Privacy.** No telemetry is added. The app index and process data stay local; the fast path never contacts a model, and remote providers only ever see data when explicitly configured.

---

## 8. Documentation and spec follow-ups

- New: `src/jarvis/fastpath/fastpath.spec.md`, `src/jarvis/platform/windows/windows.spec.md`, and registry rows for both in AGENTS.md.
- Update: `llm/llm.spec.md` (backend-owned options, targets), `listening/listening.spec.md` (fast-path hook, warm-up semantics), `reply/reply.spec.md` (fast-path branch), `reply/planner.spec.md` (no planner on fast-path turns), `tools/selection.spec.md` (new tools), `desktop_app/settings_window.spec.md` (new category), `docs/llm_contexts.md` (contexts no longer carry `num_ctx`; fast path is not an LLM context), `docs/CONFIGURATION.md`, README built-in tools and configuration sections.

---

## 9. Decisions

Settled:

1. Fast commands return short deterministic replies with no LLM call; conversational replies keep LLM personality.
2. A confidently matched fast command on a finalised utterance skips the intent judge and the collection window; everything else falls through unchanged.
3. `pycaw` and `comtypes` (Windows only) are added in phase 2B. The dependency guardrail forbids reinstalling working packages, not adding new ones.
4. Routing timeout 8 s and chat timeout 45 s by default, as separate concepts from tool-execution timeouts, with explicit user values preserved.
5. `llm_num_ctx` defaults to 8192, owned by the backend and later the model target.
6. One central confirmation policy for destructive actions: `SAFE`, `CONFIRM_VOICE`, `CONFIRM_DIALOG`, `DENY`; spoken yes or no for normal destructive actions, desktop dialog for high-risk ones, no command repetition, confirmations bound to the exact action with an expiry (section 5.3).
7. Phasing as in section 6.

Still open: none. The design decisions needed for Phase 1 are settled.

# Efficiency baseline: start-up, shutdown, idle and models

Measured on 4 November 2026 against `develop` at `70361d8`. Nothing in Jarvis was changed to take these numbers; the harness in [`efficiency_harness/`](efficiency_harness/) wraps the daemon from outside. Re-run it after each fix and compare against the tables here.

## Summary

| What | Owner profile (Codex replies) | Notes |
|------|-------------------------------|-------|
| Start-up to "Listening!" | **9.6 s** median (5 runs: 9.4, 9.4, 9.6, 9.6, 13.1; the slow run overlapped the sibling test run) | Kokoro and Whisper load one after the other on the critical path |
| Start-up to "Listening!", local replies | **12.6 s** median (3 runs) | 3 s slower **and nothing is warm**: the warm-ups time out and Ollama aborts the loads |
| Shutdown, nothing to save | **1.3 s** median (5 runs, 1.1 to 1.5 s) | Up to 1 s of it is the main loop noticing the stop |
| Shutdown after a conversation | **2.9 s** median (3 runs) | **47 s** with local replies: the diary summary on `gpt-oss:20b` |
| First reply in local mode | not applicable (a fast-path command answered in 0.13 s) | **70 to 85 s**: the first model call times out while `gpt-oss:20b` cold-loads |
| Idle CPU (whole process tree) | **3.1 % of one core** median over 5 min | About half of it is the wake-word sound model |
| Idle RAM (whole process tree) | **2.27 GB** | Kokoro's GPU helper process alone is 1.1 GB |
| Idle VRAM used by Jarvis | **about 6.0 GB** of 12 GB | Intent model 3.2 GB, Whisper 1.2 GB, Kokoro 0.8 GB |
| Desktop app idle CPU | **0.6 %** of one core, face window hidden; **10.7 %** with it shown | The orb's resting animation |

The ranked list of fixes is at the end: [Proposed fixes](#proposed-fixes-ranked-by-benefit).

## Machine and conditions

- 6-core / 12-thread desktop CPU (AMD Ryzen 5 7600X3D), 31 GB RAM, NVIDIA RTX 5070 with 12 GB VRAM, Windows 11 Home (build 26200).
- Python 3.12.14 from the project micromamba environment; faster-whisper 1.0.3 (CTranslate2 4.8.2), ONNX Runtime 1.30 (Kokoro's helper uses `onnxruntime-gpu` 1.31), PyQt6 6.8, Ollama serving from the same machine.
- Running alongside: a VirtualBox Home Assistant VM holding about 4 GB of RAM, and the usual desktop apps, which held 2.2 to 2.9 GB of VRAM before Jarvis started (this baseline is subtracted from every VRAM figure below).
- The live Jarvis was not running. Ollama was not running either; `ollama serve` was started for the measurements and stopped afterwards.
- A sibling session ran `pytest` against the same Ollama for part of the night (from 01:09). The first 5-minute idle run overlapped it and lost its Ollama models to evictions, so its VRAM figures are not used; the idle figures come from a second run after the tests had finished.

### Configuration profiles

All runs use a temporary copy of the owner's config and database (`JARVIS_CONFIG_PATH`); the real `config.json` and database were never written.

| Profile | What it is |
|---------|------------|
| **owner** | The owner's settings: Codex replies, Kokoro TTS on the graphics card, Whisper `medium` on CUDA, wake-word sound detection, dictation, web chat, one MCP server (`npx` YouTube transcript), `qwen3.5:4b` as the fast tier, `nomic-embed-text` embeddings. Turned off because they act on the outside world: the animatronic extension, gesture camera, Roku TV, phone access. |
| **local** | As owner, but local replies (`gpt-oss:20b` as the chat model). |
| **min** | As local, with TTS, MCP, web chat and cloud modes off (used only for the smoke-test split). |

### How the harness keeps it safe and headless

- `bench_daemon.py` replaces `sounddevice.InputStream` with a fake stream that delivers a faint noise floor (RMS 0.001, well under `voice_min_energy`) at real-time pace, and replaces every output stream with a silent sink. No microphone is opened and nothing is played. (Pure zeros were tried first and tripped the listener's "microphone is delivering silent samples" reconnect, which is not idle behaviour.)
- Every stdout line is stamped with seconds since process creation, which gives the start-up timeline for free.
- `run_bench.py` starts the harness the way the desktop app starts the daemon in source mode (`JARVIS_STDIN_IPC=1`, stdin pipe, no window), samples the whole process tree every 5 s with `psutil` and VRAM with `nvidia-smi`, and stops it the way the desktop app does: by closing stdin.
- Before each start-up run every Ollama model is unloaded, so start-up always pays the cold model load, as it does after a reboot.

## Start-up

### Timeline (owner profile, run `owner2`, typical of the 9.6 s median)

| t (s) | Event | Phase cost |
|------:|-------|-----------:|
| 0.0 | Process created | |
| 1.23 | `jarvis.daemon` imported | 1.2 s imports |
| 1.4 | Daemon started; MCP discovery and the Codex bridge start in the background | |
| 2.0 | TTS initialisation begins (blocks the main thread) | |
| 4.70 | Kokoro voice ready on the graphics card | **2.7 s Kokoro** |
| 4.7 | Voice listener thread starts; microphone permission probe (fake) | 0.2 s |
| 4.9 | LLM warm-ups start in the background (intent model, embeddings) | |
| 5.18 | Whisper `medium` load starts (after the CUDA library probe) | 0.3 s |
| 8.64 | Whisper loaded on CUDA, warm-up transcription done | **3.5 s Whisper** |
| 9.59 | Warm-up threads joined: "Listening!" | 0.95 s waiting on `qwen3.5:4b` |

The two big loads, Kokoro (2.3 to 2.7 s) and Whisper (3.3 to 3.8 s), run strictly one after the other: `tts.start()` finishes on the main thread before `VoiceListener` is created. MCP discovery (about 2.2 s for one `npx` server) and the Codex bridge (about 0.4 s) already run in the background and are off the critical path.

In the local profile the same timeline reaches "Listening!" at 12.6 s: Whisper is loaded at 8.2 to 8.4 s, then the listener waits for the chat and intent warm-ups, which **both fail** after about 8 s (see [Models](#models)).

### Smoke test (`--smoke-test`, 3 runs each, median)

| Profile | Imported | `SMOKE_TEST_INIT_OK` | Process exit | Peak tree RSS |
|---------|---------:|---------------------:|-------------:|--------------:|
| owner | 1.16 s | 4.44 s | 6.79 s | 3.10 GB |
| local | 1.15 s | 4.20 s | 6.58 s | 3.05 GB |
| min (no TTS) | 1.16 s | 1.44 s | 4.50 s | 1.80 GB |

The 3 s difference between owner and min is Kokoro. The smoke test prints `SMOKE_TEST_INIT_OK` before Whisper has loaded (the listener loads it on its own thread), so it is not a readiness measure; the 2 s between `INIT_OK` and exit is the smoke path's `voice_thread.join(timeout=2.0)` timing out while Whisper is still loading.

### Imports (`python -X importtime -c "import jarvis.daemon"`, 3 runs, median)

Total: **1.22 s**. Top cumulative entries:

| ms | Module | Note |
|---:|--------|------|
| 1201 | `jarvis.daemon` | everything below |
| 531 | `jarvis.listening.listener` | |
| 271 | `jarvis.tools.registry` | |
| 211 | `jarvis.memory.conversation` | |
| 188 | `jarvis.llm` | mostly `requests` |
| 178 | `rapidfuzz` | |
| 176 | `requests` | |
| 132 | `sounddevice` | |
| 131 | `webrtcvad` | 128 ms of it is `pkg_resources`, which `webrtcvad` imports for its version |
| 130 | `numpy` | |
| 108 | `jarvis.config` | |
| 99 | `jarvis.tools.builtin.windows.system_tools` | |
| 42 | `jarvis.tools.builtin.windows.media_control` | |
| 32 | `jarvis.tools.builtin.weather` | |

`faster_whisper`, `onnxruntime` and `torch` are not imported at module load (faster-whisper is imported on the listener thread when the model loads; `torch` is never imported by the daemon). Imports are a small share of start-up.

### Profile of the main thread (`cProfile`, owner smoke test)

| Cumulative | Function | Meaning |
|-----------:|----------|---------|
| 3.01 s | `output/tts.py: _ensure_initialized` | Kokoro start-up on the main thread (under the profiler) |
| 1.40 s | `output/kokoro_gpu.py: __init__` | spawning the GPU helper process and creating its session |
| 2.33 s | `output/kokoro.py: synthesize` (2 calls) | the warm-up synthesis inside `load_kokoro_voice` |
| 0.54 s | `platform/windows/apps.py: _discover_system_applications` | the Start-menu application index (on its own thread, `windows-app-index`) |
| 0.81 s | `jarvis/daemon.py` module import | |
| 1.90 s | `codex_bridge/app_server.py: _stop_process` | teardown at the end of the smoke test (see Shutdown) |
| 1.88 s | `tools/external/mcp_runtime.py: shutdown` | teardown |
| 1.85 s | `output/tts.py: stop` | teardown |

### Desktop app (offscreen, `QT_QPA_PLATFORM=offscreen`, daemon start and orphan clean-up patched out, 3 runs)

| Step | Time |
|------|-----:|
| `import desktop_app.app` | 0.30 to 0.33 s |
| `JarvisSystemTray()` construction (log viewer, memory viewer, face window, wake overlay, pointer ring, tray menu) | 0.47 to 0.62 s |
| Process start to tray ready | 0.9 to 1.1 s |
| RSS | 96 MB, 8 threads |

The desktop app is not a start-up bottleneck. (The real app also runs `cleanup_orphaned_processes`, which walks every process's command line; it was patched out here so the benchmark could never stop another process.)

## Shutdown

The desktop app (source mode) stops the daemon by closing its stdin; the bundled app calls `request_stop()`. Both end in the daemon's `finally` block.

### Nothing to save (owner and local, 5 runs)

| Step | Time after stdin closes |
|------|------------------------:|
| Main loop notices the stop (`time.sleep(1.0)` loop) | 0.5 to 0.95 s |
| Phone access, scheduler, web chat, gestures, dictation stop; voice thread joined | about 0.25 s |
| Diary pass (0 pending chunks), TTS stop, MCP runtime, reply bridge, database | about 0.05 s |
| Interpreter exit | about 0.3 s |
| **Total** | **1.1 to 1.5 s** (median 1.3 s) |

When nothing is mid-start, every teardown step is quick; the largest single item is the one-second polling loop.

### Stopping while things are still starting

The smoke test stops Jarvis 4 to 5 s after launch, while Whisper, the Kokoro warm-up and the Codex spare session are still in flight. Teardown then took 2 s from `INIT_OK` to return, and under the profiler the steps ran **one after another**: Codex app-server stop 1.9 s, MCP runtime 1.9 s, TTS stop 1.85 s, plus the voice thread's 2 s join timeout. The desktop app's Restart and Quit during start-up hit the same path.

### After a conversation (local replies, diary summary on shutdown)

The pending conversation lives only in memory, so the daemon summarises it into the diary before it exits (`SHUTDOWN_DIARY_TIMEOUT_SEC` = 45 s bounds the pass). The harness sent one chat message through the desktop's stdin channel 30 s after "Listening!", waited for the reply, then closed stdin. 3 runs per profile.

| Profile | Diary model | Message | Shutdown | Of which the diary |
|---------|-------------|---------|---------:|-------------------:|
| owner (Codex replies) | `qwen3.5:4b` (fast tier, already warm) | "What time is it?" (answered by the fast path, no cloud) | **2.9 s** median (2.1, 2.9, 2.9) | 1.4 to 1.8 s |
| local | `gpt-oss:20b` (chat model) | "Tell me one short fun fact about octopuses." | **47.2 s** median (46.6, 47.2, 52.7) | **45.0 to 50.8 s** for a two-line exchange |

In local mode the diary pass takes about as long as its 45 s budget allows, for two lines of conversation: `gpt-oss:20b` is a reasoning model that does not fit in the free VRAM (see [Models](#models)), so every call runs partly on the CPU. The desktop app shows its diary dialog for this whole time.

The same runs also measured the **first local reply: 70 to 85 s**. The first model call (tools offered, on `gpt-oss:20b`) hit the 45 s `llm_chat_timeout_sec` while the model was still cold-loading, and the engine then retried without tools, which took another 25 to 40 s. In Codex mode the same message was answered by the fast path in 0.13 s.

## Idle

### Daemon, owner profile, 5 minutes after start-up settled (60 samples)

| Metric | Median | Range |
|--------|-------:|------:|
| CPU, whole process tree | **3.07 % of one core** | 2.45 to 3.98 % |
| RSS, whole process tree | **2,274 MB** | 2,271 to 2,277 MB |
| Threads, whole tree | 128 | 126 to 145 |
| VRAM used by Jarvis | about **6.0 GB** | drops by 0.3 GB after 5 min (embedding model unloads) |

RSS by process at the end of the run:

| Process | RSS |
|---------|----:|
| Daemon (`python`) | 864 MB |
| Kokoro GPU helper (`python`) | 1,099 MB |
| Codex app-server (`codex.exe`) | 126 MB |
| MCP server (`npx` → 2 × `node.exe`) | 126 MB |
| Shell wrappers (`cmd`, `conhost`) | about 50 MB |

### Where the daemon's idle CPU goes

A sampler thread inside the harness recorded every thread's current frame 4 times a second for 4 minutes (959 samples) and the per-thread CPU time at the end:

- The **listener thread** is the only steady consumer (11.5 s of CPU since launch, which includes Whisper's load). Everything else sleeps on a lock, a selector or a pipe: main loop, scheduler, web chat HTTP and hub, MCP runtime, Codex readers, stdin monitor, dictation hotkey hook, Whisper transcription worker.
- Measured in isolation on 30 s of audio: the **wake-word sound detector** (three ONNX models scored every 80 ms, whether or not anyone is speaking) costs **1.6 % of one core**, about half of the daemon's idle CPU. WebRTC VAD costs under 0.1 %.
- The web chat hub wakes every 0.5 s and runs two SQLite queries even when no browser is connected: 0.06 % of one core. Negligible, but it is the one non-audio timer that never sleeps.
- About 12 native threads (CTranslate2 and ONNX Runtime pools) each used about 1 s during the warm-ups and then stayed idle.

### Desktop app

Offscreen, 30 s idle, 3 runs: **0.6 %** of one core with the face window hidden. With the face window shown (it opens at launch) the orb animates at its resting 16 FPS (`RESTING_INTERVAL_MS = 62`) and costs **10.7 %** of one core, more than three times the whole daemon. The orb's timer stops only when the window is hidden, not when it is minimised behind other windows or covered. This was measured with Qt's offscreen platform; the real display path may differ and should be checked on the desktop.

Other desktop timers, all cheap: tray icon refresh every 500 ms, daemon status check every 2 s, wake overlay state poll every 100 ms (a safety net under its file watcher), pointer ring flag read every 200 ms.

### Short-interval loops found in the code

| Where | Interval | Runs when | Verdict |
|-------|---------:|-----------|---------|
| `daemon.py` main loop | 1.0 s sleep | always | adds up to 1 s to every shutdown; an `Event.wait` would wake at once |
| `listener.py` capture loop | `get(timeout=0.2)` | always | blocking wait, fine |
| `listener.py` dictation pause | 0.05 s sleep | only while dictating | fine |
| `tts.py` worker queues | `get(timeout=0.5)` | always | blocking wait, fine |
| `tts.py` playback | 0.05 s sleep | only while speaking | fine |
| `webchat/hub.py` sync | 0.5 s | always while web chat is on | negligible CPU, polls SQLite with no client |
| `webchat/mic.py` | 1.0 s | while web chat is on | negligible |
| `orb_widget.py` | 62 ms (16 FPS) at rest, 30 ms active | whenever the face window is shown | **10.7 % of one core** |
| `wake_overlay.py` watch | 100 ms | always | cheap state read |
| `pointer_ring.py` poll | 200 ms | always | cheap shared-memory read |
| `app.py` tray icon / status | 500 ms / 2 s | always | cheap |

## Models

Loaded at start (owner profile) and what each costs. Load times are cold loads, 3 runs each; Ollama sizes are from `GET /api/ps` at Jarvis's `num_ctx` of 8192.

| Model | Loaded | Load time | RAM | VRAM | Used | Residency |
|-------|--------|----------:|----:|-----:|------|-----------|
| Whisper `medium` int8, CUDA (faster-whisper) | at start, listener thread | 3.0 to 3.8 s + 0.75 s warm-up transcription | 0.54 GB in the daemon | 1.18 GB | every utterance | for the life of the process |
| Kokoro v1.0 (ONNX, GPU helper process) | at start, **main thread** | 2.2 to 3.5 s (helper spawn 1.4 s, warm-up synthesis) | **1.10 GB** helper process | 0.79 to 0.93 GB | only when Jarvis speaks | for the life of the process |
| openWakeWord (`hey_jarvis` + melspectrogram + embedding) | at start, background | 0.14 s | 57 MB | none (CPU) | **scored every 80 ms, always** | life of the process |
| `qwen3.5:4b` (fast tier: intent judge, tool router) | warm-up at start | 3.8 s (8.7 s first run of the night) | Ollama's | **3.19 GB** | every voice request (intent judge) | 30 min after each request |
| `nomic-embed-text` | warm-up at start | 1.35 s | Ollama's | 0.31 GB | memory lookups | **5 min** (Ollama's default: embedding requests carry no `keep_alive`) |
| `gpt-oss:20b` (chat, local replies only) | warm-up at start in local mode | 13 to 14 s (45 s first run) | **3.7 GB spills to system RAM** | 9.72 GB on the GPU of a 13.4 GB model | local replies, diary in local mode | 30 min |

Findings:

1. **Local mode warm-ups are aborted.** Chat and intent warm-ups use `llm_routing_timeout_sec` (8 s). A cold `gpt-oss:20b` needs 13 to 45 s, and with both loads competing `qwen3.5:4b` misses the deadline too. When the client gives up, Ollama logs `client connection closed before llama-server finished loading, aborting load` and drops the half-loaded model. Result: 3 s longer start-up, both warm-ups reported failed, and the first reply pays the whole cold load anyway.
2. **`gpt-oss:20b` does not fit this card.** At `num_ctx` 8192 it is 13.4 GB; even alone, 3.7 GB lives in system RAM. With Whisper (1.2 GB), Kokoro (0.8 GB), `qwen3.5:4b` (3.2 GB) and the desktop's own 2 to 3 GB also on the GPU, Ollama has to push far more of it to the CPU or evict the intent model.
3. **Kokoro is the biggest RAM item and is used rarely.** Its GPU helper is a second Python process holding 1.1 GB of RAM and about 0.8 GB of VRAM for the whole session, for speech that happens a few times per conversation. The same voice on the CPU (`tts_kokoro_device: cpu`, measured 3 times each):

   | Kokoro device | Load | RAM | VRAM | First audio of a two-sentence reply | Whole reply synthesised |
   |---------------|-----:|----:|-----:|------------------------------------:|------------------------:|
   | GPU (helper process) | 2.2 to 3.5 s | 1.10 GB | 0.8 to 0.9 GB | 0.17 to 0.22 s | 0.44 to 0.57 s |
   | CPU (in the daemon) | 1.1 s | 0.38 GB | none | 0.69 to 0.71 s | 1.95 s (still faster than it plays) |
4. **The intent model is the biggest VRAM item.** `qwen3.5:4b` stays resident (3.2 GB) because every voice request refreshes its 30-minute residency. It is needed on every voice request, so it is used often; the question is only whether a smaller model judges intent well enough.
5. **The embedding model's residency does not follow the setting.** `/api/embeddings` requests carry no `keep_alive`, so Ollama unloads `nomic-embed-text` after its 5-minute default (observed at about 300 s in the idle run) and the next memory lookup pays a 1.35 s reload. `listening.spec.md` says the backend applies the 30-minute residency to every inference request.
6. Nothing unloads models when Jarvis quits: Ollama keeps them for the remainder of `keep_alive` (30 min) unless the desktop app stops an Ollama it started itself.

## Proposed fixes, ranked by benefit

Benefit is what the owner notices: seconds at start-up and shutdown, and resources held all day. Fixes that touch daemon start-up or config (marked ⚙️) should wait until `feat/smart-home` has merged, as agreed.

| # | Fix | Expected gain | Risk | Specs touched |
|---|-----|---------------|------|---------------|
| 1 | ⚙️ **Load Kokoro (TTS) in parallel with Whisper** instead of before the listener starts. Start TTS on its own thread; queue anything spoken before it is ready. | **−2.3 to −2.7 s** of the 9.6 s start-up (about 25 %) | Low to medium: two GPU loads at once (check VRAM peak and that both still land on the GPU); start-up message order changes | `output/tts.spec.md`, `listening/listening.spec.md` (Startup & Model Warmup) |
| 2 | **Calm the orb when nothing is happening.** Lower the resting frame rate (for example 4 to 8 FPS, or freeze after a few idle seconds) and stop the timer while the face window is minimised or fully covered. | **About −8 to −10 % of one core** whenever the face window is open (larger than the whole daemon's idle use) | Low: visual only; verify on the real display first, since offscreen painting may cost more or less | `desktop_app/orb_widget.spec.md` (Lightweight: the 16 FPS figure is specified) |
| 3 | **Do not make shutdown wait for the diary.** Save the pending turns to the database at shutdown (milliseconds) and summarise them in the background after the next start, instead of summarising before exit. | **−45 s** on every local-mode shutdown after a conversation; −1.5 s in Codex mode; the diary dialog goes away | Medium: pending turns become stored data (same local database, same redaction and private-turn rules); the diary entry appears after the next start instead of at quit | `memory/summariser.spec.md` (time budget), `memory/db.spec.md`, `desktop_app/desktop_app.spec.md` (clean shutdown, diary dialog), `docs/llm_contexts.md` |
| 4 | **Run Kokoro on the CPU** (owner's call: replies start about 0.5 s later). Alternatives: unload the GPU helper after a few idle minutes (costs about 2.5 s before the first spoken reply after idle), or Piper. | **−0.7 GB RAM and −0.8 GB VRAM** all day, −1.4 s start-up (−1 s more on top of fix 1's parallel load) | Low technically (already a setting); the cost is first-word latency | `output/tts.spec.md` (only if the default changes) |
| 5 | ⚙️ **Stop aborting model warm-ups in local mode.** Give warm-ups their own budget sized for a cold load and let them finish in the background without holding "Listening!" (or hold only for the intent model). | **−3 s** start-up in local mode, and the first local reply no longer pays the cold load (today 70 to 85 s, with a timed-out first call) | Low: warm-ups are already best-effort; must not overlap a real request's load badly | `listening/listening.spec.md` (warmup concurrency), `llm/llm.spec.md` (`warm_up`), `docs/llm_contexts.md` |
| 6 | **Only score the wake-word sound model when there is sound.** Skip the three ONNX models while the frame energy is below a floor (below `voice_min_energy`), keeping a short pre-roll so the start of the word is not lost. | **About −50 % of the daemon's idle CPU** (−1.6 % of one core) | Medium: quiet wake words could be missed; needs a recorded-audio check | `listening/listening.spec.md` (wake word sound) |
| 7 | ⚙️ **Make the local chat model fit the card**, or let the intent judge share the chat model when they are both local. This is also what makes fix 3 and fix 5 slow today. Owner decision: a smaller chat model (for example a 9B, about 6.6 GB) or keeping `gpt-oss:20b` with CPU spill. | Removes 3.7+ GB of CPU spill and model eviction in local mode; much faster local replies | Low technically; it is a quality choice | `llm/llm.spec.md` (tiers), config defaults |
| 8 | **Try a smaller intent model** (`qwen3.5:0.8b` is installed, about 1 GB) or a smaller context for the fast tier, if the intent and routing evals hold. | Up to **−2.2 GB VRAM** all day, faster intent decisions | Medium: accuracy; must be proven with the intent judge and routing evals before switching | `listening/listening.spec.md` (intent judge), `llm/llm.spec.md`, `docs/llm_contexts.md` |
| 9 | **Send `keep_alive` with embedding requests**, so `nomic-embed-text` follows `llm_keep_alive` like every other request. | Removes a 1.35 s reload on the first memory lookup after 5 idle minutes; +0.3 GB VRAM held | Low; brings the code in line with the spec | `llm/llm.spec.md` |
| 10 | ⚙️ **Faster shutdown:** wake the main loop on an `Event` instead of a 1 s sleep, and stop independent subsystems (Codex child, MCP runtime, TTS helper, voice thread) in parallel. | −0.5 s on every shutdown; up to −5 s when quitting or restarting during start-up | Low to medium: teardown order where one part depends on another (the diary pass needs the database open) | `desktop_app/desktop_app.spec.md` (stop flow), `bridge/bridge.spec.md`, `tools/external/mcp_runtime.spec.md` |
| 11 | **Trim imports:** import `webrtcvad` (and its `pkg_resources`), `rapidfuzz` and `requests` where first used. | About −0.3 s start-up | Low | none (no behaviour change) |
| 12 | **Let the web chat hub sleep while no browser is connected** instead of polling SQLite twice a second. | Negligible CPU; fewer wake-ups | Low | `webchat/webchat.spec.md` |

Not worth doing now: the desktop app's own start-up (about 1 s), the 0.54 s application index (already on a background thread), the idle native thread pools.

## Results after the spec-neutral fixes

Branch `perf/efficiency-fixes`, cut from `develop` at `32d0ea8` (which includes smart home). Only the fixes that need no spec change were made; the rest wait for the owner because they contradict a sentence in a spec or are product decisions.

"Before" is `32d0ea8` itself, measured again with the same harness, the same owner profile and the same night, alternating before and after runs so drift affects both equally. (The `develop` baseline is about 0.8 s faster to "Listening!" than the `70361d8` figures above, mostly because the earlier runs overlapped the sibling test run.)

| Measure (owner profile) | Before | After | Change | Runs |
|-------------------------|-------:|------:|-------:|-----:|
| Start-up to "Listening!" | 8.76 s (8.50 to 10.07) | **6.74 s** (6.50 to 7.02) | **−2.0 s (−23 %)** | 5 + 5 |
| Shutdown, nothing to save | 0.90 s (0.86 to 1.34) | **0.49 s** (0.44 to 0.67) | **−0.4 s (−46 %)** | 5 + 5 |
| Embedding model still loaded after 6 min idle | no (unloaded at about 5 min) | **yes** | next memory lookup no longer pays a 1.35 s reload | 1 + 1 |
| Idle CPU, 5 min | 2.77 % of one core | 2.77 % of one core | none expected | 1 + 1 |
| Idle RAM | 2,277 MB | 2,277 MB | none expected | 1 + 1 |
| `--smoke-test` (through the harness) | passes 3/3 | passes 3/3 | `SMOKE_TEST_INIT_OK` comes about 0.5 s later because TTS now loads after the listener starts; the smoke marker is not a readiness measure | 3 + 3 |

Hardware check after the review fixes (5 cold starts, owner profile, Kokoro on the graphics card through its helper): every run printed `Kokoro voice ready … on the graphics card` and `Whisper 'medium' loaded on cuda`, with no cuDNN error, no ONNX Runtime move to the processor and no fall-back to Piper. "Listening!" at 6.5 to 7.8 s (median 6.9 s), shutdown 0.61 to 0.68 s, and after each quit no MCP `node`/`cmd` process remained (two `node` processes ran during each session).

Start-up timeline after the change (first after run): the listener's microphone check ends at 2.5 s, Whisper starts loading at 2.8 s, Kokoro is ready at 5.2 s (it loaded during Whisper's load), Whisper is loaded at 6.2 s, "Listening!" at 7.0 s.

| # | Fix | Status | Why |
|---|-----|--------|-----|
| 1 | Load the voice while Whisper loads | **Done** | The listener starts first and reports when its microphone check is over; the voice then loads on the starting thread while Whisper loads on the listener's. No DLL load races the check (the Windows loader-lock hazard the code guards against), and voice outputs still warm on the starting thread as `extensions/extensions.spec.md` requires. |
| 2 | Calm the orb | Waits for the owner | `orb_widget.spec.md` specifies about 16 FPS at rest |
| 3 | Do not make shutdown wait for the diary | Waits for the owner | `memory/summariser.spec.md` defines the shutdown flush |
| 4 | Kokoro on the CPU | Waits for the owner | voice-quality and latency choice |
| 5 | Stop aborting warm-ups in local mode | Waits for the owner | `listening/listening.spec.md` fixes the warm-up timeout (`llm_routing_timeout_sec`) and the 60 s join |
| 6 | Wake-word sound gate | Waits for the owner | `listening/listening.spec.md` says every captured frame is fed to the detector, which scores each 80 ms step |
| 7 | Local chat model choice | Waits for the owner | product decision |
| 8 | Smaller intent model | Waits for the owner | accuracy decision; needs evals |
| 9 | `keep_alive` on embedding requests | **Done** | `llm/llm.spec.md` and `docs/llm_contexts.md` already require it on every Ollama request |
| 10 | Event-woken main loop, engines stopped side by side | **Done** | No spec fixes the teardown order; TTS, the MCP runtime and the reply bridge stop concurrently, bounded past the slowest one's own worst case; the MCP runtime stops its servers in parallel too |
| 11 | Trim imports | Not done | Measured on `develop`: the only avoidable import is `pkg_resources` (0.13 s, pulled in by `webrtcvad`), and avoiding it means depending on that package's private C module. The other heavy imports (`rapidfuzz`, `requests`, `sounddevice`, `numpy`) are needed during start-up anyway, so deferring them only moves the time. |
| 12 | Web chat hub idle sleep | Not done | `remote/remote.spec.md` promises the phone's poll wakes "within half a second" (the hub's 0.5 s tick), and `webchat/webchat.spec.md` requires every turn to be mirrored into the open chat whether or not a page is open; the gain was 0.06 % of one core |

## Reproducing

1. Copy `~/.config/jarvis/config.json` and the database to a temporary folder, point `db_path` at the copy, and turn off anything that acts on the outside world (extensions, gestures, TV, phone access). Point `JARVIS_CONFIG_PATH` at the copy.
2. Start Ollama. From the temporary folder, with `PYTHONPATH=src` and the project interpreter:

```bash
python docs/benchmarks/efficiency_harness/run_bench.py smoke <config.json> 3 owner
python docs/benchmarks/efficiency_harness/run_bench.py run <config.json> owner_short1 15
BENCH_SAMPLE_DELAY=60 BENCH_SAMPLE_SEC=240 python docs/benchmarks/efficiency_harness/run_bench.py run <config.json> owner2 300
python docs/benchmarks/efficiency_harness/summarise.py owner_short owner2
python docs/benchmarks/efficiency_harness/measure_models.py whisper
python docs/benchmarks/efficiency_harness/ollama_sizes.py
python docs/benchmarks/efficiency_harness/kokoro_speed.py cpu
QT_QPA_PLATFORM=offscreen python docs/benchmarks/efficiency_harness/bench_desktop.py --show-face
```

`run_bench.py` writes logs and JSON to `BENCH_OUT` (default: the current folder). `BENCH_CHAT="..."` sends one chat message after start-up, to measure shutdown with a conversation to save. Imports: `python -X importtime -c "import jarvis.daemon" 2> imports.txt`. Main-thread profile: `python -m cProfile -o smoke.prof docs/benchmarks/efficiency_harness/bench_daemon.py smoke`, then `pst.py smoke.prof`.

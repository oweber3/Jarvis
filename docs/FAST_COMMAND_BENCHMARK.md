# Local command routing measurements

Measured on 3 October 2026, Windows 11, using the existing micromamba environment
and local Ollama `qwen3.5:0.8b` for FAST and CHAT. The configured default
`gemma4:e2b` is not installed on this machine, so the harness selects the
installed model in memory without rewriting configuration. The application
index is ready before the timer starts. Each command has fresh temporary
dialogue/database state; MCP and location enrichment are off.

`tests/performance/benchmark_fast_commands.py` feeds synthetic finalised
wake-word transcripts into the actual listener processing method, waits for
the configured collection deadline where applicable, runs the actual reply
engine and invokes real routine tools through the central registry. Capture,
Whisper, TTS playback and application startup completion are outside the timer.
The harness dispatches the engine directly with fresh state; the shared query
lock and TTS completion/hot-window callbacks are exercised by integration tests.
Contention with another active query is outside these measurements.
The harness permits only the requested tool/action; unrelated model-selected
operations are recorded and blocked.

| Command | Conversational return | Conversational tool invocation | Fast tool | Fast tool entry | Fast completion |
|---------|-----------------------|--------------------------------|-----------|-----------------|-----------------|
| What time is it? | 8,637 ms | getWeather, wrong location/action | getTime | 9 ms | 9 ms, success |
| Open Word | 5,772 ms | getWeather after judge changed the query | appControl/open | 13 ms | 99 ms, launch accepted |
| Set volume to 30% | 5,958 ms | No tool invoked | systemVolume/set | <1 ms | 61 ms, device state read back |
| Pause the music | 5,840 ms | mediaControl with invalid `toggle` action | mediaControl/pause | <1 ms | 16 ms, no active player |
| What's using the most RAM? | 5,351 ms | No tool invoked | systemInfo/top_memory | <1 ms | 778 ms, success |

Every conversational measurement calls both the LLM and intent judge and waits
through collection. Every fast measurement calls neither and skips collection.
The conversational timings are transcript-to-return measurements: none executes
the correct requested action, so there is no valid action latency to compare.
The fast completion figures include the real tool's result and safety checks.
One sample per command is indicative rather than a percentile distribution.

The RAM-process read is the largest measured remaining cost. All Windows tools
keep their existing bounded deadlines. Pause's no-player result verifies honest
failure reporting, not a playback transition. Unit/integration tests cover true
pause and its no-toggle guarantee; active-player manual validation needs a
running SMTC player. Microphone-to-action and spoken-response latency need a
live speech capture run. Normal dispatch's TTS callbacks are covered by tests.

Run in a desktop session so Windows can launch apps and access audio sessions:

```powershell
$env:PYTHONUTF8 = '1'
.\.mamba_env\python.exe tests/performance/benchmark_fast_commands.py --output .tmp/fast-command-benchmark.json
```

The benchmark changes volume to 30%, requests a Word launch and sends an explicit
media pause. It does not close applications or modify the user's memory/config.
Raw local results remain in the chosen output file; no telemetry is sent.

Data privacy comes first, always. Do not expose personal information externally without an explicitly configured integration, and do not add telemetry.

Jarvis is local and offline-first by default. No proprietary cloud service may ever become a required dependency, and all existing local functionality must continue working fully offline without external providers. Optional model-provider adapters (such as OpenAI-compatible endpoints or a custom model router) are permitted when intentionally and explicitly configured by the user, but the core offline path remains the baseline. When evaluating integrations, avoid hard dependencies on vendor-locked cloud services and prioritise self-hostable protocols and local equivalents.

All user-facing command line output should make use of emojis. Especially an initial emoji to start off the lines that depict what the line is about. Output should make use of indentation spacing to establish a visual hierarchy and aim to make output as easy to sift through as possible. Exception: Windows .bat scripts cannot use emojis (cmd.exe doesn't render Unicode properly).

Any important point in our logical flows should have debug logs using the `debug_log` method from `src/jarvis/debug.py`. Avoid excessive logging to keep the logs easily readable and actionable.

Any code change must either adhere to our spec files perfectly or you should ask the user to confirm changes, which should also propagate to the specs themselves. Spec files follow the \*.spec.md format and live next to the code that implements them. Always search for related spec files before starting any work. When corrected about how something should work, check if there's a spec for it and whether it needs updating.

### Spec File Registry

Windows application, window (including virtual desktops) and path controls (including open-by-name file search and Steam games) are specified in `src/jarvis/platform/windows/apps_paths.spec.md`.

Local extensions in `extensions/<name>/` keep their own spec files next to their code; search there too.

| Spec file | Covers | Key principles |
|-----------|--------|----------------|
| `src/jarvis/fastpath/fastpath.spec.md` | Deterministic whole-utterance local commands, locale phrases, engine/listener hooks | One routine tool through central safety; uncertain requests fall through; no LLM |
| `src/jarvis/bridge/bridge.spec.md` | Reply modes (local, Codex, Claude) and runtime switching by voice, text and tray among the modes allowed in Settings; shared layer of the background reply bridges: request broker, `jarvis_execute` tool contract and snapshot, tool execution, confirmation, delivery, reply path, per-mode sharing settings, runtime handle | A cloud mode that is not allowed is never entered; local mode is the offline baseline and starts nothing; every tool runs through central safety on the query thread; replies only after a successful turn; failures are explicit, never rerouted to a model or replayed |
| `src/jarvis/claude_bridge/claude_bridge.spec.md` | Optional background Claude reply mode: headless Claude Code CLI per request, stream-json protocol, in-process MCP tool server on the same pipe, child-only environment (analytics off), isolation flags, subscription-only sign-in, spare session, failure mapping, instructions | Claude does the reasoning in hidden Jarvis-owned processes; no shim, socket or API key; builds on the shared bridge contract |
| `src/jarvis/codex_bridge/codex_bridge.spec.md` | Optional background Codex reply mode: per-request ephemeral sessions and ownership, spare thread, preflight, dynamic tool transport, failure mapping, instructions, config migration and return to local | Codex does the reasoning in a hidden Jarvis-owned process; builds on the shared bridge contract |
| `src/jarvis/codex_bridge/app_server.spec.md` | The Jarvis-owned `codex app-server` child: hidden start, JSON-RPC over pipes, isolation overrides, executable resolution, measured tool surface and persistence | Owns only its own process; process-scoped config only, never edits the user's Codex files; the reader thread never executes |
| `src/desktop_app/desktop_app.spec.md` | System tray app, startup flow, daemon integration, windows, theme, updates | Desktop is separate from core; jarvis has no knowledge of desktop_app |
| `src/desktop_app/orb_widget.spec.md` | Reactive orb visual in the face window: state mapping, animation model, the live voice levels the daemon shares (`jarvis.voice_levels`) | Pure model separate from painter; animates only while visible; real amplitude optional with graceful fallback |
| `src/desktop_app/wake_overlay.spec.md` | Wake screen effect: soft holographic light on every screen's edges that breathes and pulses with the state, from the wake word until the conversation ends (`wake_overlay_enabled`), edge windows, full-screen skip, Windows helpers | Click-through, never focused, never covers the middle of a screen; animates only while visible and repaints only what changed; removes every window when done |
| `src/desktop_app/settings_window.spec.md` | Auto-generated settings UI from config metadata | Metadata-driven; only non-default values written; preserves unknown keys |
| `src/desktop_app/setup_wizard.spec.md` | First-run wizard (Ollama, models, Whisper, location) | Minimal friction; only shown when user action required; doesn't configure everything |
| `src/desktop_app/chat_window.spec.md` | Text chat interface alongside voice; shared conversation, no TTS, bundled callbacks + subprocess IPC | One conversation for voice + text; text never speaks; redaction shared with voice path |
| `src/jarvis/dictation/dictation.spec.md` | Hold-to-dictate engine, hotkey, clipboard paste | Independent from assistant pipeline; shared Whisper model; pause flag on listener |
| `src/jarvis/listening/listening.spec.md` | Voice listener, wake word detection, audio pipeline, optional speaker verification and barge-in | Transcript-first wake; the owner's voiceprint is local biometric data (never logged, never sent, deletable); verification never blocks speech when unavailable |
| `src/jarvis/reply/reply.spec.md` | LLM reply generation, tool use, profiles | Tools return raw data; profiles handle formatting |
| `src/jarvis/reply/evaluator.spec.md` | **Deprecated** — evaluator no longer runs in the reply engine; preserved for reference | Replaced by the planner; see planner.spec.md |
| `src/jarvis/reply/planner.spec.md` | Task-list planner: pre-loop query decomposition + direct-exec step resolver for small models | Fail-open; rides warm small model chain; advisory for large models, direct-exec for small |
| `src/jarvis/platform/windows/windows.spec.md` | Windows OS layer and the `systemVolume`, `mediaControl`, `systemInfo`, `systemSettings` (settings pages, brightness, power plan, audio output) and `inputControl` (hotkeys, clipboard) tools | OS functions know nothing of tools or the LLM; lazy OS imports; every OS call time-bounded; pause never toggles; routine actions need no confirmation |
| `src/jarvis/platform/windows/workspaces.spec.md` | Named windows workspaces (`windows_workspaces`): browser windows and apps opened and placed in one request, `workspaceControl` tool, fast path | Config-driven (no code to add one); whole workspace validated before anything launches; one launch per item, never relaunched; paths and URLs never leave `config.json` or reach logs/results |
| `src/jarvis/routines/routines.spec.md` | Routines (`routines`): named chains of existing tool calls run in one request, whole-routine validation against the live registry, ordered steps, per-step results, the recent-actions journal, saving from what Jarvis did with a confirmed read-back, `routineControl` tool, fast path, bridge snapshot rules | Platform-neutral; config-driven, no code per routine; every step goes through central safety on every run and a definition never pre-approves anything; steps come only from executed calls; changes always confirm; no arguments, paths or URLs in results or logs |
| `src/jarvis/devices/roku.spec.md` | Roku TV control (`roku_host`, `tvControl` tool, ECP client, SSDP recovery, `tv` fast-path family) | Local network only, no cloud; address in config only and private ranges only; routine actions SAFE (power off included); honest failure when the TV is unreachable or Control by mobile apps blocks it; tests use a fake ECP server and never command the real TV |
| `src/jarvis/extensions/extensions.spec.md` | Local extensions (`extensions_enabled`, `extensions_dir`): loading the user's own code from `extensions/<name>/`, `ExtensionAPI` (tools, fast-path phrases, settings pages, voice outputs, start and stop, state events, device referents), the `VoiceOutput` contract in the Piper engine | Nothing loads unless named; a broken extension warns and is skipped; everything it adds goes through central safety and the normal paths; core never names a particular extension |
| `src/jarvis/gestures/gestures.spec.md` | Webcam hand tracking for gestures: `HandTracker` pipeline (OpenCV capture, MediaPipe Hand Landmarker, mirrored `HandFrame` landmarks to subscribers), verified model download, landmark recordings, `python -m jarvis.gestures` diagnostic | Local only; frames stay in memory and subscribers never see pixels; camera on only while running; missing MediaPipe or model keeps it off and says why |
| `src/jarvis/platform/windows/ui_automation.spec.md` | `uiControl` (UI Automation snapshots and pattern-based actions on any app) and `pdfNavigate` (pages, chapters and topics of the open PDF) | Patterns only, never mouse or keyboard input or focus; irreversible controls confirm, password fields are denied; PDF text read locally with `pypdf`; files never guessed |
| `src/jarvis/tools/builtin/local_files.spec.md` | `localFiles`: find by name, type and date (Everything, Windows Search or a folder scan), list, read, write, append, delete, move, copy, rename; known folders; home boundary | Find is read-only and bounded; changes stay inside the home folder; move, copy and rename never replace anything; no names or paths in logs |
| `src/jarvis/tools/builtin/tool_search.spec.md` | toolSearchTool escape hatch for mid-loop tool routing | Re-runs the same router; never removes stop/self; capped per reply |
| `src/jarvis/tools/builtin/screenshot.spec.md` | Screen awareness (`screen_awareness_enabled`, `screenshot` tool): capture on request, offline Windows OCR, fenced screen text, one scaled image delivered to the local vision model, Codex or Claude | Only when asked, never stored; one switch removes it from every route; screen text is untrusted data; nothing of the screen in logs |
| `src/jarvis/tools/external/mcp_runtime.spec.md` | Persistent MCP runtime: per-server long-lived stdio session, queue-based dispatch, retry on transient session loss | One worker per server keyed by config; calls to the same server serialise; `MCPServerSessionError` for session-level failures; opt-in `idle_timeout_sec` for stateless servers |
| `src/jarvis/reply/prompts/prompts.spec.md` | System/user prompt templates | — |
| `src/jarvis/tools/builtin/web_search.spec.md` | webSearch tool: cascade fetch, SSRF guard, prompt-injection fence, links-only envelope | Untrusted web content is fenced as data, not instructions; rank preference over speed; honest failure over confabulation |
| `src/jarvis/tools/builtin/nutrition/log_meal.spec.md` | logMeal tool: single-property schema for planner fast-path, internal nutrition extraction, untrusted-data fence, follow-ups | Public schema is a single optional `meal` string; nutrition fields are internal; user text is fenced as data |
| `src/jarvis/utils/location.spec.md` | GeoIP location detection | Privacy-first; local GeoLite2 DB only |
| `src/jarvis/memory/activity_log.spec.md` | Opt-in local activity log: foreground application plus redacted window title and idle time in the Jarvis database, exclusions, pause, retention, delete-all, tray items, the read-only `activityLog` tool, the cloud boundary for Codex and Claude | Off by default; an explicit exception to the "no window titles" convention; never reaches the diary, graph, logs, issue reports or a cloud model unless `activity_log_share_with_cloud` is true |
| `src/jarvis/remote/remote.spec.md` | Opt-in phone access: the daemon serves a phone web app (orb, shared conversation, quick actions, desktop confirmations) to paired phones; pairing codes and hashed device tokens; private-network client allowlist; tray `📱 Phone Access` dialog and `python -m jarvis.remote` | Off by default; no cloud relay, standard library server; only private network clients and paired devices; requests go through `submit_text_query` so redaction, the lock and central safety apply; transcript mirrored in memory only |
| `src/jarvis/memory/graph.spec.md` | Node graph memory (v2), self-organising tree, UI explorer | Dynamic structure; access-aware; auto-split/merge (future) |
| `src/jarvis/memory/summariser.spec.md` | Diary summariser prompt contract, hygiene rules (deflection, attribution, topic separation), post-process scrub, and bulk-sweep clean button | Two-layer defence: prompt + deterministic scrub; corrupted summaries poison every downstream consumer |
| `src/jarvis/memory/desktop_referents.spec.md` | Short-lived record of what Jarvis just acted on (windows it opened, focused or placed, the TV or head, the media player, the clipboard's content type) and the window the user is looking at per request, so follow-ups ("close this", "move it to the second monitor", "pause it") act on the exact target; cloud sharing switches | In memory only, bounded, dialogue-window lifetime; recorded from tool outcomes on every route; foreground read on demand, never Jarvis's or the shell's windows, never for phone requests; the model resolves references, never language patterns; no titles, paths or content |
| `src/jarvis/memory/recall_gate.spec.md` | Deterministic skip-enrichment heuristic when the hot window covers a follow-up | Fail-open; language-agnostic via `\w{3,}` + `re.UNICODE`; planner intent always wins |
| `src/jarvis/llm/llm.spec.md` | Pluggable LLM backend abstraction: `LLMBackend` ABC, `OllamaBackend`, `OpenAICompatibleBackend`, factory dispatch on `llm_provider`, `get_embedding_backend` override, backend-owned `num_ctx` and `llm_keep_alive`, config migrations, the two-tier model system (`Tier.FAST` / `Tier.CHAT` via `resolve_model`), function-style helpers | Provider-agnostic interface so Jarvis can run on Ollama or OpenAI-compatible servers (LM Studio / oMLX / llama.cpp / vLLM / LocalAI); every context states its tier instead of defining a model fallback chain |

The LLM contexts graph at `docs/llm_contexts.md` maps every LLM call in the app (model, gating, inputs, outputs, limits, flow). Keep it up-to-date at all times: any change that adds, removes, or alters an LLM context (model resolution, timeout, cap, prompt source, gating flag, data-flow edge) must update `docs/llm_contexts.md` in the same PR.

Avoid hardcoded language patterns as this assistant needs to support an arbitrary amount of different languages.

Tools define when/how to be used and return raw data without LLM processing. The unified system prompt in `src/jarvis/system_prompt.py` handles response formatting and personality through the daemon's LLM loop.

## Project Context & Direction

- **Scope:** A Windows 11 desktop voice assistant based on `isair/jarvis`, local-first and offline by default.
- **Long-term Goal:** A highly capable Windows desktop assistant.
- **Key Design Goals:**
  - Native Windows application and system control.
  - Low-latency execution for simple commands with deterministic routing where appropriate (bypassing full LLM deliberation for obvious actions).
  - Preserve the normal LLM/agent path for ambiguous, multi-step, or conversational requests.
  - Modular Windows-specific functionality isolated cleanly from core assistant logic.
  - Future support for multiple model providers without rewriting application logic.
  - Preserve existing Jarvis functionality.
- **Fast Command Examples:**
  - "What time is it?"
  - "Open Word"
  - "Open MATLAB"
  - "Set volume to 30%"
  - "Pause the music"
  - "What's using the most RAM?"
- **Architecture Documentation:** Future architectural work is designated to live in `docs/JARVIS_DESIGN.md`.

## Development Rules & Safety

- Inspect relevant existing code before modifying it.
- Follow established Jarvis architecture and reuse existing abstractions rather than creating duplicate systems.
- Keep Windows-specific functionality isolated where practical.
- Prefer reliable Windows APIs, Win32, UI Automation, PowerShell, or established libraries over fragile coordinate-based clicking.
- Avoid unrelated refactors.
- Preserve working functionality. Test any behaviour that changes; do not claim a feature works unless it was actually verified.
- Do not expose personal information externally, and do not add telemetry.
- **Confirmation Policy:**
  - **Routine local actions** (e.g. opening applications, changing volume, controlling media, reading system information, opening folders, taking screenshots) do not need excessive confirmation.
  - **Potentially destructive actions** (e.g. deleting files, uninstalling software, overwriting important files, shutting down or restarting the PC, terminating critical processes) require explicit confirmation.

## Development Environment & Setup Guardrails

Each checkout has its own micromamba environment at `.mamba_env/` in the repo root. Call its interpreter directly rather than relying on activation or the system `python` (which has no project dependencies):

```bash
# Run the tests (from the repo root)
.mamba_env/python.exe -m pytest -q

# Headless boot check: starts the daemon, verifies it initialises, exits
PYTHONPATH=src .mamba_env/python.exe -m jarvis.main --smoke-test

# Launch the desktop app
python scripts/launch.py run_desktop_app
```

User config lives at `~/.config/jarvis/config.json` and is per-machine. Do not adapt code or defaults to the hardware of the machine you develop on; anything that depends on specific hardware (CUDA, monitor layouts, installed apps) should be verified on matching hardware, and the change summary should say what was and was not verified.

### Expected Working Setup
- Micromamba is installed and configured.
- The project Python environment already exists in `.mamba_env/`.
- Jarvis desktop UI launches successfully.
- Whisper runs locally (CUDA on an NVIDIA GPU, otherwise CPU).
- Ollama is installed and running with local models including `qwen3.5:0.8b` and `nomic-embed-text`.
- Text-to-speech (TTS) works.
- Dialogue memory works.
- MCP support exists.
- Chrome automation exists.
- Dictation works.

### Guardrails
- Do **not** reclone the repository.
- Do **not** recreate the environment.
- Do **not** reinstall working dependencies unnecessarily.
- Do **not** overwrite working configuration merely to normalise the setup.

## Git Workflow

- Never push to upstream `isair/jarvis`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for all commit messages and PR titles (e.g. `fix:`, `feat:`, `refactor:`, `docs:`, `test:`, `chore:`).
- For upstream contributions:
  - Feature branches and PRs must target `develop`, not `main`.
  - When pushing commits to a PR, always update the PR title and body to cover the entire changeset.
  - After creating a PR, run the `/review-pr` skill on it before considering the task complete.
  - Squash-merged commits on `develop` should only carry the PR number in the title (e.g. `(#171)`), never the originating issue number. Issue references belong in the commit body as `Closes #NNN`.

## Extending Jarvis

Before adding a tool, routine, workspace, fast-path family or MCP server, read `.agents/skills/new-tool/SKILL.md`. It picks the smallest fitting option and lists every place a new built-in tool must touch.

## Issue Triage

Use the `/triage` skill for triaging open issues and discussions. It owns the full workflow, diagnosis patterns, labelling conventions, and reply tone.

## Releases

"Release" means fast-forwarding `main` to the current tip of `develop` and pushing it. First sync local `develop` with `origin/develop` so you ship the real head. No merge commit, no force push: just `git checkout main && git merge --ff-only develop && git push origin main`. This triggers the release workflow and the auto-close of issues referenced by `Closes #NNN` in the develop commits.

## README Maintenance

Keep README.md up-to-date when making changes that affect user-facing functionality. Update the README when:
- Adding or removing built-in tools (update Features → Built-in Tools list)
- Changing configuration options (update Configuration section)
- Adding new MCP integration examples
- Changing system requirements or installation steps
- Fixing or introducing known limitations

README priorities (in order of importance):
1. **Privacy-first messaging** - The local/offline nature is a core selling point
2. **Quick install** - Users should get running in minutes
3. **Features list** - High-level capabilities at a glance
4. **Known limitations** - Be transparent about what doesn't work yet
5. **Configuration** - Only document options users actually need
6. **MCP integrations** - Examples for popular tools
7. **Troubleshooting** - Common issues with solutions

Keep sections concise. Use collapsible `<details>` for lengthy content. Avoid documenting internal implementation details - the README is for end users, not developers.

---

When the user says "remember" something, add it to AGENTS.md in the appropriate section (project-specific above the ---, or portable below).

Run your changes and test them manually, iterate until everything is good.

Always use TDD: write failing tests first, then implement the fix. Tests should verify **behaviours**, not implementation details. Test what the system does (observable outcomes), not how it does it (internal state, mock call counts, etc.).

Ensure all your changes are covered by all appropriate form of automated tests - unit, integration, visual regression, evals, etc.

Tests should verify mechanisms, not current values. Assert against config-driven or computed references rather than hardcoding specifics that change between migrations.

Run evals after finalising a change that can affect agent accuracy.

Any change to LLM prompts (system prompts, tool incentives, constraints, etc.) must be verified against a relevant eval case. If no eval exists for the behaviour being changed, write one first. The eval should demonstrate the improvement — i.e. it should fail or show worse results before the prompt change and pass or improve after.

Commit your changes when you finish a fix or feature before moving on to the next task.

Before running `git commit --amend`, always check `git log --oneline -3` first to verify you're amending the correct commit.

Always use British English everywhere (e.g. "colour" not "color", "behaviour" not "behavior", "initialise" not "initialize").

Do not use em dashes (—) in GitHub issue/PR/discussion replies or any user-facing writing. Prefer a comma, a full stop, a colon, or parentheses depending on the clause. This applies to replies you post on the user's behalf and to text generated for them.

## Code, comments, specs, docs: describe the current state, not the history

The codebase is ours and releases are versioned. Git carries the history; the code carries the present. Anything you write that lives in a file (code comments, docstrings, `*.spec.md`, `docs/*`, READMEs) should describe what the system *is*, not what it *was* or how it *changed*. Avoid:

- "previously did X", "used to be Y", "this fixes the regression where…"
- "refactored to", "migrated from", "now uses", "no longer reads"
- "PR 2.5b will…", "deferred to follow-up", "TODO(PR X): drop this once…"
- Phases tables, migration rollouts, "Done / Pending" status columns inside specs

Commit messages and PR descriptions are the right place for "what changed and why" — they exist to be read in sequence. Files under `src/`, `docs/`, and `*.spec.md` are read as the present-day reference; historical narrative there ages badly and confuses future readers.

## Refactor completely or not at all

If you're touching every legacy call site, finish the job. Don't leave compat shims, fallback parameters, `SimpleNamespace` defaults, or `TODO(PR X)` markers for paths you are also rewriting in the same change. The whole point of doing the refactor is that the legacy shape goes away — keeping a half-converted state means future readers have to figure out which version of the contract is canonical, and the reasoning behind the old shape sits in the codebase as dead weight.

The exception is genuinely external boundaries the codebase does not own: on-disk config files written by a previous release, third-party API shapes, persisted database rows. Those need migration paths because users depend on them. Internal function signatures, helper modules, and call patterns inside `src/` are ours to change cleanly.

## Qt Layout: showing hidden widgets compresses existing ones

When toggling widget visibility inside a constrained layout, provide a scroll viewport whose content retains its minimum usable size. Wizard pages use `ScrollableWizardPage` or a dedicated scroll area. Do not grow the wizard with `adjustSize()` or hardcoded heights when revealing controls; the window must stay within the available screen and navigation must remain reachable.

## Prompt-engineering: denial-template mirroring

When a small model keeps producing a canonical denial ("I only have access to the information you have shared in our current conversation", "I don't have any personal information about you", etc.), don't argue against the denial in the system prompt — that rarely wins against strong priors. Instead, phrase the injected context so it literally occupies the semantic slot the denial refers to. If the model denies having "information the user has shared in prior conversations", label the block exactly that. The denial stops triggering because the thing it claims to lack is now visibly present in the prompt. Arguing with the model's priors is expensive; feeding the denial its own words with the data pre-filled is cheap.

# LLM Contexts Map

Every distinct LLM call in Jarvis, what feeds it, what consumes it, and how it is gated. This is the reference for optimising the app's main bottleneck (LLM latency). Keep it in sync with the code — see the note at the bottom.

> **Request shape.** Every Ollama completion, including startup probes, uses backend-owned `llm_num_ctx` (default 8192), residency (`llm_keep_alive`, `1m` in low-power mode) and a `think: false` default. FAST and CHAT sharing a model share the same load shape. Callers supply generation settings; context sizes belong to backends and future model targets.

> **Backend abstraction.** Every context below routes through `jarvis.llm` ([spec](../src/jarvis/llm/llm.spec.md)) via `get_llm_backend(cfg)` / `get_embedding_backend(cfg)`. Picking `llm_provider: openai_compatible` swaps the wire shape end-to-end without touching call sites. The active chat model is read directly from `cfg.llm_chat_model` (the `Settings` field that always carries the resolved value, populated by config-load from `ollama_chat_model` when the provider-aware key is left empty).

---

## 0. Deterministic command gate

- **Files**: `src/jarvis/fastpath/matcher.py`, `dispatcher.py` and `phrases/en.json` ([contract](../src/jarvis/fastpath/fastpath.spec.md)). No model context.
- **Engine gate**: after redaction and pending confirmations, a whole-utterance routine match executes through the central tool registry and returns through shared output/dialogue recording. Tool routing, planning, enrichment, warm-profile preparation and main chat contexts are all skipped.
- **Voice gate**: after echo/stop checks and pending confirmations, an engaged, finalised command calls normal dispatch directly and skips the intent judge and collection window. Collection fragments and TTS-captured transcripts retain the judge path.
- **Fallthrough**: uncertain/compound requests, unavailable targets/tools, disabled `fast_commands_enabled`, unlisted `fast_commands_locales` and unsupported languages retain their existing LLM data-flow edges. Only English phrases are supplied; text with no language signal uses English.
- **Tool result**: local-only time, routine OS data or locale acknowledgement, without personality generation or tool-result digestion. User/assistant turns enter shared redacted dialogue; routine OS carryover is omitted. Fast execution still rechecks central safety and validation.

## 0b. Background Codex reply route (external, cloud, opt-in)

- **Files**: `src/jarvis/codex_bridge/` ([spec](../src/jarvis/codex_bridge/codex_bridge.spec.md), [app-server client](../src/jarvis/codex_bridge/app_server.spec.md)) on the shared bridge layer `src/jarvis/bridge/` ([spec](../src/jarvis/bridge/bridge.spec.md)). Not an `LLMBackend`: a hidden, Jarvis-owned `codex app-server` child process runs one ephemeral session per request using the user's Codex ChatGPT sign-in. No Codex window, OpenAI API key or direct model request is involved.
- **Gating**: the active reply mode is `codex` (`bridge.modes`, started from `reply_mode`, default `local`, and switched at runtime by "use ChatGPT" / "go local" or the tray), which requires `codex_enabled` (default off; set in Settings or on the setup wizard's optional Cloud reply modes page). In `local` mode nothing here runs or is imported and no process starts. That wizard page runs only `lifecycle.check_sign_in` (start the child, `account/read`, stop): no thread or turn, no inference and no request content.
- **Model**: `codex_model` (default `gpt-6-luna`) at `codex_reasoning_effort` (default `low`), both checked against the runtime's model list before use and never substituted. No local tier.
- **Engine position**: after redaction, pending confirmations and the fast-command path. Deterministic commands still run locally with no session.
- **Skipped local contexts** (codex mode): #1 main loop, #3 enrichment extractor, #3b recall gate, #4 memory digest, #5 tool-result digest, #6 max-turn digest, #7 tool router, #8 tool searcher, #12 planner, #13 plan resolver, the warm profile, and the startup chat-model warmup (#21; the same holds in Claude mode).
- **Retained local contexts**: #2 intent judge (voice engagement still needs one small local call), embeddings, #9 summariser, #10 to #11b graph work, tool-specific contexts (#14), dictation filler removal. Those that would use the chat model (#9, #10, `logMeal`, dictation) run on the BACKGROUND tier, which resolves to the fast model here, so the chat model is never loaded.
- **Instruction source**: `src/jarvis/codex_bridge/prompts.py` (versioned, at most 2000 characters), sent as each session's base instructions. It holds no utterance, dialogue or configuration. Changes require a background eval comparison (`evals/codex_runner.py --mode codex --variant baseline|proposed`) on the configured model with inert tools.
- **Transmitted to OpenAI through Codex**: the redacted utterance; up to `codex_recent_dialogue_messages` recent messages when `codex_share_recent_dialogue` is on (otherwise no earlier conversation at all, since every session is new); redacted, size-bounded results of tools Codex calls (including `uiControl` snapshots and read text, and `pdfNavigate` outline titles and page snippets, when Codex calls those tools). Long-term memory, personal-data tools, the database, configuration and raw audio are never sent unless `codex_share_long_term_memory` exposes the personal-data tools. No window listing, display listing, path or window title is attached to a request: Codex obtains them on demand through `windowControl` (`displays`, `list`) or `appControl`, and those redacted results are then sent like any other tool result. The same holds for the system-control tools: `inputControl clipboard_read` returns the redacted clipboard text (at most 4000 characters) and an `openPath` name lookup returns candidate file and folder names and paths only when Codex calls them; nothing is attached to a request in advance. The only desktop data attached is, in the turn's request JSON next to any shared dialogue ([spec](../src/jarvis/memory/desktop_referents.spec.md)): the window the user is looking at (`foreground_window`: application, process, window handle, display, state; never the title) on every request made at the PC, not from a phone, while `codex_share_foreground_window` is on (default); and Jarvis's own records while `codex_share_desktop_referents` is on (default): `desktop_referents` (application, process, window handle, display, zone, state, last action and age of at most five windows Jarvis acted on within the dialogue memory window) and `other_referents` (the device it controlled, the media player's application and status, the clipboard's content type; never track names or clipboard contents). A follow-up can therefore name the exact target with no dialogue shared. Sessions are ephemeral and the child keeps no message history; OpenAI's own retention is outside Jarvis. `activityLog` is not in Codex's tool snapshot unless `activity_log_share_with_cloud` is true; otherwise no activity data is ever sent. Screen contents are sent only when Codex calls `screenshot` for a request that asks about the screen ([spec](../src/jarvis/tools/builtin/screenshot.spec.md)): the redacted, fenced OCR text and one JPEG of the captured monitor or window (longer side at most 1568 px) added to the running turn with `turn/steer` right after that call's result, labelled as reference data; with `screen_awareness_enabled` off the tool is not in the snapshot and nothing of the screen is ever sent.
- **Limits**: `codex_timeout_sec` (90 s) from session start to a validated completion; `codex_max_tool_calls` (8) per request; one active plus `codex_queue_limit` (1) queued request; protocol message 1 MiB, tool arguments 8 KiB, tool result 4000 characters, reply 2000 characters.
- **Flow**: reply engine -> bridge service (query thread) -> `thread/start` (ephemeral, isolated, one dynamic tool; normally already started in the background as the spare thread, used when its isolation config still matches) and one `turn/start` carrying the whole request (redacted utterance, any shared dialogue and any shared desktop records, marked as reference data, as JSON in the input; the answer object as `outputSchema`; the allowed tools are listed in the thread's `jarvis_execute` definition) -> Codex calls `jarvis_execute` (run on the query thread through the central safety path) as needed -> its final message, the answer object, is delivered once, only after a matching successful `turn/completed`. A tool that needs confirmation returns the question at once, interrupts the turn and releases the query lock. Model round trips per request: one for a direct answer, one more per tool step.
- **Failure**: explicit message (missing executable, sign-in, model, effort, unsupported runtime, usage limit, service, process exit, deadline) and an offer to switch to local mode; never a silent fallback to the local model, an API key or a replay.

## 0c. Background Claude reply route (external, cloud, opt-in)

- **Files**: `src/jarvis/claude_bridge/` ([spec](../src/jarvis/claude_bridge/claude_bridge.spec.md)) on the shared bridge layer `src/jarvis/bridge/` ([spec](../src/jarvis/bridge/bridge.spec.md)). Not an `LLMBackend`: hidden, Jarvis-owned headless Claude Code processes (`claude -p`, stream-json in and out), one session per request, using the user's Claude subscription sign-in. No Claude window, API key, shim process or socket is involved; Jarvis's tools are an in-process MCP server on the same pipe.
- **Gating**: the active reply mode is `claude` (`bridge.modes`, started from `reply_mode` or switched at runtime by "use Claude" or the tray), which requires `claude_enabled` (default off; set in Settings or on the setup wizard's optional Cloud reply modes page). In `local` mode nothing here runs or is imported and no process starts. That wizard page runs only `lifecycle.check_sign_in` (`claude auth status`): no session, no inference and no request content.
- **Model**: `claude_model` (default `sonnet`) at `claude_effort` (default `low`; not sent to a model without effort levels), both checked against the models the CLI lists before use and never substituted. No local tier.
- **Engine position**: after redaction, pending confirmations and the fast-command path. Deterministic commands (including "go local") still run locally with no session.
- **Skipped and retained local contexts**: as for Codex (#0b).
- **Instruction source**: `src/jarvis/claude_bridge/prompts.py` (versioned, at most 2000 characters), sent as each session's system prompt (`--system-prompt`, replacing Claude Code's own). It holds no utterance, dialogue or configuration. Changes require an eval run (`evals/codex_runner.py --mode claude`) on the configured model with inert tools.
- **Transmitted to Anthropic through Claude Code**: the same as #0b under the `claude_*` sharing switches: the redacted utterance; up to `claude_recent_dialogue_messages` recent messages when `claude_share_recent_dialogue` is on; the foreground window on requests made at the PC while `claude_share_foreground_window` is on; Jarvis's desktop records while `claude_share_desktop_referents` is on; redacted, size-bounded results of tools Claude calls. Personal-data tools only with `claude_share_long_term_memory`. The child runs with analytics, error reporting, update checks and memory files off for that process only, no session persistence and no user settings. `activityLog` is likewise withheld from Claude's tool snapshot unless `activity_log_share_with_cloud` is true. Screen contents are sent as for Codex, only when Claude calls `screenshot`: the fenced OCR text and the image as an MCP `image` block of that call's result.
- **Limits**: `claude_timeout_sec` (90 s) from session start to a validated completion; `claude_max_tool_calls` (8) per request and `--max-turns` of that plus 3; one active plus `claude_queue_limit` (1) queued request; protocol message 1 MiB, tool arguments 8 KiB, tool result 4000 characters, reply 2000 characters.
- **Flow**: reply engine -> bridge service (query thread) -> a session started ahead of the request (the spare, used only when its tool snapshot matches and it has done nothing) or a new one: process start, `initialize`, in-process MCP `initialize` and `tools/list` (one tool, `jarvis_execute`, with the allowed tools' descriptions and argument schemas in its input schema, because Claude Code shows a model only the first 2048 characters of a tool description) -> one user message carrying the request JSON -> Claude calls `jarvis_execute` as `tools/call` control requests (run on the query thread through the central safety path) -> the `result` frame's structured answer (`--json-schema`) is delivered once, only when the turn succeeded. A tool that needs confirmation returns the question at once, interrupts the turn and ends the process. Model round trips per request: one for a direct answer, one more per tool step, plus the structured-answer step. Measured on Haiku while the PC was in use: 3.4 to 4.5 s for a direct reply or one tool call, about 13 s for two tool calls; session start 0.4 to 0.9 s, hidden by the spare.
- **Failure**: explicit message (missing CLI, not signed in, API-key sign-in, model, effort, usage limit, service, too many steps, process exit, deadline) and an offer to switch to local mode; never a silent fallback to the local model, an API key or a replay.

## 1. Main Reply Loop (agentic messages loop)

- **File**: [src/jarvis/reply/engine.py](src/jarvis/reply/engine.py) — `reply()` and the loop at ~lines 1370-1650; native tool-call path in `chat_with_messages()` (~1424, 1455).
- **Trigger**: every user message. Runs up to `agentic_max_turns` (default 8) iterations per reply.
- **Model / gating**: `cfg.llm_chat_model` via `get_llm_backend(cfg)`. Not optional. No size branching on the loop itself — size branching affects the digests/evaluator around it.
- **Inputs**:
  - Redacted user query
  - Recent dialogue (last 5 minutes), including in-loop tool-call + tool-role messages from prior replies within the active conversation (tool carryover, `DialogueMemory.record_tool_turn` / `get_recent_turns_with_tools` in [src/jarvis/memory/conversation.py](src/jarvis/memory/conversation.py); per-prompt cap via `cfg.tool_carryover_max_turns` / `tool_carryover_per_entry_chars`; storage cap `_tool_turns_max_storage = 16`; cleared on `stop` signal AND on new-conversation entry; UNTRUSTED WEB EXTRACT fence markers preserved on truncation; both `content` and `tool_calls[*].function.arguments` scrubbed on write)
  - Unified system prompt from [src/jarvis/system_prompt.py](src/jarvis/system_prompt.py) + ASR note + tool-protocol guidance
  - **Warm profile block** (query-agnostic User + Directives excerpt from the knowledge graph, composed by `build_warm_profile()` / `format_warm_profile_block()` in [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) at Step 3.5 of `reply()`; no LLM call, pure SQLite read; injected unconditionally so personalisation is the default; result cached in `DialogueMemory._hot_cache` under `DialogueMemory.WARM_PROFILE_CACHE_KEY` for the lifetime of the active conversation. Invalidated on `stop`, on new-conversation entry, AND on User/Directives graph mutations via the listener registered in [src/jarvis/daemon.py](src/jarvis/daemon.py) against `register_graph_mutation_listener` in [src/jarvis/memory/graph.py](src/jarvis/memory/graph.py); World-branch writes are ignored)
  - Digested memory enrichment (optional, see #4)
  - Time + location context (computed once per reply, placed at the END of the system message's dynamic region — never the head — so every in-loop call sends a byte-identical system message and the server's KV/prefix cache can reuse the whole prompt head; in text-tools mode it sits just before the tool-call syntax guidance so the instruction block stays final)
  - Tool schema: native via `generate_tools_json_schema()` ([src/jarvis/tools/registry.py](src/jarvis/tools/registry.py)) or text fallback via `_text_tool_call_guidance()` ([engine.py:68](src/jarvis/reply/engine.py:68))
  - Tool results from prior turns (raw or digested — see #5)
  - Tool images (`screenshot` only, [spec](../src/jarvis/tools/builtin/screenshot.spec.md)): kept on the tool-result message as internal `_images` and sent by `chat_with_messages()` as Ollama `images` only to a call whose model reports `vision` (`LLMBackend.supports_images`, one `POST /api/show` per model); never in tool carryover
  - **Desktop referents block** (`format_for_model()` in [src/jarvis/memory/desktop_referents.py](src/jarvis/memory/desktop_referents.py), [spec](../src/jarvis/memory/desktop_referents.spec.md)): the window the user is looking at (requests made at the PC only), the windows Jarvis itself opened, focused or placed within the dialogue memory window (application, process, window handle, display, zone, state), newest first, and the device, media player and clipboard type it last acted on, labelled reference data. No LLM call; OS calls are one bounded foreground read per request (0.5 s) and resolving a pending launch (one bounded window listing). Appended to the system message after the plan block and before the text-tool instructions; absent when there is no foreground window and Jarvis has not acted on anything. Recorded by `appControl` / `windowControl` on every route, including fast-path launches
- **Output**: OpenAI-style `{content, tool_calls, thinking}`. Consumed by the tool orchestrator and TTS pipeline. Natural-language content is delivered immediately; no post-turn evaluator runs. A tool call (model-emitted or direct-exec) that leaves a confirmation pending ends the loop at once: no further model turn runs, and the central gate's own question is the reply, word for word (see `reply.spec.md`, Action Safety).
- **Limits**: Backend-owned `llm_num_ctx` (default 8192). Timeout `llm_chat_timeout_sec` (45s). Auto-fallback from native to text tool-calls on HTTP 400 (`ToolsNotSupportedError`), sticky for the session. Risk: `fetch_web_page` truncates at 50,000 chars (~37k tokens) — mitigated for SMALL models by tool-result digest (#5) which compresses the payload before it enters the messages history. LARGE models receive the raw payload and may silently see a truncated context.
- **Text-chat entry**: The desktop `ChatWindow` (see `src/desktop_app/chat_window.spec.md`) submits via `jarvis.daemon.submit_text_query`, which calls this same context on a worker thread with `tts=None` and `language=None` (no Whisper-detected language for typed input). Voice and text share the global `DialogueMemory` so they are one conversation. No new LLM context is introduced — the planner, router, enrichment, and digests all run unchanged. Text chat never speaks; the reply is returned to the UI via callbacks (bundled) or `__CHAT__:` IPC events (subprocess). Opt-in phone access (`src/jarvis/remote/remote.spec.md`) is a second caller of `submit_text_query`: a paired phone's typed request enters this same context, and the phone reads the reply from the shared `DialogueMemory`.

## 1b. Tool Phase (tool-model mode, opt-in)

- **File**: [src/jarvis/reply/engine.py](src/jarvis/reply/engine.py) (the loop of #1, tool-model branch) and [src/jarvis/reply/tool_stage.py](src/jarvis/reply/tool_stage.py) ([spec](../src/jarvis/reply/reply.spec.md), Tool-Model Mode).
- **Trigger**: every local reply while `tool_model` is set (default empty: off, and this context never runs). It replaces the tool router (#7), the planner (#12), the plan step resolver (#13) and the memory extractor (#3) for that reply.
- **Model / gating**: TOOL tier — `resolve_model(cfg, Tier.TOOL)` (`cfg.tool_model`), native tool calling only.
- **Inputs**: `TOOL_STAGE_PROMPT` (about 730 characters; no persona, no warm profile), the time/location line, the desktop referents block, recent dialogue with tool carryover, the redacted query, and the whole builtin + MCP catalogue (minus `toolSearchTool` and `refreshMCPTools`) plus the engine-internal `recallMemory` tool.
- **Output**: tool calls, executed by the loop exactly as in #1 (central safety, confirmations, guards). A `recallMemory` call runs the diary and graph lookups with the model's own keywords and questions, no extractor call. The phase ends when the model answers without a tool call (its text becomes a note for the reply), after 6 tool calls, or on failure. A call that leaves a confirmation pending ends the reply with the gate's question instead, and no reply phase runs. Otherwise the loop's last turn runs on the chat model (#1) with the persona prompt, any recalled memory, a handover note and **no tools**.
- **Limits**: `llm_chat_timeout_sec`; at most `TOOL_PHASE_MAX_CALLS` (6) tool calls. Measured on the 28-case local sweep in `evals/codex_runner.py`: `gpt-oss:20b` for tools and replies passed 25/28, median 7.0 s per request (first tool 1.9 s), against 28/56 at about 2.4 s for `qwen3.5:9b` without tool mode. With `gpt-oss:20b` for tools and `qwen3.5:9b` for replies on a 12 GB GPU the two models reload on every request.

## 2. Intent Judge

- **File**: [src/jarvis/listening/intent_judge.py](src/jarvis/listening/intent_judge.py) — `IntentJudge.evaluate()`.
- **Trigger**: on a speech segment *only if* there is an engagement signal (wake word detected, hot-window active, or TTS playing). Pure ambient speech skips it, and so does an utterance the speaker gate ignored as not the owner's voice (`speaker_verification` `soft` or `strict`, an enrolled voice; the check is local and uses no LLM). An engaged stop command while a request is being collected or its reply generated is handled before the judge and never reaches it.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)` via `get_llm_backend(cfg).chat(...)`. Provider-aware default at config load: `qwen3.5:0.8b` on the Ollama chat path; on an OpenAI-compatible chat provider an unset `fast_model` resolves to the active `llm_chat_model` (the Ollama pull-name does not exist on the user's server). An explicit `fast_model` in config.json wins on both paths. The backend re-raises `ConnectionError` so the judge can apply a 30s cooldown after the server actively refuses; falls back to text-based wake detection while the cooldown is active.
- **Inputs**:
  - Rolling transcript buffer (last 120s, with timestamps)
  - Wake-word timestamp (if any), normalised aliases
  - Last TTS text + finish time (echo rejection)
  - State flags (wake_word_mode, hot_window_mode, during_tts)
- **System prompt**: `SYSTEM_PROMPT_TEMPLATE` at [intent_judge.py:135](src/jarvis/listening/intent_judge.py:135). Teaches query extraction, echo detection, stop commands, pronoun/topic disambiguation, imperative re-addressing, declaratives to the wake word.
- **Output**: strict JSON `IntentJudgment{directed, query, stop, confidence, reasoning}` ([intent_judge.py:94](src/jarvis/listening/intent_judge.py:94)). Consumed by the listening state machine, which collects the query until the pause after speech (`voice_collect_seconds`) and hands it to the serial reply worker. When `content` is empty **or truncated mid-JSON** (reasoning models count thinking tokens against the generation cap), the judge also recovers the JSON answer from `reasoning_content` — reasoning models typically end their thinking with the full structured answer.
- **Limits**: `intent_judge_timeout_sec` (6s). Backend-owned `llm_num_ctx` (default 8192). `max_tokens: 1500` is the generation cap. The Ollama backend supplies context size and residency (`llm_keep_alive`, default `30m`; `1m` in low-power mode); the judge supplies only generation settings.

## 3. Memory Enrichment Extractor

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `extract_search_params_for_memory()` (~line 71).
- **Trigger**: once per reply, **only when the pre-flight planner (#12) emitted a `searchMemory` directive or returned an empty plan (fail-open)**. Pure reply-only plans skip this entirely — saves one LLM call per greeting / small-talk turn.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)`. Factory-dispatched. Small classification task; rides the same small/warm model as the router. Silent empty-dict on failure (early-return when no chat model is configured — no wasted LLM round-trip).
- **Inputs**: user query (with the planner's `topic` hint appended when present), optional context hint (live-context compact summary) or UTC-now anchor, both carried in the USER message.
- **System prompt**: inline at [enrichment.py:35-63](src/jarvis/reply/enrichment.py:35). Byte-static — no hint block, no timestamp — so the system prompt is identical across every extractor call and stays cacheable; the per-call hint / UTC anchor rides at the end of the user content.
- **Output**: `{keywords, from?, to?, questions?}`. Consumed by memory search in the reply engine.
- **Limits**: timeout from `llm_routing_timeout_sec` (default 8 s). `max_tokens: 50`. A reply cut off by the cap keeps every field completed before the cut (small models often run on into optional `from`/`to` timestamps); a second attempt runs only when no complete keyword list can be recovered.
- **Caching**: result cached in `DialogueMemory._hot_cache` under key `enrichment:{redacted_query[+topic_hint]}` for the lifetime of the active conversation. Identical follow-ups within the same conversation reuse the dict and skip the LLM hop. Cleared by `clear_hot_cache()` on the `stop` signal and on new-conversation entry.

## 3b. Recall Gate (pre-enrichment short-circuit)

- **File**: [src/jarvis/memory/recall_gate.py](src/jarvis/memory/recall_gate.py) — `should_recall()`.
- **Trigger**: once per reply, before diary/graph/digest enrichment runs (after the planner has decided memory is potentially needed).
- **Model / gating**: NO LLM — deterministic keyword-coverage heuristic. Cheap.
- **Inputs**: query, recent dialogue (incl. tool carryover rows).
- **Output**: `False` only if hot-window contains a fresh tool result AND ≥50% of the query's content words appear in the hot-window transcript → skips diary, graph, and memory digest for this reply. Else `True`. Fail-open on any exception. Content-word extraction uses `\w{3,}` with `re.UNICODE`, so the gate works for Latin, Cyrillic, CJK, Arabic, Hebrew, etc. (per CLAUDE.md "no hardcoded language patterns"). Overlap words are run through `redact()` before being written to debug logs.
- **Planner precedence**: when the planner explicitly emitted a `searchMemory` step, the gate is bypassed — the planner has more signal than coverage and overriding it would silently drop intent. The gate only short-circuits the fail-open empty-plan path.
- **Rationale**: prevents re-running diary/graph lookups when the hot window already grounds the follow-up (e.g. "his most famous song" after a Bieber webSearch).

## 4. Memory Digest (optional, SMALL models)

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `digest_memory_for_query()` + `_distil_batch()`.
- **Trigger**: once per reply when enrichment returns hits AND `memory_digest_enabled` (default OFF; `null` = auto-ON for SMALL ≤7.5B / OFF for LARGE). Skipped if raw < `_DIGEST_MIN_CHARS` (400). Batched if raw > `_DIGEST_BATCH_MAX_CHARS` (2000).
- **Model / gating**: `cfg.llm_chat_model` via `get_llm_backend(cfg)`. Gated by `memory_digest_enabled`; the auto-on path reads the same chat model so model-size detection follows the active provider.
- **Inputs**: user query, raw diary entries, raw graph nodes.
- **System prompt**: `_DIGEST_SYSTEM_PROMPT` at [enrichment.py:122](src/jarvis/reply/enrichment.py:122). Teaches relevance filtering, preference-signal detection, attribution preservation, `NONE` sentinel, identity queries.
- **Output**: ≤400 chars text per batch (`_DIGEST_MAX_CHARS`) injected as reference-only memory context into the main loop's system message. Empty on failure.
- **Limits**: `llm_digest_timeout_sec` (8s, shared). `max_tokens: 200`.

## 5. Tool-Result Digest (optional, opt-in)

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `digest_tool_result_for_query()` + `_distil_tool_batch()`.
- **Trigger**: after each tool result in the loop, if `tool_result_digest_enabled` (default `null` = auto-ON for SMALL ≤7.5B, OFF for LARGE). Primary motivation on small models: prevents `fetch_web_page`'s 50k-char payloads from filling the 8192 num_ctx window. Skipped if raw < 400 chars (`_TOOL_DIGEST_MIN_CHARS`); batched if > 2500 (`_TOOL_DIGEST_BATCH_MAX_CHARS`).
- **Model / gating**: `cfg.llm_chat_model` via `get_llm_backend(cfg)`. Gated by `tool_result_digest_enabled` — auto-on for SMALL via `detect_model_size(cfg.llm_chat_model)`.
- **Inputs**: user query, tool name, raw tool result (e.g. webSearch payload inside UNTRUSTED WEB EXTRACT fence).
- **System prompt**: `_TOOL_DIGEST_SYSTEM_PROMPT`. Teaches attributed fact extraction, `NONE` sentinel, no inference.
- **Output**: ≤600 chars per batch (`_TOOL_DIGEST_MAX_CHARS`) replacing the raw payload in the messages stream. Falls back to raw on `NONE`.
- **Limits**: `llm_digest_timeout_sec` (8s, shared). `max_tokens: 300`.

## 6. Max-Turn Loop Digest

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `digest_loop_for_max_turns()` (~line 847).
- **Trigger**: when the loop exhausts `agentic_max_turns` without producing a natural-language reply (e.g. pure tool-call loop). The evaluator no longer drives this — termination on content is immediate.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)`. Factory-dispatched.
- **Inputs**: user query + loop activity (tool calls, results summaries, any prose).
- **System prompt**: `_LOOP_DIGEST_SYSTEM_PROMPT` — caveat-prefixed, user-language, concise.
- **Output**: caveat-prefixed final reply. Fails open to the last raw candidate or generic error.
- **Limits**: `llm_digest_timeout_sec` (8s, shared). `max_tokens: 200`.

## 7. Tool Router (pre-loop tool selection)

- **File**: [src/jarvis/tools/selection.py](src/jarvis/tools/selection.py) — `select_tools_with_llm()` (~line 331).
- **Trigger**: once per reply, **at the very front of the flow before the planner (#12)**. Runs on every local reply except in tool-model mode (#1b) — the router is the authoritative tool picker, and its narrowed catalogue is what the planner sees. When the planner later references tools, those names are unioned into the router's allow-list but never replace it; small models tend to default to `webSearch` where a dedicated tool like `getWeather` should win, and the router is tuned for that classification. `tool_selection_strategy == "llm"` is the default; other strategies (`all`, `keyword`, `embedding`) also run here.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)`. Factory-dispatched.
- **Inputs**: user query, tool catalogue (builtin, including the tools of any enabled local extension (`extensions/extensions.spec.md`), + MCP with descriptions), optional narrow-down hint (time/location facts, on Windows with Windows tools on the process name of the application the user is working in as a KNOWN FACTS line (`Foreground application (the window the user is working in): pdfeditor`; Jarvis's own windows skipped, read within 0.5 s, never the window title) so "find where it talks about soil preparation" with a PDF viewer in front routes to `pdfNavigate`, recent dialogue and, inside the dialogue section, the desktop referents block from #1, so "move it" after "open Word" routes to the window tools; while referents are live the engine also unions `windowControl` and `appControl` into the router's picks, never caching that overlay). User-prompt order is KV-cache-disciplined: the mostly-static catalogue opens, the dynamic hint (time + dialogue) follows, the query is the final token — consecutive router calls in one conversation share the full catalogue as prefix.
- **Windows tools**: the builtin catalogue includes `appControl`, `windowControl`, `openWebsite` and `openPath` when enabled on Windows. They flow through the same router, planner and chat tool loop. OS result strings (including window titles and paths) pass through `redact()` in the adapters before entering chat or tool-result digests. `windowControl` also exposes `displays` and `place`, and `appControl open`, `openWebsite` and `openPath` accept `monitor`, `zone` and `state` (an omitted monitor means the primary display); these are schema and action descriptions inside the existing tool definitions, with no separate model context. `openWebsite` is the only tool that opens web addresses (`openPath` refuses them) and places its new browser window itself, so "open YouTube on the right" is one tool call; the Codex (version 5) and Claude (version 2) instructions say so and call `windowControl displays` only when a display is named. Monitors can be a number, `primary`, `left` or `right`, and zones can be configured names or FancyZones names and numbers listed by `displays`; the tool resolves them from the structured arguments, so no extra prompt text or model call is involved. The router sees only each tool's top-level description, so `appControl`'s keeps its focus and switch wording (those requests stay routed to it) and names Steam games so game launches route to it. `systemSettings` (settings pages, brightness, power plan, audio output; [spec](../src/jarvis/platform/windows/windows.spec.md)) and `inputControl` (hotkeys, clipboard) are further builtin Windows tools with an `action` enum each, so the router's catalogue grows by two entries and their descriptions carry explicit "NOT for" pointers to `systemVolume`, `appControl` and `windowControl`; virtual desktop actions are extra actions inside `windowControl`, and `openPath` also finds a file or folder by name through a local index (no model call; [spec](../src/jarvis/platform/windows/apps_paths.spec.md)). All of them are deterministic fast-path families where the utterance is a whole routine command. `workspaceControl` (`open`, `list`; [spec](../src/jarvis/platform/windows/workspaces.spec.md)) is a separate tool registered only when the user has defined `windows_workspaces`, so those descriptions stay short and users without workspaces see no catalogue change. Workspace names are config data: the fast path opens them deterministically ("open my study workspace" is a `FAST_ROUTE`, no model call), and the model path calls `workspaceControl` with the name. It adds no model context. Its results carry item labels and outcomes only, never the paths or URLs in the configuration. `uiControl` and `pdfNavigate` ([spec](../src/jarvis/platform/windows/ui_automation.spec.md)) are two more catalogue entries with no model context of their own: `uiControl` returns a bounded UI Automation snapshot (at most 60 elements with short ids, names truncated, password values never read) or an action outcome, and `pdfNavigate` returns page numbers, outline titles and short text snippets read locally with `pypdf`. Choosing among candidate pages or controls is ordinary reply-model work over those tool results; nothing calls a model inside the tools. `activityLog` ([spec](../src/jarvis/memory/activity_log.spec.md)) is one more catalogue entry, registered only when the user has turned the opt-in activity log on; it returns raw per-application and per-title aggregates for an ISO range and has no model context of its own (the reply model turns "yesterday afternoon" into the range from the date and time it is already given). Its description carries "NOT for" pointers to `windowControl`, `systemInfo` and memory. `routineControl` ([spec](../src/jarvis/routines/routines.spec.md)) is one more catalogue entry, always registered and in every bridge snapshot, with no model context of its own: `run` executes a saved chain of tool calls through `run_tool_with_retries` as one call whose result is a report of step labels, positions, statuses and reasons (never step arguments, paths or web addresses; information results are not included), `list` and `recent` return routine names and step labels, and `save`, `delete` and `rename` build the change only from calls Jarvis executed (the in-memory journal) and always confirm with a full read-back. A change is refused after any MCP tool or a built-in tool that returns outside content ran earlier in the same request. Routine names are offered to the fast path, so "start movie mode" is a `FAST_ROUTE` with no model call. Run through a bridge, a routine may use only the tools in that request's snapshot.
- **TV tool**: `tvControl` ([spec](../src/jarvis/devices/roku.spec.md)) is one more builtin catalogue entry, registered only when `roku_host` is set to a private address, so users without a TV see no change. Its description opens with the TV and points to `systemVolume` and `mediaControl` for the computer so "turn the TV volume down" and "turn the volume down" route differently (`evals/test_tv_routing.py`). It has no model context of its own: actions are network calls to the TV, results (power state, active app, installed app names) are raw facts, and app-name resolution is deterministic. The `tv` fast-path family ("turn off the TV", "put on Netflix") is a `FAST_ROUTE`, no model call.
- **System prompt**: inline (~lines 260-315). Teaches pick up-to-5 tools or `none`.
- **Output**: comma-separated tool names or `none`. Capped at `_LLM_MAX_SELECTED` (5). Always-included tools (`stop`, `toolSearchTool`) are unioned in regardless.
- **Limits**: `llm_timeout_sec`, supplied from `cfg.llm_routing_timeout_sec` (default 8 s) by the engine and toolSearchTool. `max_tokens: 50`. On failure → all tools.
- **Caching**: `routed_tools` cached in `DialogueMemory._hot_cache` under key `router:{redacted_query}|{strategy}|{builtin-names}|{mcp-names}|fg:{foreground-process}` for the lifetime of the active conversation. The catalogue signature lets a mid-conversation MCP refresh invalidate the cache, and the foreground process is part of the key because it changes what "it" refers to; the rest of `context_hint` is intentionally excluded so time/location drift inside one conversation doesn't bust it. Cleared by `clear_hot_cache()` on the `stop` signal and on new-conversation entry.
- **Carry-over guard (engine-side overlay)**: after the cache lookup/write, the engine inspects the previous assistant turn's tool calls. When a previous tool reported `success=False` on its `ToolExecutionResult` (read via the `tool_failed` flag stamped onto each recorded tool result), that tool name is unioned back into the local `routed_tools` for this turn only. Compensates for small routers that misroute follow-ups where the user is supplying missing info (e.g. "I'm in London" routing to `webSearch` after a stalled `getWeather` chain). Successful chains do not carry over — a genuine new short ask after a completed chain keeps the router pick clean. The augmentation never touches the cache; replays of the same query in future turns get the raw router output. See `src/jarvis/reply/reply.spec.md` §6 (Tool allow-list per turn) for the full contract.

## 8. Tool Searcher (mid-loop escape hatch)

- **File**: [src/jarvis/tools/builtin/tool_search.py](src/jarvis/tools/builtin/tool_search.py) — `toolSearchTool`.
- **Trigger**: when the model explicitly invokes `toolSearchTool` during the loop. Capped at `tool_search_max_calls` (3) per reply.
- **Model**: reuses the tool router (#7) — no separate LLM call here.
- **Inputs**: self-contained query from the model.
- **Output**: newline-separated tool names + one-liners, merged into the allow-list for the next turn.

## 9. Conversation Summariser

- **File**: [src/jarvis/memory/conversation.py](src/jarvis/memory/conversation.py) — `generate_conversation_summary()` (~lines 350/355).
- **Trigger**: background, periodic when unsaved dialogue reaches `dialogue_memory_timeout`, plus the normal daemon shutdown path with `force=True`. One summary is stored per day per `source_app`.
- **Model / gating**: BACKGROUND tier — `resolve_model(cfg, Tier.BACKGROUND)` (the chat model in local reply mode, the fast model in Codex or Claude mode) via `get_llm_backend(cfg)`. Respects `llm_thinking_enabled`. Uses streaming when a token callback is provided, else direct.
- **Inputs**: recent conversation chunks + prior same-day summary (for incremental update). Turns that used the opt-in `activityLog` tool are excluded from the chunks (`DialogueMemory.add_message(..., diary=False)`), so activity data never reaches a summary or the graph built from summaries ([spec](../src/jarvis/memory/activity_log.spec.md)).
- **System prompt**: inline (~lines 310-320). Hygiene rules per [src/jarvis/memory/summariser.spec.md](src/jarvis/memory/summariser.spec.md): no deflection narration, attribution preservation, topic separation. The deflection rule (rule 6) is enumerated with concrete BAD/GOOD pairs in English plus parallel pairs in Turkish and Spanish so small models don't assume the rule is keyed to English phrasing. ≤200 words + 3-5 topic keywords.
- **Output**: `(summary_text, topics_text)` → `conversation_summaries` table, embedded for vector search, feeds enrichment (#3) and graph extraction (#10). No post-process scrub — the prompt is single-source-of-truth, language-agnostic, and improves automatically as the chat model upgrades.
- **Deflection rewrite (separate bulk op)**: `rewrite_all_diary_summaries()` (`POST /api/diary/scrub-deflections`) — cleans historical rows. One `cfg.llm_chat_model` call per row with `_REWRITE_DEFLECTION_SYSTEM_PROMPT`, asking the model to drop sentences that narrate the assistant's own failures while keeping everything else verbatim. Diary text is fenced as untrusted data (same fence used by the web tool). Preserves `ts_utc`; re-embeds updated rows best-effort via `get_embedding_backend(cfg)`. Empty-rewrite guard keeps the original if the model would have emptied the row. Fail-open at every layer (LLM call, write-back, embed). User-triggered from the Maintenance section in the diary sidebar.
- **Topic optimisation (separate bulk op)**: `optimise_diary_topics()` (`POST /api/diary/optimise-topics`) — collects all unique tags from `conversation_summaries`, makes one `cfg.llm_chat_model` call with `_TOPIC_OPTIMISE_SYSTEM_PROMPT` to propose a normalised taxonomy (merge synonyms, split compound tags), then applies the mapping to every row that needs updating. Preserves `ts_utc`; re-embeds updated rows best-effort. User-triggered from the Maintenance section in the diary sidebar.
- **Limits**: `timeout_sec` (30s default). `max_tokens: 400` on the direct (non-streaming) path so a full 200-word summary + TOPICS line is never truncated; the streaming path is uncapped.

## 10. Knowledge Graph Fact Extraction + Branch Classification

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) — `extract_graph_memories()`.
- **Trigger**: after each daily summary (#9). Background.
- **Model**: BACKGROUND tier — `resolve_model(cfg, Tier.BACKGROUND)` via `get_llm_backend(cfg)` when called from the diary flush; the memory viewer's user-run import passes the chat model.
- **Inputs**: summary text + optional date.
- **System prompt**: inline — asks for JSON array of `{"branch": "USER|DIRECTIVES|WORLD", "fact": "..."}` objects, with a heuristic ("user telling the assistant how to behave → DIRECTIVES; user telling the assistant about themselves → USER; external facts → WORLD"). Unknown branches default to USER. The DO-NOT-EXTRACT block hardens two recurring traps: assistant-generated recommendations (would-a-different-assistant-give-the-same-answer? heuristic separates these from external lookups, which DO count as facts) and transient snapshots like the current weather / time of day (described as "moments not facts" so the model stops conflating ephemera with persistent climate / location knowledge).
- **Output**: list of `(branch_id, fact_text)` tuples → routed into the tagged branch via branch-pinned descent (no cross-branch contamination).
- **Limits**: `timeout_sec`. Failures → empty list.

## 11. Knowledge Graph Best-Child Picker

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) — `_llm_pick_best_child()` (~line 167).
- **Trigger**: during graph insertion, per fact, to place it under the best existing category. Background.
- **Model**: `picker_model` when passed through from `update_graph_from_dialogue` (daemon resolves it via `resolve_model(cfg, Tier.FAST)` → small model when available); falls back to `cfg.llm_chat_model`. Factory-dispatched.
- **Inputs**: fact text + numbered list of candidate child nodes (name + description).
- **System prompt**: inline (~lines 156-161) — answer with number or `NONE`.
- **Output**: child node id or `None` (fact still inserted, just not under an optimal parent).

## 11b. Knowledge Graph Node Merge (rewrite-on-write consolidation)

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) — `merge_node_data()` (system prompt at `_MERGE_SYSTEM_PROMPT`).
- **Trigger**: **once per (node, flush)** during `update_graph_from_dialogue`. The orchestrator first applies the exact-match dedupe fast-path, then groups the remaining facts by their resolved `node_id` so a 5-fact flush hitting the User node fires one rewrite, not five. Cold-start writes (empty target node) skip straight to plain append. Also invoked with `new_facts=[]` by the `consolidate_all_populated_nodes` maintenance op (powering the memory viewer's 🧹 button) to re-apply current rules to historical data.
- **Model**: same `picker_model` as #11 (the fast tier when the caller resolves it, falling back to `cfg.llm_chat_model`). Factory-dispatched. Temperature 0 — the task is rule-following classification.
- **Inputs**: existing node `data` + the batch of new facts (zero or more) routed to that node in this flush.
- **System prompt**: defines an ordered rule set — contradiction/reversal drops the old version, near-duplicate phrasings collapse to one, repeated daily activities consolidate into patterns, independent attributes coexist (visible contradictions are NOT silently dropped), common-knowledge facts are pruned. Demands a bare `{"facts": [...]}` JSON object. Parser tries direct `json.loads` first, then a scoped regex (no greedy `\{.*\}`) before giving up.
- **Output**: `MergeResult(success: bool, incorporated_indices: list[int])`. The revised fact list is written back as the node's full `data`; `incorporated_indices` tells the orchestrator which inputs survived as new lines (under NFKC + casefold matching) so consolidated-out facts aren't reported as "newly stored". Subsumes per-flush supersession, near-duplicate dedupe, and ongoing consolidation in a single call. Because the latest prompt rewrites the whole node, updated conventions propagate to old data without a separate migration step.
- **Limits**: 20s timeout. **Hallucination guard**: rewrites with more than `len(existing) + len(new) + 2` lines are rejected as runaway output. Fail-open on any error, parse failure, oversized rewrite, or empty rewrite → caller falls back to plain `append_to_node` for each new fact so they still land (a contradiction is recoverable; a silent wipe or hallucinated bloat is not).

## 12. Task-list Planner (pre-flight decomposition, gates the whole turn)

- **File**: [src/jarvis/reply/planner.py](src/jarvis/reply/planner.py) — `plan_query()`.
- **Trigger**: once per reply, **after the tool router and before memory search**. Skipped in tool-model mode (#1b), when `cfg.planner_enabled = False`, when the query is shorter than `MIN_QUERY_CHARS` (4), when no model / base URL is available, or when the **engine-level fast-path skip** fires (the tool router returned no real tools AND the query is ≤ 8 words — the engine injects `["Reply to the user."]` as the plan without calling the LLM).
- **Model / gating**: CHAT tier — `resolve_model(cfg, Tier.CHAT)`. Factory-dispatched. The planner tracks the active chat model so upgrading it (via setup wizard, config, or provider switch) automatically upgrades plan quality.
- **Inputs**: user query, dialogue context (with the desktop referents block from #1 appended when present), **router-narrowed** tool catalogue (names + one-line descriptions) — not the full 30+ list. When the carry-over guard from #7 fires, the previous turn's failed tool name is unioned into this catalogue before the planner sees it, so the planner can plan a re-call without `toolSearchTool` round-tripping. **No** memory context — the planner decides *whether* memory is needed.
- **System prompt**: `_PROMPT_TEMPLATE` in `planner.py`. Teaches the `searchMemory topic='...'` directive for prior-conversation lookups, short imperative tool steps, angle-bracket entity placeholders, final synthesis step, same-language output, no numbering.
- **Output**: list of plan steps (max `MAX_STEPS` = 5). Gates memory enrichment (#3 / #4) and augments the tool router (#7 — planner's picks are unioned in, not replacing). Single-step `["Reply to the user."]` plans are the planner's positive "no memory, no tools" signal. An empty list is fail-open — the engine reverts to running #3 unconditionally. A **stop-only plan** (every step is `stop`) is also rejected by a deterministic post-plan guard and returns `[]` — same fail-open path as an LLM failure — so the engine falls through to the tool router and chat model rather than silently dismissing the conversation. Consumed further by the engine to build the `ACTION PLAN:` system-message block and drive the direct-exec loop (#13) for small models.
- **Limits**: `planner_timeout_sec` (3s). `max_tokens: 150`. Fail-open → `[]`.
- **Cache cost**: the planner shares the chat model, and Ollama keeps one prompt cache slot per loaded model, so a planner call evicts the main loop's cached prompt (see KV-cache discipline, slot sharing).

## 13. Plan Step Resolver (per direct-exec turn, small models)

- **File**: [src/jarvis/reply/planner.py](src/jarvis/reply/planner.py) — `resolve_next_tool_call()`.
- **Trigger**: top of each agentic-loop iteration when `use_text_tools` is True, the plan from #12 still has unexecuted tool steps, AND the plan is not under-specified (`plan_has_unresolved_tool_steps` returns False — steps that paraphrase tools without naming them skip direct-exec so the resolver doesn't guess arguments). Runs instead of the chat model for that turn. **Fast path skips the LLM entirely** when the step is fully concrete (tool name + `key='value'` args, no `<placeholder>`); the LLM call only fires when entity substitution or key remapping is needed.
- **Model**: same chain as #12.
- **Inputs**: next planned step text, prior tool calls (name + args + result excerpt), per-turn tool schema.
- **System prompt**: `_STEP_RESOLVER_SYSTEM` at [planner.py:300](src/jarvis/reply/planner.py:300). Teaches one-JSON-object output, placeholder substitution from prior results, `null` for synthesis steps.
- **Output**: `(tool_name, arguments)` tuple or `None`. Unknown tool names are rejected via the allow-list guard.
- **Limits**: `planner_timeout_sec` (3s). `max_tokens: 100`. Fail-open → `None` (engine falls back to the chat-model turn).

## 14. Tool-specific LLM calls

- **Weather** ([src/jarvis/tools/builtin/weather.py](src/jarvis/tools/builtin/weather.py), ~line 60) — factory-dispatched. Place extraction is a FAST-tier pass (`resolve_model(cfg, Tier.FAST)`) so small/warm models handle the parse without paging in the chat model. `max_tokens: 50`. Parses location/time/unit from the query.
- **Nutrition log_meal** ([src/jarvis/tools/builtin/nutrition/log_meal.py](src/jarvis/tools/builtin/nutrition/log_meal.py), lines 48 & 136) — factory-dispatched. Both the nutrition extractor and the follow-up generator use the BACKGROUND tier (`resolve_model(cfg, Tier.BACKGROUND)`: the chat model in local reply mode, the fast model in Codex or Claude mode).
- **Dictation filler removal** ([src/jarvis/dictation/dictation_engine.py](src/jarvis/dictation/dictation_engine.py), `_llm_clean_dictation`) — factory-dispatched, only while `dictation_filler_removal` is on. BACKGROUND tier. 5 s timeout, `max_tokens` about half the input length (floor 64). Falls back to the raw transcription. Extractor `max_tokens: 200`, follow-up `max_tokens: 100`. Extracts nutrients, confirms logging.

## 15. Server Capability Probe (setup-time, OpenAI-compatible only)

- **File**: [src/jarvis/llm/openai_compatible.py](src/jarvis/llm/openai_compatible.py) — `OpenAICompatibleBackend.check_capabilities()`. Called from the setup wizard's `_CapabilityWorker` ([src/desktop_app/setup_wizard.py](src/desktop_app/setup_wizard.py)).
- **Trigger**: not part of the runtime pipeline. Fires when the user clicks **Connect** on the OpenAI-compatible wizard page (once per connection attempt). The desktop startup reachability check (`_check_openai_compat_reachable` in [src/desktop_app/app.py](src/desktop_app/app.py)) uses only `list_models`, not this probe.
- **Model / gating**: the chat model the user selected on the page (and the selected embedding model, if any). Off the UI thread.
- **Inputs**: a fixed `"ping"` message; a trivial no-op tool schema; a `"ping"` embedding input. No user or memory data.
- **Output**: `ServerCapabilities{reachable, chat, tools, embeddings, models}`. Consumed only by the wizard to render an honest capability summary and offer the Ollama-embeddings fallback. Never persisted.
- **Limits**: `timeout_sec` default 8s per sub-request. Issues up to two `/chat/completions` calls (plain + tool), one `/embeddings`, one `/models`. Fail-soft: every error collapses to a `False` flag; a `ConnectionError` short-circuits to `reachable=False`.

---

## Frequency / Size Summary

| # | Context | Per reply | Optional? | Model tier |
|---|---------|-----------|-----------|------------|
| 1 | Main chat loop | 1-8 | No | LARGE |
| 2 | Intent judge | 1 (voice only) | fallback available | SMALL |
| 3 | Memory enrichment extract | 0-1 | gated by planner | SMALL (FAST tier) |
| 4 | Memory digest | 0-N | auto by size | SMALL (uses chat model) |
| 5 | Tool-result digest | 0-N | auto by size | SMALL (uses chat model) |
| 6 | Max-turn digest | 0-1 | No | SMALL |
| 7 | Tool router | 1 | always runs; planner picks unioned in | SMALL |
| 8 | Tool searcher | 0-3 | model-initiated | SMALL (reuses #7) |
| 9 | Summariser | ~1/session | No (background) | LARGE |
| 10 | Graph extraction | ~1/session | No (background) | LARGE |
| 11 | Graph best-child | 0-N | No (background) | SMALL (FAST tier) |
| 11b | Graph node merge | 0-N (per node, batched) | No (background) | SMALL (FAST tier) |
| 12 | Planner (plan_query) | 1 | yes (planner_enabled) | LARGE/SMALL (tracks chat model) |
| 13 | Plan step resolver | 0-N (SMALL only) | auto by size + plan | tracks chat model (CHAT tier; runs only when that model is SMALL) |
| 14 | Tool-specific | per-tool | n/a | LARGE |
| 0b | Background Codex reply (external) | 1 session per reply in codex mode | opt-in (`reply_mode`) | Cloud, via the Codex app-server (no local tier) |

## Size-aware auto switches

Driven by `detect_model_size(model_name) → SMALL (≤7.5B) | LARGE (>7.5B)` — uses a regex to extract the parameter count from the model name, handles MoE (`8x7b`) as LARGE, and defaults bare `gemma4` names (no size tag) to SMALL while sized variants (e.g. `gemma4:12b`) follow the threshold:

| Feature | SMALL | LARGE |
|---------|-------|-------|
| Memory digest | ON | OFF |
| Tool-result digest | ON | OFF |
| Text-based tool calling | ON | OFF (native) |
| Planner direct-exec | ON | OFF |

## Config keys

- Models: `llm_chat_model` (CHAT tier), `fast_model` (FAST tier), `tool_model` (TOOL tier, opt-in, empty = off; see #1b). The BACKGROUND tier reads `llm_chat_model` in local reply mode and `fast_model` in a cloud reply mode. Every context resolves via `resolve_model(cfg, tier)`. Legacy on-disk keys (`ollama_chat_model` as a v1 → v2 alias; `intent_judge_model` / `tool_router_model` / `evaluator_model` / `planner_model` folded into `fast_model` by the v2 → v3 migration) are readable but no longer part of `Settings`.
- Flags: `memory_digest_enabled`, `tool_result_digest_enabled`, `llm_thinking_enabled`, `intent_judge_thinking_enabled`, `tool_selection_strategy`, `low_power_mode`, `llm_keep_alive`, `screen_awareness_enabled` (whether `screenshot` is offered at all, so whether screen text and images can enter #0b, #0c, #1 and #1b)
- Timeouts: `llm_chat_timeout_sec` (45s), `llm_digest_timeout_sec` (8s, shared across #4/#5/#6), `llm_routing_timeout_sec` (8s, routing/extraction/warmup), `llm_tools_timeout_sec` (300s, tool execution), `intent_judge_timeout_sec` (6s), `planner_timeout_sec` (3s)
- Caps: `agentic_max_turns` (8), `tool_search_max_calls` (3), `_LLM_MAX_SELECTED` (5), `_DIGEST_MAX_CHARS` (400), `_TOOL_DIGEST_MAX_CHARS` (600). Per-context `max_tokens` caps listed above (50–1500 depending on task — the intent judge's 1500 covers reasoning + answer on reasoning models; rewrite tasks scale with input length).
- Runtime residency: every Ollama inference request carries `keep_alive` from `llm_keep_alive` (default `"30m"`). `low_power_mode` skips startup LLM warmups and shortens it to `"1m"`. Neither changes prompts, model selection, timeouts, or context limits.

## KV-cache discipline (prompt construction rules)

Every context is built against servers (Ollama, vLLM, SGLang, llama.cpp `llama-server`, LM Studio) that reuse the KV state of the longest matching prompt prefix. The first diverging token decides how much compute is saved, so these rules are load-bearing:

1. **System prompts are byte-static** — no timestamps, hints, or per-call data inside. Per-call data (time, location, dialogue) lives in the user message.
2. **Dynamic blocks go to the tail** — anything that changes per call (context line, hint blocks) is appended at the END of its message, never at the head.
3. **Stable-before-dynamic ordering** — the mostly-static block (persona, tool catalogue) opens the prompt; per-query blocks (digest, plan, hint) follow; the user query is the final token.
4. **Per-reply memoisation** — the main loop's time/location context string is computed once per reply, so all in-loop calls of one reply are byte-identical from token 1; the KV prefix extends through the whole history, not just the system message.
5. **Ollama payloads set `cache_prompt: true` explicitly** on `chat()`, `direct()`, and `streaming()` so the server always retains the request's KV state.

Anything that reorders messages between calls, injects a changing value at the head of a prompt, or rebuilds a system prompt with per-call content breaks prefix reuse for every token after the divergence point.

**Slot sharing.** Ollama serves each loaded model with one prompt cache slot by default, so prefix reuse only spans consecutive calls to the same model. A different context on the same model in between (the planner #12 on the CHAT tier, a digest on the chat model) replaces the slot, and the next main-loop call prefills its whole prompt again. Measured on 2026-10-04 with `qwen3.5:9b` as CHAT on an RTX 5070: the main-loop call after a planner call prefilled about 3,250 tokens in 880 to 1,090 ms; after a previous main-loop call it took 37 to 320 ms. The `qwen3.5` hybrid models reuse a prefix only from saved checkpoints, so an identical prompt is a full hit but a prompt that diverges late still costs a partial prefill. Raising `OLLAMA_NUM_PARALLEL` gives the server more slots at the cost of KV memory per slot; it is a server setting outside Jarvis and has not been measured here.

## Flow

```
user input
  └─▶ [2] Intent Judge            (voice only, SMALL)
        └─▶ [7] Tool router (narrows catalogue for the planner)
              └─▶ [12] Planner (gates memory; advisory for the router allow-list)
                    ├─ plan requests searchMemory  → [3] Enrichment extract → [4] Memory digest (optional)
                    ├─ plan empty (fail-open)      → [3] Enrichment extract → [4] Memory digest
                    └─ plan reply-only             → skip #3 and #4 entirely
                    └─▶ AGENTIC LOOP  (≤ agentic_max_turns)
                                      ├─ [13] Plan step resolver (SMALL, direct-exec)
                                      ├─ [1] Main chat turn
                                      ├─ tool execution
                                      │    └─ confirmation pending → its question is the reply (loop ends)
                                      │    └─ [5] Tool-result digest (optional)
                                      │    └─ [8] Tool searcher (model-initiated)
                                      └─ content → deliver immediately
                                      └─ if max turns → [6] Max-turn digest
                          └─▶ TTS / output
                          └─▶ background: [9] summariser → [10] graph extract → [11] best-child
```

## Optimisation ideas (seed list)

1. Batch multi-chunk memory digests (#4) into a single call with explicit markers.
2. Parallelise multiple tool-result digests (#5) when several results land at once.
3. Pre-warm the intent-judge model before TTS finishes.
4. Cache tool-router (#7) output by query hash.
5. Give each digest its own timeout budget rather than sharing `llm_digest_timeout_sec` (today a slow memory digest can starve the max-turn digest).
6. Consider single-model deployments: the FAST tier prefers a small dedicated model while the planner tracks `llm_chat_model`; loading a second model hurts cold-start latency on small hardware. (On an OpenAI-compatible chat provider an unset `fast_model` already resolves to the chat model, so every context rides the one served model.)
7. Narrow `llm_thinking_enabled` to router/planner only, not every context.
8. `intent_judge_timeout_sec` was already reduced from 15s → 6s. Consider racing it against text-based wake detection to avoid blocking the audio loop entirely.

## 21. Model warm-up probe (OpenAI-compatible path)

- **Source**: `src/jarvis/llm/openai_compatible.py` — `warm_up()` (Phase 2)
- **Trigger**: once per model (chat, judge, router) at listener startup, with the chat model only in local reply mode, run in parallel daemon threads; the embed model takes a separate path via `backend.embed()` (see Notes)
- **Model / gating**: the model being warmed, via direct `requests.post` (not via `chat()`, no response parsing). The two-phase probe (GET /models + POST /chat/completions) is specific to the `openai_compatible` provider; Ollama checks `/api/version` then uses a one-token `POST /api/chat` probe.
- **What is sent**: a fixed `{"role": "user", "content": "ping"}` message, `max_tokens=1`, `stream=False`
- **Gating**: startup skips LLM probes in low-power mode. Otherwise, configured chat and judge models are probed; the router participates when LLM tool selection is enabled. Roles sharing a model share a probe. Ollama sends its chat probe with the same backend-owned `num_ctx`, `keep_alive` and default `think` as live requests. Inference probes use `llm_routing_timeout_sec` (8 s default); embedding probes retain their separate budget.
- **Output**: `True`/`False`, reported as model warmup probe passed/failed, grouped by shared chat/judge/router model. Full requests and their deadlines are explicitly not tested. The configured intent deadline is shown separately; warmup success is not a guarantee that a full intent request will meet it.
- **Limits**: preceded by `GET /models` (Phase 1, capped at 25 % of budget, max 5 s); Phase 2 timeout is `max(0.1, timeout_sec - list_to)` with caller default 60 s. The embed warmup path does not use this two-phase split — it passes the full timeout directly to `backend.embed()`.
- **Data flow**: `warm_up()` → raw `requests.post` → `resp.ok` (any 2xx) → `bool` returned to `_start_llm_warmup()` → listener startup print
- **Notes**: Best-effort and non-blocking. A failed warmup never prevents the listener from starting. The Ollama probe performs minimal inference, but neither provider's probe exercises the full intent prompt or router tool selection.

    The **embed warmup** is a separate path: `listener.py:_start_llm_warmup` calls `backend.embed("ping", embed_model)` instead of `backend.warm_up()`. This is because embedding-only models (e.g. nomic-embed-text, modernbert) are not served on the `/chat/completions` endpoint. The embed probe uses the provider's embedding endpoint with a single-token input and has no Phase 1 reachability check. It runs independently even when the embedding model name matches chat, since chat success cannot prove embedding support or connectivity to a separately configured embedding provider.


## 21b. Local model release on leaving local mode (Ollama)

- **Source**: `src/jarvis/bridge/modes.py` — `_release_local_models()`, via `LLMBackend.release()` in `src/jarvis/llm/ollama.py`
- **Trigger**: a successful switch from local to Codex or Claude, on a background `reply-model-release` thread
- **Model / gating**: `llm.tiers.local_reply_models(cfg)`: the CHAT and TOOL tier models, less any model the FAST tier or `embedding_model` uses. Only models `GET /api/ps` lists as loaded are released; OpenAI-compatible backends send nothing.
- **What is sent**: `POST /api/chat` with the model name, no messages and `keep_alive: 0` (no prompt, no user data)
- **Output**: `True` when unloaded, printed as `🧹 Unloaded local model '…'`; failures are logged and never affect the switch
- **Limits**: 5 s per request
- **Data flow**: `modes.switch()` → `_release_local_models()` → `backend.release(model)` → console line

---

## Measuring

`tests/performance/test_pipeline_timings.py` times each context in this graph against a live Ollama. Run:

```
pytest tests/performance/ -v -m performance -s
```

It records per-context p50/p95 latencies using a monkey-patch recorder that infers the context from the caller's `__qualname__` (see `_CALLER_TO_CONTEXT` in `tests/performance/timing_recorder.py`). Dumps a JSON report to `tests/performance/reports/`. A micro-benchmark with a tiny fixed prompt runs alongside to give a per-call floor — if that floor moves, every context's total moves with it, so hardware/model drift is visible immediately.

Baseline on a local gemma4:e2b (as of 2026-04-22, 3 queries × 3 runs): main chat turn p50 ~4.5s, enrichment extract p50 ~0.9s (small-model chain), micro-prompt floor ~0.15s. Sample sizes: main 25 calls, enrichment 9. Use these as rough reference points — the assertions in the test are relative-shape (router ≤ 1.5× main chat turn), not absolute.

When you add or change a context, update `_CALLER_TO_CONTEXT` so it shows up in the report instead of landing in the `other:` bucket.

## Keep this doc in sync

This graph is the reference for LLM-latency optimisation. Treat it as authoritative: whenever code changes affect an LLM call — a new context, a removed one, a changed model/timeout/cap/gating/prompt source, or a new data-flow edge — update this file in the same PR. If the update would be more than a one-line tweak, reflect it in the relevant `*.spec.md` too.

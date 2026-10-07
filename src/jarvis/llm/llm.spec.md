# LLM Backend Specification

The `jarvis.llm` package owns every LLM HTTP call Jarvis makes and lets the same reply engine, planner, intent judge, evaluator, memory pipeline, and tools run against any local runtime: Ollama or an OpenAI-compatible server (LM Studio, oMLX, llama.cpp's `llama-server`, vLLM, LocalAI).

The optional background Codex reply mode is not an LLM backend: it hands a whole request to a separately installed agent and never passes through this package (see `codex_bridge/codex_bridge.spec.md`). Every context in this spec, including the intent judge, embeddings and memory work, stays on the configured backend in every reply mode.

## Goals

1. **Pluggable.** New backends drop in by subclassing `LLMBackend` and being registered in `factory.get_llm_backend`. Call sites stay unchanged.
2. **Privacy-first.** Backends never send data anywhere unless the user has explicitly configured the URL. Defaults remain `127.0.0.1:11434`.
3. **Single source of truth.** Every call site dispatches through `get_llm_backend(cfg)` / `get_embedding_backend(cfg)`. The `Settings` object carries provider, base URL, API key, and model fields; the factory reads them.

## Public surface

```python
from jarvis.llm import (
    LLMBackend,                  # provider-agnostic ABC
    OllamaBackend,               # implementation: Ollama
    OpenAICompatibleBackend,     # implementation: OpenAI-compatible servers
    ToolsNotSupportedError,
    get_llm_backend,             # factory: settings → chat backend
    get_embedding_backend,       # factory: settings → embedding backend
    check_version,               # verify a URL points to a live Ollama server
    call_llm_direct,             # base-URL helper (see below)
    call_llm_streaming,
    chat_with_messages,
    extract_text_from_response,
)
```

Two interchangeable styles dispatch to the same backend:

- **Object-style** (preferred): `get_llm_backend(cfg).direct(...)`. The factory dispatches on `cfg.llm_provider` so swapping providers does not touch call sites. Every site under `src/jarvis/` uses this.
- **Function-style**: `call_llm_direct(base_url, ...)`. A thin wrapper that constructs an `OllamaBackend(base_url)` and delegates. Used by the performance-recording shims in `tests/performance/` and the eval scripts under `evals/`, where only a base URL is in scope.

## `LLMBackend` interface

| Method | Returns | Contract |
|--------|---------|----------|
| `direct(model, system, user, *, timeout_sec, thinking, temperature, max_tokens)` | `Optional[str]` | Single-shot system+user. Returns assistant text, or `None` on timeout / error / empty content. `max_tokens` caps generation length — essential for small reasoning models on classification tasks. |
| `streaming(model, system, user, *, on_token, timeout_sec, thinking)` | `Optional[str]` | Streams tokens via `on_token`; returns the concatenated full text or `None` if no content was produced. |
| `chat(model, messages, *, timeout_sec, extra_options, tools, thinking)` | `Optional[Dict]` | Arbitrary messages array. Returns the raw response dict so callers (today: the reply engine) can inspect `content` and `tool_calls`. Raises `ToolsNotSupportedError` when the model rejects native tools. Re-raises `requests.ConnectionError` so callers can distinguish "server unreachable" from a transient HTTP failure. |
| `embed(text, model, *, timeout_sec)` | `Optional[List[float]]` | Vector embedding. Returns `None` on error or when the runtime does not expose embeddings. |
| `supports_images(model)` | `bool` | Whether the model can see images on chat messages. The `LLMBackend` default is `False`, so images are never sent where they would be rejected or ignored. `OllamaBackend` reads the model's `capabilities` from `POST /api/show` (`vision`) once per server and model; an unreachable server or unknown model answers `False` and is asked again next time. `OpenAICompatibleBackend` keeps the default. |
| `list_models(*, timeout_sec)` | `List[str]` | Names of models the runtime has available. Returns `[]` on error. |
| `release(model, *, timeout_sec)` | `bool` | Unload a model that is resident now, never loading one that is not; True when the runtime unloaded it. The `LLMBackend` default returns `False` without a request (runtimes that manage residency themselves, such as OpenAI-compatible servers). Used when leaving local reply mode (`bridge/bridge.spec.md`). |
| `warm_up(model, *, timeout_sec)` | `bool` | Pre-load probe before the first real request. The `LLMBackend` default returns `True` (no-op for runtimes without a useful probe). `OllamaBackend` verifies the server is Ollama via `GET /api/version`, then issues a minimal `/api/chat` completion with the backend-owned `keep_alive` duration (`llm_keep_alive`, default `"30m"`; `"1m"` in low-power mode) to page the model into resident memory **and** trigger full inference-pipeline initialisation (JIT compilation, KV-cache allocation) — the chat-endpoint warmup prevents the timeout that an empty `/api/generate` ping would mask on the first real call. `OpenAICompatibleBackend` first runs a fast reachability check (`GET /models`, 25 % of budget, max 5 s), then sends a single-token chat completion (`max_tokens=1`) to force the runtime to load the model into memory. |

`direct()` and `streaming()` are convenience methods over `chat()`: they construct the `[system, user]` messages array internally so callers running classification-shaped passes (planner, intent judge, evaluator, enrichment extractor) do not have to. `chat()` is the low-level primitive for arbitrary message arrays — multi-turn dialogue, native tool calls, and anything that needs custom roles.

### Tool calling

The `tools` parameter accepts the OpenAI-compatible JSON-schema format produced by `jarvis.tools.registry.generate_tools_json_schema()`. Ollama 0.4+ adopts that exact format, so no translation layer is needed for the Ollama backend; the OpenAI-compatible backend translates inside its `chat()` method so the reply engine sees a single shape.

When a model rejects the `tools` parameter (Ollama returns HTTP 400 in that case), the backend raises `ToolsNotSupportedError`. The reply engine catches it and falls back to text-based tool calling for the rest of the session.

### Streaming

Each backend parses its own stream format internally (Ollama JSONL, OpenAI SSE). The public `on_token(str)` contract is identical across backends.

### Cancelling a chat call

`chat_cancellable(model, messages, cancel, ...)` is `chat` that ends with `RequestCancelled` as soon as the `cancel` event is set; with the event already set it never contacts the server. `RequestCancelled` is a `BaseException`, so the reply engine's `except Exception` guards cannot swallow a Stop.

- `OllamaBackend` streams the request (`/api/chat` with `stream: true`) on a helper thread and puts the pieces back together as the single response `chat` returns: the content and thinking joined, the tool calls collected, the closing chunk's fields kept. `timeout_sec` bounds the whole call, as it does for `chat`; failures map as in `chat` (a timeout or server error gives `None`, an unreachable server raises, a model that rejects `tools` raises `ToolsNotSupportedError`). On cancel the connection is closed with a reset at once, which makes Ollama stop generating, and the caller is released without waiting for the helper thread.
- Every other backend inherits the default, which cannot drop a request already sent: it runs `chat` on a helper thread and stops waiting for it, so the caller is free at once and the server finishes its answer for nobody.

Only the reply engine's chat call uses it, and only for a request that has a Stop signal (`reply/reply.spec.md`, Stopping a reply); every other call is unchanged.

### Embeddings

`embed()` is part of the same backend interface so the same provider can serve both chat and embeddings when capable. The `embedding_provider` config key lets users on runtimes without embeddings (e.g. some oMLX builds) route embeddings through Ollama while keeping chat on their preferred runtime. Every embedding call site under `src/jarvis/` resolves through `get_embedding_backend(cfg)`.

## Configuration

Provider-aware fields in `Settings` (see [src/jarvis/config.py](../config.py)):

| Key | Default | Meaning |
|-----|---------|---------|
| `llm_provider` | `"ollama"` | `"ollama"` or `"openai_compatible"`. Unknown values fall back to `"ollama"`. |
| `llm_base_url` | (OpenAI-compatible only) | The OpenAI-compatible server's URL, e.g. `http://localhost:1234/v1` (LM Studio default). Read only when `llm_provider == openai_compatible`; the Ollama path always uses `ollama_base_url`. |
| `llm_api_key` | `""` | Optional bearer token. Sent only when non-empty. Kept in the OS credential store (`jarvis.credentials`), not in `config.json`; `load_settings` reads it from there |
| `llm_chat_model` | (OpenAI-compatible only) | The model name the OpenAI-compatible server exposes. Read only when `llm_provider == openai_compatible` (falling back to `ollama_chat_model` if blank); the Ollama path uses `ollama_chat_model`. |
| `embedding_provider` | inherits `llm_provider` | `"ollama"` / `"openai_compatible"`. Override for runtimes without embeddings. |
| `embedding_base_url` | inherits from llm config | Override per-provider URL. |
| `embedding_api_key` | inherits `llm_api_key` | Override per-provider key. Kept in the credential store like `llm_api_key` |
| `embedding_model` | (OpenAI-compatible only) | The OpenAI-compatible embedding model. Read only when the effective embedding provider is `openai_compatible` (falling back to `ollama_embed_model` if blank); the Ollama path uses `ollama_embed_model`. |
| `llm_keep_alive` | `"30m"` | How long Ollama keeps a model loaded after a request: a duration in Go's `time.ParseDuration` form, which Ollama parses (`"30m"`, `"2h"`, `"1h30m"`, `"1.5h"`), or whole seconds (`-1` keeps it loaded, `0` unloads at once). Anything else (`"30 m"`, `"1e3s"`) would make Ollama refuse every request, so it uses the default. |
| `low_power_mode` | `false` | When enabled, voice startup skips LLM warmup and every Ollama request uses a `1m` keep-alive instead of `llm_keep_alive`. |

The `ollama_base_url` / `ollama_chat_model` / `ollama_embed_model` keys hold the Ollama configuration and are authoritative whenever the active (chat or embedding) provider is Ollama. `_load_settings` resolves `cfg.llm_chat_model`, `cfg.embedding_model`, and `cfg.fast_model` per-provider — the Ollama keys win on the Ollama path, the provider-aware keys win on the OpenAI-compatible path — so the codebase reads a single resolved field while each provider keeps its own on-disk model name. The v1 → v2 migration promotes any explicitly-set `ollama_*` values into the provider-aware keys; per-provider resolution means a promoted value never shadows the Ollama picker. The v2 → v3 migration folds the retired per-context model keys (`intent_judge_model`, `tool_router_model`, `evaluator_model`, `planner_model`) into `fast_model` (an explicitly chosen judge or router model is kept; the old default value is not pinned).

### Model tiers

Every LLM context runs on one of two models, or a third when the opt-in tool model is set, resolved through `resolve_model(cfg, tier)` in `tiers.py`:

| Tier | Field | Contexts | Default |
|------|-------|----------|---------|
| `Tier.FAST` | `cfg.fast_model` | intent judge, tool router, tool searcher, enrichment extractor, graph placement, max-turn digest, evaluator | `qwen3.5:0.8b` on the Ollama chat path; the active chat model on an OpenAI-compatible provider (the Ollama pull-name does not exist there) |
| `Tier.CHAT` | `cfg.llm_chat_model` | main reply loop, planner + plan-step resolver, memory/tool-result digests (size-gated passes on the chat model), user-run diary maintenance (rewrite sweep, topic optimisation) | `qwen3.5:9b` unless the user picks another at setup |
| `Tier.BACKGROUND` | `cfg.llm_chat_model` in local reply mode, `cfg.fast_model` while Codex or Claude writes replies (`bridge.modes.active_mode()`) | conversation summariser, graph fact extraction, `logMeal` extraction and follow-ups, dictation filler removal: local work that runs whatever the reply mode, so a cloud session never pages in the chat model | follows the two fields |
| `Tier.TOOL` | `cfg.tool_model` | the tool phase of a local reply in tool-model mode (`reply/reply.spec.md`, Tool-Model Mode) | empty: off, and no context runs on it |

`cfg.llm_chat_model` can change while Jarvis runs: on the Ollama provider the web chat's model picker calls `daemon.set_local_chat_model`, which saves `ollama_chat_model`, replaces the daemon's and the voice listener's `Settings` (frozen, so by replacement), warms the new model and releases the old one unless the fast tier, the tool model or embeddings share it (`webchat/webchat.spec.md`, Models). Contexts keep reading the field, so they pick the new model up on their next call.

Fast-tier contexts take a few thousand tokens in and emit tiny strict-JSON answers, so latency dominates; chat-tier contexts produce long-form output, so quality dominates. Contexts state their tier instead of defining a per-context fallback chain, and any future routing logic lands in exactly one place.

### Request-shape ownership

Load-affecting options belong to the backend (and, once model targets exist, to the target), not to call sites. For Ollama these are `num_ctx`, `keep_alive` and the default `think` value. They come from configuration (`llm_num_ctx`, default 8192; `llm_keep_alive`, or `1m` in low-power mode) and are identical for every request to a given model, including `warm_up`. Ollama reloads a model when `num_ctx` changes between requests, so alternating context sizes against one model costs a reload per switch. Call sites state only task options (`max_tokens`, `temperature`, `thinking`). A FAST and a CHAT model may use different context sizes provided each is stable for its own model.

### Timeouts

LLM routing calls (`llm_routing_timeout_sec`, default 8 s) are separate from tool execution (`llm_tools_timeout_sec`) and from chat generation (`llm_chat_timeout_sec`, default 45 s). Explicit user values override the defaults. Routing includes the tool router, memory search extractor, toolSearchTool and listener inference warmup. Intent judgement retains `intent_judge_timeout_sec`; planning retains `planner_timeout_sec`; digest passes retain `llm_digest_timeout_sec`. Tool execution retains its 300 s default.

The v4 config migration seeds `llm_routing_timeout_sec` from an explicitly stored `llm_tools_timeout_sec` only when routing is unset. Explicit routing, chat, context and tool values and unknown keys are preserved. Missing routing and chat values use 8 s and 45 s respectively; defaults are not written to disk.

### Factory dispatch

- `get_llm_backend(cfg)` reads `llm_provider`. For `openai_compatible` it resolves `llm_base_url` (falling back to `ollama_base_url`); for `ollama` it uses `ollama_base_url` directly so a stale `llm_base_url` from a previous OpenAI-compatible config cannot leak into the Ollama backend. `llm_api_key` is read regardless (sent only when non-empty).
- `get_embedding_backend(cfg)` reads `embedding_provider` (falls back to `llm_provider` when unset), resolves `embedding_base_url` (falls back per-provider: `llm_base_url` for OpenAI-compatible, `ollama_base_url` for Ollama), and `embedding_api_key` (falls back to `llm_api_key`).
- Construction is fail-soft: an unset URL becomes the default Ollama URL, so `get_*_backend` never raises. Errors surface at request time, not construction time.

### v1 → v2 config migration

The migration in `_migrate_config` runs once when `_config_version < 2`:

1. If `llm_provider` is unset, default to `"ollama"`.
2. Promote `ollama_base_url` → `llm_base_url`, `ollama_chat_model` → `llm_chat_model`, `ollama_embed_model` → `embedding_model` (only when the new key is empty).
3. Bump `_config_version` to `2` and persist via `_save_json` (which restricts the file to `0o600` on POSIX so credentials are not world-readable).

## Wire-shape specifics

### Ollama (`OllamaBackend`)

- Endpoints: `POST /api/chat` (including warmup and release), `POST /api/embeddings`, `POST /api/show` (vision capability), `GET /api/tags`, `GET /api/ps` (release), `GET /api/version`.
- `release(model, timeout_sec=5.0)` reads `GET /api/ps` and, only when the model is listed (a bare name also matches its `:latest` tag), sends `POST /api/chat` with no messages and `keep_alive: 0` to unload it. It never loads a model that is not resident. Errors return False.
- Images: `chat()` keeps a message's `images` (base64 strings, Ollama's native field) alongside the OpenAI fields. The reply engine sets them per call only for a model whose `supports_images` is true (`tools/builtin/screenshot.spec.md`). Every other backend drops the field with the other non-standard fields.
- Streaming: JSON-lines (`{...}\n`).
- Tool calls: native `tools` parameter (Ollama 0.4+); arguments returned as a Python dict.
- Prompt caching: every chat payload (`chat()`, `direct()`, `streaming()`) sets `cache_prompt: true` explicitly so the server retains the request's KV state and reuses it when the next request shares the same prefix. Callers keep prefixes cacheable by keeping system prompts byte-static and pushing per-call data (time, hints) to the tail of the prompt (see `docs/llm_contexts.md` "KV-cache discipline").
- `extra_options` translates `max_tokens` to `num_predict` and forwards generation settings such as `temperature`, `format` and `think`. Nested `options` sampling settings are supported. Backend-owned `num_ctx` and root `keep_alive` always win; a nested `keep_alive` is removed.
- `OllamaBackend(base_url, num_ctx=8192, keep_alive="30m")` owns a stable request shape. `get_llm_backend(cfg)` supplies `cfg.llm_num_ctx` and the residency (`1m` in low-power mode, otherwise `cfg.llm_keep_alive`). Both tiers share these settings today. Separate future model targets can construct backends with different stable settings, without changing inference callers.
- `warm_up(model, timeout_sec=...)` verifies Ollama via `GET /api/version`, then sends a one-token `/api/chat` request with the live context, residency and default `think: false`. The inference timeout uses the remaining budget after the version probe. Prompt caching is enabled on warmup and every live completion.

### OpenAI-compatible (`OpenAICompatibleBackend`)

- Endpoints: `POST /chat/completions`, `POST /embeddings`, `GET /models`.
- Streaming: Server-Sent Events. Lines start with `data:` and an empty payload terminator is `data: [DONE]`. Comment lines (`: ping`) and malformed payloads are skipped.
- Tool calls: native `tools` parameter; OpenAI returns `tool_calls[*].function.arguments` as a JSON-encoded string. The backend decodes them to a dict so the reply engine sees a single shape.
- Response normalisation: `_normalise_response` lifts `choices[0].message` to top-level `message` so callers do not branch on provider. Servers that already return Ollama-shaped responses pass through unchanged.
- `extra_options` lifts sampling fields (`temperature`, `max_tokens`, `top_p`, `stop`, …) to the payload root and silently drops Ollama-only knobs (`keep_alive`, `num_ctx`, `num_predict`, `think`) that have no equivalent in the OpenAI shape.
- `warm_up(model)` is a two-phase probe: it first issues ``GET /models`` as a fast reachability check (capped at 25 % of the budget, max 5 s), then sends a minimal chat completion (``max_tokens=1``, ``content: "ping"``) to force the runtime to load the model into memory. Without the inference phase, an OpenAI-compatible server may keep the model in a cold state until the first real user request, incurring latency on the first query. The fallback stance remains: a failed warmup is informational and never blocks operation.
- Authentication: `Authorization: Bearer <api_key>` header sent only when `api_key` is non-empty.
- Error logs do not echo URLs or API keys: HTTP errors print only the status code, generic exceptions print only the class name, connection errors print a fixed string and re-raise so callers can apply their own back-off.
- `check_capabilities(chat_model, embed_model=None, *, timeout_sec)` returns a `ServerCapabilities` dataclass (`reachable`, `chat`, `tools`, `embeddings`, `models`). It probes with real requests — `list_models`, a one-message chat, a trivial tool call, and an embedding — and never raises (every failure collapses to a `False` flag). `chat` is True for either a text reply or a tool-call-only reply. Used by the setup wizard and the desktop startup check to report honestly what a server+model can do before the user relies on it. The probe issues real inference, so it is recorded in `docs/llm_contexts.md`.

## Module-local LLM wrappers

Each migrated module exposes a single intercept point so tests can patch one symbol per module instead of reaching into the backend ABC:

- `jarvis.reply.engine.chat_with_messages(cfg, messages, ...)` — agentic-loop chat boundary.
- `jarvis.reply.planner.call_llm_direct(*, cfg, chat_model, ...)` — planner + step resolver.
- `jarvis.reply.evaluator.call_llm_direct(*, cfg, chat_model, ...)` — terminal evaluator.
- `jarvis.reply.enrichment.call_llm_direct(*, cfg, chat_model, ...)` — memory enrichment extractor + digest passes.
- `jarvis.memory.graph_ops.call_llm_direct(*, cfg, chat_model, ...)` — knowledge graph extraction, best-child picker, node merge.
- `jarvis.memory.conversation._direct_llm(cfg, system_prompt, user_content, ...)` — diary summary, deflection rewrite, topic optimisation.
- `jarvis.tools.builtin.nutrition.log_meal.call_llm_direct(*, cfg, chat_model, ...)` — nutrition extractor + follow-up generator.
- `jarvis.tools.builtin.weather.get_llm_backend` — hoisted to module scope so the place extractor's backend lookup is patchable.

A factory-dispatch wiring guard at `tests/test_factory_dispatch_wiring.py` parametrises across each migrated module and asserts the wrapper actually constructs `OpenAICompatibleBackend` for `llm_provider: openai_compatible` and `OllamaBackend` for `ollama`. A regression that drops `get_llm_backend(cfg)` from a wrapper would bypass every unit test but trip this guard.

`import requests` is re-exported from the package `__init__.py` so tests that patch `jarvis.llm.requests.post` keep working without reaching into the per-backend modules.

## File layout

```
src/jarvis/llm/
├── __init__.py             # public re-exports + function-style helpers
├── backend.py              # LLMBackend ABC + ToolsNotSupportedError
├── ollama.py               # OllamaBackend + extract_text_from_response
├── openai_compatible.py    # OpenAICompatibleBackend + _normalise_response
├── factory.py              # get_llm_backend(cfg) + get_embedding_backend(cfg)
└── llm.spec.md             # this file
```

## Failure handling

`chat()` recognises response-body read timeouts wrapped as `requests.ConnectionError(urllib3.ReadTimeoutError(...))`. These follow the normal timeout path: a safe message includes the configured timeout, `None` is returned, and intent detection does not enter connection-failure cooldown. Classification uses exception types and explicit causes/arguments, never message matching or raw exception text. Actual connection failures still propagate; URLs, headers and credentials are not printed.

Backends fail soft for transient issues so the reply engine can degrade gracefully: timeouts and HTTP errors return `None` (or `[]` for `list_models`); HTTP 400 with `tools` set raises `ToolsNotSupportedError`; any other unexpected error is logged via `debug_log("...", "llm")` and returns `None`. The one exception is `requests.ConnectionError` (server unreachable), which `chat()` re-raises so callers like the intent judge can apply their own back-off — voice, for example, wants a 30s cooldown after a connection-refused error so it stops hammering an unresponsive Ollama between wake words.

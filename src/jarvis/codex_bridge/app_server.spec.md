# Codex app-server client

`app_server.py` runs one Jarvis-owned `codex app-server --listen stdio://` child process and speaks its newline-delimited JSON-RPC protocol. It is the only transport of the background Codex reply mode (see `codex_bridge.spec.md`). It knows nothing of the broker, tools, prompts or the reply engine. In `local` reply mode it is never imported or started.

## Runtime

- Tested against `codex-cli 0.159.0-alpha.12.1`, the copy bundled with the Codex desktop app (`OpenAI.Codex` 26.930.x). The app-server protocol, `dynamicTools` and the `item/tool/call` request are marked experimental by Codex; the client opts in with `capabilities.experimentalApi` and fails explicitly when a call it needs is rejected.
- Executable: `codex_executable` (default `codex`). A bare name is looked up on `PATH`; when it is the default name and not on `PATH`, a standard install location is used. On Windows that is the copy the Codex desktop app installs for the current user (`%LOCALAPPDATA%\OpenAI\Codex\bin\<build>\codex.exe`, newest first). On macOS, where apps started from the Dock or Finder get a short `PATH`, it is the first executable `codex` in `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin` or `~/.npm-global/bin`, then the copy bundled in `ChatGPT.app` or `Codex.app` (in `/Applications`, then `~/Applications`) directly under `Contents/Resources/`, in one subfolder of it, or in a subfolder's `bin` folder (ChatGPT.app ships `Contents/Resources/codex-cli/bin/codex`). An explicit path is used as given. Nothing is downloaded or upgraded. A missing executable is reported as `not_found`.
- Authentication is Codex's own: the child uses the user's existing Codex sign-in from its normal home directory. Jarvis never reads, copies or prints credential files or tokens and never passes an API key.

## Process

- Started with an argument list (no shell), stdin, stdout and stderr pipes, `CREATE_NO_WINDOW` on Windows and the dedicated runtime directory as working directory. The environment is the user's, plus `RUST_LOG=warn` and `NO_COLOR=1` for the child only, so Codex's local log store does not record request content.
- Process-scoped `-c` overrides (below) are the only configuration change. The user's Codex configuration files are never edited. `--analytics-default-enabled` is never passed and `analytics.enabled=false` is set.
- `start()` launches the child, then sends `initialize` (client info, `experimentalApi`, opted-out streaming notifications) and the `initialized` notification. It is bounded by a start timeout. Each start increments the process generation; every event carries the generation it came from.
- `close()` closes stdin (the server exits on end of input), waits a bounded time, then terminates and finally kills the child it started. It never looks for, signals or terminates any other Codex process.

## Protocol handling

- stdout is read on a reader thread in chunks and split on newlines, so fragmented and coalesced messages are handled. A message larger than the message bound (1 MiB), a line that is not a JSON object, and output without a newline past the bound are protocol errors: the generation is closed and the child is stopped.
- stderr is drained on its own thread and discarded: it is never mixed into the protocol stream, kept or logged.
- Responses are matched to pending requests by ID. An error response raises `AppServerError("rpc_error")` with the code and a bounded message. A request whose response does not arrive in time raises `timeout`; a request on a closed child raises `closed`.
- Server-initiated requests and notifications go to an event queue read with `poll_event(timeout)`. The reader thread never executes anything. Notifications beyond the queue bound are dropped; server requests and the closing event are never dropped.
- End of output or a protocol error fails every pending request with `closed` and queues one `closed` event with a reason (`exited`, `oversized`, `malformed`).
- `respond(id, result)` and `respond_error(id, code, message)` answer server requests. A write to a closed pipe closes the generation.

## Isolation overrides

The child exposes only the host-provided dynamic tools to the model. The surface was measured on the tested runtime by asking the model to name its tools in a synthetic turn, with and without the overrides:

| Without overrides | With overrides |
|-------------------|----------------|
| code mode `exec`/`wait`, `request_user_input`, MCP resource tools, goals, plugin install, web search, image generation, sleep, sub-agent `collaboration.*`, the user's MCP servers and apps | code mode `exec`/`wait`, `request_user_input`, sub-agent `collaboration.*`; nested tools: the dynamic tools and a read-only clock |

Process overrides (`ISOLATION_OVERRIDES`): the shell, image viewing, sleep, multi-agent, apps, plugins, browser use, computer use, image generation, tool suggestion, skill search and skill dependency install, hooks, goals, in-app browser, memories, workspace dependencies, remote plugins and collaboration modes features are off; web search is `disabled`; message history persistence is `none`; `notify` is empty; project instruction files are not read (`project_doc_max_bytes=0`); apps, environment, permissions and collaboration-mode instructions are not injected; update checks are off; `agents.max_threads=1`.

Per-thread overrides (`thread_isolation_config`): every MCP server and plugin named in the effective configuration (read with `config/read` for the runtime directory before each thread) is disabled by name, because a table override merges rather than replaces. Naming a server that is no longer configured makes `thread/start` fail, so the list is never cached.

Remaining surface and why it is acceptable:

- The `unified_exec` feature reports as enabled whatever its override, so it is not overridden. With the shell tool off and no execution environment, no command tool reaches the model (measured).

- Code mode `exec` runs model-written JavaScript in Codex's isolated host. Its only nested tools are the dynamic tools and the clock, so every action still arrives as an `item/tool/call` request that Jarvis validates.
- `request_user_input` arrives as a server request and is refused.
- The sub-agent tools come from the model's catalogue entry and cannot be removed by configuration. A sub-agent inherits the same restricted surface, `agents.max_threads=1` leaves no concurrent slot for one, and its dynamic tool calls carry a different thread ID, which Jarvis refuses.

Threads are started with `sandbox: read-only`, `approvalPolicy: never` and `environments: []` (no execution environment), and every server request other than `item/tool/call` is answered with an error, which declines approvals. The overrides are guidance to Codex, not a Jarvis boundary: Jarvis's boundary is the per-request tool snapshot, validation, confirmation and bounds in `codex_bridge.spec.md`.

## Persistence

Measured on the tested runtime: an ephemeral thread reports `ephemeral: true` and no path, and writes no rollout file under the Codex home. With `history.persistence=none` and `RUST_LOG=warn`, request text was not found in the Codex history or log stores. Requests are still processed by OpenAI's service; nothing here promises remote retention behaviour.

## Verification

- Subprocess-fixture tests (`tests/test_codex_app_server.py`) cover initialise ordering, interleaved responses and events, fragmented and coalesced messages, server requests and responses, stderr volume, error responses, timeouts, oversized and malformed output, unexpected exit, bounded close, a missing executable, executable resolution and the isolation configuration.
- A live, no-inference probe (`JARVIS_CODEX_LIVE=1`) starts the installed runtime with the overrides and checks the handshake, the effective feature flags, an ephemeral thread with no path and no instruction sources, and that no MCP server starts.
- The model-visible tool surface needs inference and is checked by the live background evals.

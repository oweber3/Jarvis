# MCP runtime spec

## Purpose

Keep one stdio session per configured MCP server alive across tool
invocations. The naive `asyncio.run(open → call → close)` pattern works
for stateless servers but breaks any server that owns external state
(e.g. `chrome-devtools-mcp` launches Chrome on first navigation —
closing the session kills the browser). This module replaces that
pattern with a singleton runtime that keeps each server's subprocess
resident for the daemon's lifetime.

## Architecture

- One process-wide singleton `_PersistentMCPRuntime` accessible via
  `get_runtime()`. Created lazily on first use; recreated after
  `shutdown_runtime()`.
- A single background thread runs an `asyncio` event loop
  (`JarvisMCPRuntime`). All MCP I/O happens on this loop.
- Per server, a `_ServerWorker` task lives on that loop. The task
  holds `stdio_client(...)` and `ClientSession(...)` open and consumes
  `(kind, payload, future)` tuples from an `asyncio.Queue`.
- Callers (registry → `MCPClient.list_tools` / `invoke_tool`) submit
  requests via `runtime.invoke(...)` / `runtime.list_tools(...)`. Each
  call hops the request onto the loop with `call_soon_threadsafe(put_nowait, ...)`
  and blocks on a `concurrent.futures.Future` for the result.

## Lifecycle

| Event | Effect |
|-------|--------|
| First `get_runtime()` call | Spawns the background thread + loop. |
| First call referencing a server | Creates a `_ServerWorker`, awaits `_ready` (the worker signals readiness once `session.initialize()` returns). A server that does not finish the handshake within 30 s (`_SETUP_TIMEOUT_SEC`) fails the call with `TimeoutError` and its worker is shut down, so the half-started subprocess ends. |
| A call times out while still queued | Its future is cancelled and the worker skips it: a request reported to its caller as failed never runs later. |
| A call times out in flight | The session is stuck in that call, so the worker is shut down (its task cancelled, its subprocess ended). Requests queued behind it are drained with `_WorkerDeadError` and retried once on a fresh worker while their callers still wait; the next call gets a fresh session. |
| Server config equality holds | Subsequent calls reuse the cached worker. |
| Server config changes | Old worker is shut down; a fresh worker replaces it. |
| Worker raises `_WorkerDeadError` | Runtime drops it and retries the call once with a new worker. Second failure surfaces as `MCPServerSessionError` to the public layer. |
| `idle_timeout_sec` set on a server config | Worker self-terminates after that long without activity. Next call spawns a new worker. |
| Daemon shutdown calls `shutdown_runtime()` | Each worker is asked to exit (sentinel `None`); any wedged task is cancelled. The loop is stopped, the thread is joined with a 5s timeout. |

## Invariants

- One in-flight `call_tool` per server at any time. Tool calls to the
  same server are serialised by the queue. Different servers run in
  parallel because each has its own worker.
- A worker is never reused after `alive` flips to `False`. The
  finally-block in `_run` drains pending requests, resolving each
  outstanding future with `_WorkerDeadError` so callers do not hang.
- A request runs only while its caller still waits: the worker marks
  each future running before calling the server and skips one its
  caller has cancelled after a timeout.
- A call still in flight when its worker is stopped (a config change or
  shutdown) fails with `MCPServerSessionError` and is never retried, since
  it may already have acted. Only requests that never started are retried.
- A dead worker is evicted only if it is still the cached one, so
  concurrent retries never tear down each other's fresh worker.
- `MCPClient.invoke_tool_async` is unchanged and still uses one-shot
  sessions. Sync `MCPClient.list_tools` / `invoke_tool` route through
  the runtime.

## Public surface

- `MCPClient.list_tools(server_name)` — returns a list of tool dicts.
  Routes through the persistent runtime so discovery and the first
  invocation share a session.
- `MCPClient.invoke_tool(server_name, tool_name, arguments)` — returns
  the standard MCP response dict. Raises `MCPServerSessionError` if
  the runtime cannot keep a session alive after one retry.
- `MCPServerSessionError` (in `mcp_client.py`) — public, stable type
  signalling a session-level failure (distinct from a tool-level error
  carried in the response dict's `isError`).
- `get_runtime()` / `shutdown_runtime()` — module-level helpers used
  by the daemon's startup and shutdown paths.

## Start-up discovery

The daemon fills the tools cache with `registry.start_mcp_discovery(mcps, on_done)`
on a background thread (`mcp-discovery`), so starting servers (an `npx`
download can take seconds) overlaps TTS, Whisper and model warm-ups instead
of holding them up. The MCP client module is imported on the calling thread
first, so its DLL loads never race the speech models' on the Windows loader
lock. When discovery ends, `on_done` prints one line per server (tools
available, an error, or none discovered), and only then is the cache
released:

- `get_cached_mcp_tools()` waits for a discovery still in progress, so a
  request that arrives early sees the full tool catalogue.
- `get_cached_mcp_tools(wait=False)` never waits: it returns no tools until
  discovery has finished (used for start-up tips only).
- A failed discovery reports its errors and releases readers with an empty
  cache. The smoke test waits for discovery before reporting success.

## Configuration

Each server entry in `config.mcps` is a dict consumed by
`MCPClient._connect_stdio`. The runtime additionally honours:

| Key | Type | Default | Effect |
|-----|------|---------|--------|
| `idle_timeout_sec` | float \| null | null | If set, the worker self-terminates after that many seconds with an empty queue. Stateful servers (browser automation) must leave this unset. |
| `timeout_sec` | float \| null | 120 (`_DEFAULT_INVOKE_TIMEOUT_SEC`) | Bounds a single `call_tool` round trip and `list_tools` discovery. Servers whose tools legitimately run long (e.g. delegating a task to an external CLI agent) should raise this; a bare `concurrent.futures.TimeoutError` propagates on expiry (see Lifecycle for what happens to the session). Non-finite or non-positive values fall back to the default. |

## Test contract

Behavioural tests live in `tests/test_mcp_client.py` (with doubles) and
`tests/test_mcp_runtime_robustness.py` (with fake stdio servers as Python
child scripts). The contract verified there:

- After a call times out in flight, the stuck subprocess ends and the
  next call succeeds on a fresh session.
- A call that timed out while queued never runs.
- A call queued behind a call that times out in flight runs exactly
  once, on a fresh session.
- A server that misses the handshake window leaves no process running.
- `shutdown_runtime()` ends the server subprocess.

- A second `invoke_tool` does not open a new stdio connection.
- `list_tools` followed by `invoke_tool` shares one stdio connection.
- A `_WorkerDeadError` from a worker triggers exactly one retry, which
  spawns a fresh connection.
- A config change replaces the worker and spawns a fresh connection.
- A failure during subprocess spawn propagates to the caller rather
  than hanging.
- Distinct servers do not share workers.
- `timeout_sec` overrides the default for `invoke_tool` and `list_tools`.
- Invalid `timeout_sec` values (non-finite, non-positive, `bool`, or
  unparseable) fall back to the default and log a diagnostic.
- `invoke_tool` accepts an optional per-call ``timeout`` override.
- `list_tools` accepts an optional per-call ``timeout`` override,
  mirroring `invoke_tool`.
- An exception with an empty `str(e)` (e.g. bare `TimeoutError`)
  produces a diagnosable error message via type-name fallback.

## Non-goals

- Hot-reloading `config.mcps` proactively. The runtime replaces a
  worker only when a request arrives carrying the new config.
- Recovering from SIGKILL of the daemon process. Subprocess children
  (e.g. Chrome) become orphans and must be cleaned up by the OS.
- Parallel `call_tool` to the same server. The MCP stdio framing is
  request-response per session; parallelism is per-server, not
  per-call.

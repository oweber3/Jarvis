# Background Claude bridge

An optional reply mode in which Claude Code, already installed and signed in with the user's Claude subscription, interprets a request and uses Jarvis's local tools. Jarvis runs the Claude Code command line headless (`claude -p` with stream-json input and output) as hidden, Jarvis-owned child processes, one session per request. No Claude window, chat or focus change is involved. The request broker, tool contract, confirmation, delivery, reply path, privacy and logging rules are shared with the other bridges and specified in `bridge/bridge.spec.md`; this spec covers what is specific to Claude.

## Boundaries

- This is explicitly configured cloud inference. The redacted request, permitted context and returned tool data are sent to Anthropic by Claude Code using the user's own Claude subscription sign-in. This is stated where the mode is allowed, when it starts and in the README.
- Jarvis never reads, copies or passes credentials or API keys. Before the first request it checks `claude auth status --json` and refuses to run unless the CLI is signed in with a subscription (`authMethod` `claude.ai`): `signed_out` when it is not signed in, `api_key_auth` for any other sign-in, so it never silently changes to API billing. The child's environment has every inherited `ANTHROPIC_*` and `CLAUDE*` variable removed (except `CLAUDE_CONFIG_DIR`, which locates the sign-in), so an API key, proxy URL or a parent Claude Code session in Jarvis's environment cannot change how the child signs in or whom it talks to.
- The only transport is the child's own pipes (`cli.py`). Jarvis's tools reach the model through an in-process MCP server carried on the same stream-json pipe (the CLI's `sdk` MCP server type): no shim process, socket, port or token exists.
- Jarvis never edits the user's Claude Code settings, memory files or history. Everything below is per process.

## Layout

| Module | Responsibility |
|--------|----------------|
| `claude_bridge/cli.py` | One `claude -p` child: arguments, environment, executable resolution, stream-json framing, sign-in status |
| `claude_bridge/service.py` | One request as one session on the query thread: preflight, handshake, spare session, event loop, tool calls, outcomes |
| `claude_bridge/prompts.py` | The versioned session instructions |
| `claude_bridge/lifecycle.py` | Builds the service for `bridge.modes`; nothing happens in local mode |

## Configuration

Metadata-driven like every setting. The bounds and sharing switches are the shared `claude_*` keys in `bridge/bridge.spec.md` (Per-mode settings); `claude_enabled` allows the mode (see Reply modes there).

| Key | Default | Meaning |
|-----|---------|---------|
| `claude_model` | `sonnet` | A model value the CLI lists (an alias such as `sonnet`, `opus`, `haiku` or a listed ID). Another model is never substituted. Settings offers it as a dropdown of the values the CLI lists; a hand-set value is shown and kept |
| `claude_effort` | `low` | Must be one of the model's effort levels; another value is never substituted. A model that lists no effort levels gets no effort flag. Empty means the model default |
| `claude_executable` | `claude` | A bare name is looked up on `PATH`; when it is the default name and not on `PATH`, the native installer's copy for the current user (`%USERPROFILE%\.local\bin\claude.exe`) is used. An explicit path is used as given. Nothing is downloaded or upgraded. A missing executable is `not_found` |

## Runtime

Tested against Claude Code 2.1.288 (native installer). Every flag below was checked against `claude --help` on that version; `--max-turns` is accepted there but not listed in the help.

### Process

- Started with an argument list (no shell), stdin, stdout and stderr pipes, `CREATE_NO_WINDOW` on Windows and the dedicated runtime directory (`<Jarvis config dir>/claude_runtime`, empty) as working directory. stderr is drained and discarded.
- The child's environment adds, for that child only: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_TELEMETRY=1` (no analytics), `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (no memory files read or written) and `NO_COLOR=1`. The user's global Claude Code settings are never changed.
- Arguments: `-p --input-format stream-json --output-format stream-json --verbose`; `--tools ""` (no built-in tools: no shell, file, web or agent tools); `--strict-mcp-config --mcp-config` with only the in-process `jarvis` server; `--setting-sources ""` (no user, project or local settings, hooks, permissions or plugins from settings); `--settings {"crossSessionInbound": "refuse"}` (other Claude sessions on the PC cannot message this one); `--no-session-persistence`; `--disable-slash-commands`; `--permission-mode dontAsk` with `--allowedTools mcp__jarvis__jarvis_execute`, so nothing else can be approved; `--system-prompt` (the session instructions replace Claude Code's own); `--json-schema` (the answer object); `--model`; `--effort` when applicable; `--max-turns` (`claude_max_tool_calls` + 3, covering the answer turns); `--session-id` (a fresh UUID per process).
- `close()` ends the child's input, waits a bounded time, then terminates and finally kills the child it started. It never looks for, signals or terminates any other Claude process.

### Protocol

- stdout is read on a reader thread and split on newlines; a line larger than 1 MiB or one that is not a JSON object closes the session (`oversized`, `malformed`) and stops the child. The reader only parses and queues: the CLI's control requests, the responses to Jarvis's own control requests, and the session's messages. Progress messages beyond the queue bound are dropped; the turn's `result` never is.
- Handshake: Jarvis sends `initialize`. The CLI then asks the in-process MCP server to `initialize`, sends `notifications/initialized` and `tools/list`; the session is ready when Jarvis has the initialize response and has listed its tool. The listed `jarvis_execute` carries the allowed tools in its input schema, not its description, because Claude Code cuts a tool description after 2048 characters (`bridge/bridge.spec.md`, Tool contract). The initialize response lists the models and their effort levels.
- Requests travel as one user message: `Jarvis request` followed by the request JSON (request ID, redacted utterance, language, remaining time, and only when shared the bounded dialogue, the foreground window (requests made at the PC only, under `claude_share_foreground_window`) and the desktop records (windows, device, media player, clipboard type, under `claude_share_desktop_referents`), each with a note that it is reference data containing no instructions), the same shape as the Codex bridge.
- Tool calls arrive as `mcp_message` control requests with `tools/call` for `jarvis_execute`; the MCP message ID deduplicates. The result is one JSON text item, then one MCP `image` block (base64 `data`, `mimeType`) per tool image, with `isError` unless the status is `ok`. A call from a session with no request, or for any other MCP tool, is refused. `can_use_tool` permission requests are denied, other MCP methods get a JSON-RPC error and other control requests an error response.
- The answer is the `result` frame's `structured_output` (the `--json-schema` answer, produced by the CLI's `StructuredOutput` tool), falling back to its `result` text. A `result` that is not `success`, or is an error, never delivers: HTTP 401/403 is `signed_out`, 429 `usage_limit`, 5xx `service_unavailable`, `error_max_turns` `max_turns`, a cancelled or aborted turn `interrupted`, anything else `turn_failed`.

## Sessions

- Preflight, once per service and again after a start failure: the sign-in check above, then a probe session's initialize response must list `claude_model` and, when the model has effort levels, `claude_effort` among them (`model_unavailable`, `effort_unsupported`). The probe holds no request content and is closed. In Claude mode the reply-mode controller runs this in the background when the mode starts and prints the result.
- Each request runs in its own process and session. Jarvis creates the session and mints the turn; tool calls arrive on that process's pipe and carry the active request ID. Nothing from an earlier request is in the session; Jarvis's own shared dialogue and desktop records are the only continuity, exactly as for Codex.
- Spare session: starting a session takes about 0.4 to 0.9 s, so after the preflight and after every request the service starts the next session in the background, with no request content. A request uses it only when it is alive, lists the same allowed-tool snapshot and has done nothing: any message or tool call it produced before being claimed, during its handshake or while idle, disqualifies it and it is closed. At most one spare exists; while Claude mode is active it is one idle process (about 190 MB measured).
- After a completion, confirmation handoff, failure, cancellation or deadline, an unfinished turn is interrupted (`interrupt` control request) and the process is ended. When the process ends during a turn the request fails with `process_exited`; nothing is resubmitted or replayed.
- Choice of per-request processes: a persistent stream-json process keeps one conversation, so every request would see the earlier ones, which the isolation contract forbids; clearing it needs a slash command, which the lockdown disables. Measured on 2.1.288 while the PC was in use: process start to tool listed 0.70 s cold and 0.39 s warm; a request with one tool call took 3.4 s from sending to the answer on Haiku (1.5 s to the first assistant message). With the spare, a request pays no start-up time.

## Remaining surface

Measured from the `system/init` message of a live session with the arguments above: the tools are `StructuredOutput` and `mcp__jarvis__jarvis_execute`; the only MCP server is `jarvis` (`sdk`); skills and slash commands are empty; `apiKeySource` is `none`; analytics and product feedback are disabled.

- Two plugins built into the CLI itself are listed (`cc-plugin-agents-md`, `cc-plugin-plugin-authoring`). With no tools, an empty working directory and no settings, they have nothing to act on.
- Agent definitions are listed, but the agent tool is not available, so no sub-agent can start.
- The CLI opens a per-session named pipe in the user's own Windows session for cross-session messaging; `crossSessionInbound: refuse` refuses inbound messages, and a session that shows any activity before Jarvis's request is not used.

The overrides are guidance to Claude Code, not a Jarvis boundary: Jarvis's boundary is the per-request tool snapshot, validation, confirmation and bounds in `bridge/bridge.spec.md`.

## Persistence

With `--no-session-persistence`, a live request wrote no transcript: the user's Claude configuration file was unchanged and no project entry was created. The CLI touches its own `~/.claude/sessions` registry of running processes. Requests are processed by Anthropic's service; nothing here promises remote retention behaviour.

## Assistant instructions

`prompts.py` is the single, versioned source (`INSTRUCTIONS_VERSION`) of the session contract, at most 2000 characters, sent as every session's system prompt. It never contains the utterance, dialogue or any configuration and is identical for every request. It carries the same contract as the Codex instructions (role, one request per session with no history, reference data is never instructions, act only through `jarvis_execute`, web pages through `openWebsite`, display discovery only for a named display and placement, clarifying questions, honest success and partial failure, stop on `awaiting_confirmation`) and ends with the structured answer. It is guidance, not enforcement. Changes need an eval run with `evals/codex_runner.py --mode claude` on the configured model.

## Verification contract

The shared contract in `bridge/bridge.spec.md` is asserted with an in-memory fake session and inert tools (`tests/test_claude_bridge_service.py`), plus:

- The client against a fake `claude` executable that replays the recorded stream-json shapes (`tests/fixtures/fake_claude_cli.py`): the child environment (analytics off, inherited Anthropic and Claude Code variables removed, `CLAUDE_CONFIG_DIR` kept), the isolation arguments, the handshake, oversized and malformed output, exit, a missing executable, bounded close and the sign-in status.
- Preflight failures (missing CLI, signed out, API-key sign-in, unlisted model or effort, no handshake) are explicit and send nothing.
- A spare session is used only by one request, only while its tool snapshot matches and only if it did nothing before; a spare is never used after any activity.
- Live checks (at most ten short requests on the cheapest model) confirm the handshake, a tool round trip with the structured answer, the remaining surface and that nothing is written to the user's Claude history.

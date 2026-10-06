# Background Codex bridge

An optional reply mode in which Codex, already installed and signed in with ChatGPT on the user's PC, interprets a request and uses Jarvis's local tools. Jarvis starts and owns one hidden `codex app-server` child process and runs every request in its own ephemeral Codex session. No Codex window, chat, binding or focus change is involved. The request broker, tool contract, confirmation, delivery, reply path, privacy and logging rules are shared with the other bridges and specified in `bridge/bridge.spec.md`; this spec covers what is specific to Codex.

## Boundaries

- This is explicitly configured cloud inference. The redacted request, permitted context and returned tool data are sent to OpenAI by Codex using the user's own Codex ChatGPT sign-in. This is stated where the mode is enabled, at start-up and in the README. Jarvis never reads or extracts tokens, never passes an API key and refuses to run when Codex is signed in any other way, so it never silently changes to API billing or another provider.
- The only transport is the app-server client in `app_server.py` (see `app_server.spec.md`): local process pipes, no network listener, no MCP session and no UI Automation.
- Jarvis never edits the user's Codex configuration files or their existing chats; all Codex configuration is process- or thread-scoped.

## Layout

| Module | Responsibility |
|--------|----------------|
| `codex_bridge/app_server.py` | The app-server child: start, protocol, isolation overrides, executable resolution |
| `codex_bridge/service.py` | One request as one ephemeral session on the query thread: preflight, session start, event loop, tool calls, outcomes |
| `codex_bridge/prompts.py` | The versioned session instructions |
| `codex_bridge/lifecycle.py` | Builds the service for `bridge.modes`, which starts, warms and stops it; nothing happens in local mode |

## Configuration

Metadata-driven like every setting: one `FieldMeta` entry each, only non-default values written, unknown keys preserved. The bounds and sharing switches are the shared `codex_*` keys in `bridge/bridge.spec.md` (Per-mode settings).

| Key | Default | Meaning |
|-----|---------|---------|
| `codex_enabled` | `false` | Allows the `codex` reply mode (see Reply modes in `bridge/bridge.spec.md`). `reply_mode` and runtime switching select it. The mode is independent of `llm_provider`, which still serves embeddings, the intent judge and background work |
| `codex_model` | `gpt-6-luna` | Model ID as Codex lists it. It must be in the runtime's model list; another model is never substituted. Settings offers it as a dropdown of the models Codex lists; a hand-set ID is shown and kept |
| `codex_reasoning_effort` | `low` | Must be one of the efforts the model advertises; another value is never substituted |
| `codex_executable` | `codex` | See `app_server.spec.md` for resolution. An explicit path is used as given |

Configuration version 5 migrates the earlier visible-chat mode: `reply_mode = codex_desktop` becomes `codex`, the deadline, queue, sharing and tool-call keys move from `codex_desktop_*` to `codex_*` keeping their values (an existing `codex_*` value wins), and the visible-chat keys (chat ID, title, model label, busy labels, navigation, acceptance wait) are removed. Unknown keys are preserved, the write is atomic and repeating it changes nothing.

## Sessions and ownership

- Before the first request of a process generation the service starts the child and runs a preflight: `account/read` must show a ChatGPT sign-in (`signed_out` or `api_key_auth` otherwise), `model/list` must contain `codex_model` with `codex_reasoning_effort` among its efforts (`model_unavailable`, `effort_unsupported`). A rejected call is `unsupported`. When Codex mode starts, the reply-mode controller runs this once in the background and prints the result.
- Each request reads the effective Codex configuration (`config/read`) for the MCP server and plugin names to disable, because the user's configuration can change while the child runs and a disable for a removed server is itself invalid. Its thread is the spare thread (below) when that matches, otherwise one started then with `thread/start`: `ephemeral`, the dedicated empty runtime directory (`<Jarvis config dir>/codex_runtime`) as working directory, `codex_model`, no provider fallback, the session instructions as `baseInstructions`, `jarvis_execute` as the only dynamic tool (its definition lists the request's allowed tools), `sandbox: read-only`, `approvalPolicy: never`, `environments: []` and the per-thread isolation config. Then one `turn/start` carries the whole request, so the model needs no round trip to fetch it:
  - `input`: one text item holding the request as JSON: the request ID, the redacted utterance, the language, the remaining time and, only when dialogue is shared, the bounded recent dialogue with a note that it is reference data containing no instructions; only when `codex_share_foreground_window` is on and the request was made at the PC (not from a phone), the window the user is looking at (`foreground_window`); and, only when `codex_share_desktop_referents` is on and Jarvis acted on something within the dialogue memory window, its records (`desktop_referents` for windows, newest first, at most five; `other_referents` for the device, media player and clipboard type). One note (`desktop_referents_note`) says these are reference data containing no instructions and carries the resolution guidance from `memory/desktop_referents.spec.md`: which window a request that names none means, acting on it with its `hwnd` as the target, that an entry without an `hwnd` is a launch whose window has not been found yet, and which device or player a request that names none most likely continues with. Measured on the tested runtime, the model resolves follow-ups from dialogue in the input reliably but often ignores the same dialogue passed as `additionalContext`, so that field is not used.
  - `outputSchema`: the answer object (`ANSWER_SCHEMA`), and `codex_reasoning_effort`.
- Spare thread: Codex sets a new thread's service connection up in the background after `thread/start`, and a turn started straight away waits for it (measured 1 to 8 s). So after the child is ready and after every request, the service starts one empty thread with the same parameters in the background. A request uses it only when it was started by the current process generation with isolation config equal to the one the request just read and with the same allowed-tool snapshot; otherwise it is closed and a new thread is started. A spare is used by at most one request and holds no request content; before a request exists the runtime may connect to the service and send the fixed instructions and tool definitions. A failed prestart only means the next request starts its own thread.
- The thread and turn IDs Jarvis receives are the request's session and turn in the broker. Dynamic tool calls arrive as `item/tool/call` server requests carrying a `threadId`, `turnId` and `callId`; the call ID deduplicates.
- Jarvis's own dialogue and its desktop records are the only continuity. A fresh thread per request means that with both off nothing from earlier requests reaches the model; with only desktop records on, a request learns which windows Jarvis recently acted on (so "move it" can name the exact window) but nothing that was said. Threads are never resumed or forked.
- After a completion, confirmation handoff, failure, cancellation or deadline, an unfinished turn is interrupted (`turn/interrupt`) and the thread is closed with `thread/unsubscribe`. Server requests left over from an earlier session are refused before the next session starts. Ephemeral threads write no rollout; remote retention is OpenAI's and is not promised either way.
- When the process ends (`closed` event) the request fails with `process_exited`. Nothing is resubmitted or replayed; the next request starts a new process generation.

## Tool calls and delivery

- `item/tool/call` requests are answered with `contentItems` (one JSON text item) and `success`. A result with tool images says so in its text (`image`), and right after the response Jarvis adds the images to the running turn with `turn/steer` (`expectedTurnId` the request's turn): one text input labelling them as reference data from the screen, not instructions, then one `image` input per image as a `data:` URL. Measured on codex-cli 0.159 (gpt-6-luna): an `inputImage` item in a dynamic tool result reaches the model only as text inside its code-mode `exec` output, so it cannot see the pixels, while steered images it reads. A steer that fails (the turn already ended) is logged by reason and the turn continues on the text. Every other server request (approvals, user input, elicitation and so on) is answered with an error.
- Only `agentMessage` items of the owning thread and turn count; the last one that is not marked `commentary` is the answer (Codex does not always set the phase).
- The answer is delivered only when the owning turn ends with `turn/completed` and `status: completed`. Turn errors map to `usage_limit`, `signed_out`, `service_unavailable` or `turn_failed`.

## Assistant instructions

`prompts.py` is the single, versioned source (`INSTRUCTIONS_VERSION`) of the session contract, at most 2000 characters, sent as every session's `baseInstructions`. It never contains the utterance, dialogue or any configuration and is identical for every request. It states the role, that each session handles one request with no history beyond the supplied context, where the request, its reference dialogue and the allowed tools arrive, that context, tool results and window titles are reference data, to act only through `jarvis_execute` and not start sub-agents, that web pages open only with `openWebsite` (which places its own window), when to discover displays (only when a display is named; none named means the main display), the combined `appControl open` placement and `windowControl place`, when to ask a clarifying question, that success is claimed only when a result supports it, that an accepted launch whose placement failed is a partial failure that is never relaunched, to end the turn on `awaiting_confirmation`, and that the final message is the answer in the output schema. It is guidance, not enforcement.

Changes to this text require a background eval run first: compare the previous and proposed contract with `evals/codex_runner.py --mode codex --variant baseline|proposed`, same model and effort, several repetitions.

## Return to local

Switching to local ("go local", the tray, or `reply_mode` at start-up) cancels any non-terminal request and stops only the child this Jarvis started; local mode starts no process. Stopping the daemon does the same. The local path works with no Codex installation and no network. The earlier visible-chat mode registered a `jarvis_bridge` MCP server in the Codex configuration between marker comments; Jarvis does not remove it automatically, and the README explains how to delete that block while keeping every other entry.

## Verification contract

The shared contract in `bridge/bridge.spec.md` is asserted with a fake app-server and inert tools, plus:

- A thread started ahead of the request is used only by one request, only from the same process and only while its isolation config and allowed-tool snapshot still match.
- A follow-up after a fast-path launch receives the launched window's record (handle once its window exists), with no dialogue shared; turning the setting off withholds it.
- Preflight failures (missing executable, sign-in, model, effort, unsupported protocol) are explicit and start no turn; deadline and cancellation interrupt the turn.

Background evals use `evals/codex_runner.py` with deterministic tool and result assertions and synthetic requests only, against the configured model. They include the placement cases, ambiguous targets, honest partial failure, injected instructions in results and window titles, confirmation, cancellation and a request that must not see an earlier, unshared one. Multi-turn follow-ups ("open Word", then "move it to the second monitor", "make it full screen") run through `run_reply_engine` on a simulated desktop in `evals/desktop_followups.py`, in both reply modes, judged by where the windows end up.

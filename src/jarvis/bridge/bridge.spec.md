# Background reply bridges

Jarvis can hand a whole request to an agent that is already installed and signed in on the user's PC, instead of answering with its local model: Codex (`codex_bridge/codex_bridge.spec.md`) or Claude Code (`claude_bridge/claude_bridge.spec.md`). This spec covers what every bridge shares: the reply modes and switching between them, the request broker, the tool contract, tool execution, confirmation, delivery, the reply path, privacy and logging. Each bridge spec covers its transport, sessions, preflight, failure mapping and instructions.

A bridge is not an `LLMBackend`. Jarvis keeps speech recognition, speech output, conversation history, redaction, safety, confirmations, delivery and the Windows controls. The local reply path is unchanged and remains the offline baseline.

## Boundaries

- A bridge is explicitly configured cloud inference. The redacted request, permitted context and returned tool data are sent to the bridge's provider using the user's own sign-in. This is stated where the mode is enabled (Settings and the setup wizard's optional Cloud reply modes page, `desktop_app/setup_wizard.spec.md`), when it starts and in the README.
- In `local` reply mode no bridge implementation (`codex_bridge`, `claude_bridge`) runs or is imported, no process starts and no cloud dependency exists. Only the small mode switchboard (`bridge/modes.py`) is loaded.
- Core Jarvis (`src/jarvis/`) never imports `desktop_app`. Bridges report status through daemon output and the existing confirmation dialog presenter.
- Each bridge talks only to the child process it started, over that process's pipes. No network listener, socket or token is created.
- Jarvis never edits a provider's configuration files or history; all bridge configuration is process-, session- or request-scoped.

## Layout

| Module | Responsibility |
|--------|----------------|
| `bridge/broker.py` | Request lifecycle, ID issue, ownership, expiry, bounds, dedupe, cancellation. Pure logic, no I/O |
| `bridge/tools.py` | The `jarvis_execute` definition, the per-request tool snapshot, the answer schema and its parser, the shared outcome type |
| `bridge/execution.py` | One `jarvis_execute` call through the central tool path, on the request's query thread |
| `bridge/settings.py` | The per-mode bounds and sharing switches read from the `<mode>_*` keys |
| `bridge/adapter.py` | Background reply path used by the reply engine; context packaging; delivery |
| `bridge/runtime.py` | Process-wide handle to the running bridge service, used by the engine and stop handling |
| `bridge/modes.py` | The active reply mode: start from configuration, switch at runtime among the allowed modes, persist, notify |
| `tools/builtin/reply_mode.py` | The `replyMode` tool that switches by voice or text |

## Reply modes and switching

| Mode | Who answers | Allowed by |
|------|-------------|------------|
| `local` | The local model (`llm/llm.spec.md`); the offline baseline | always |
| `codex` | ChatGPT through Codex | `codex_enabled` |
| `claude` | Claude through Claude Code | `claude_enabled` |

- `reply_mode` (default `local`) is the mode Jarvis starts in. A cloud mode is entered only when its `<mode>_enabled` is true (both default false, set in Settings or by the setup wizard, which never sets `reply_mode`); otherwise Jarvis starts local and says so. Configuration version 6 sets `codex_enabled` for a configuration already in Codex mode, so an existing Codex user keeps it; nobody else is opted into anything.
- The active mode is runtime state in `bridge/modes.py`, initialised from `reply_mode` at daemon start. The reply engine routes on it, so a switch takes effect on the next request with no restart.
- A switch can come from voice or text (the `replyMode` tool, normally reached through the fast-path phrases "use Claude", "use ChatGPT", "go local" in `fastpath/phrases/<language>.json`) or from the tray (bundled mode calls `daemon.set_reply_mode`; subprocess mode sends `__REPLY_MODE__:{"mode": ...}` on the daemon's stdin). Every route goes through `modes.switch`, which accepts only `local` and the allowed cloud modes: a cloud mode that is not allowed is refused with nothing changed, so a misheard command never sends anything to the cloud.
- Switching cancels a request in flight (reason `mode_switch`), stops the previous bridge's processes (a closed service refuses later requests and warm-up as cancelled, so a request the engine handed to it just before the switch sends nothing and starts no process), starts the new bridge (its warm-up checks run in the background and print their result) and persists `reply_mode` through `config.update_config_values` (only non-default values written, unknown keys kept, atomic). A bridge that cannot be built leaves Jarvis in local mode and persists nothing. Switching to the active mode changes nothing.
- Leaving local mode for a cloud mode releases the models only local replies use (`llm.tiers.local_reply_models`: the chat and tool models, less any the fast tier or embeddings share) on a background thread (`reply-model-release`), so they do not hold memory for the runtime's keep-alive. Each is unloaded only if the runtime has it loaded now (`LLMBackend.release`), and the console prints `🧹 Unloaded local model '…' (… replies do not use it)`. A failure is logged and never affects the switch. Moving between cloud modes or back to local releases nothing; switching back to local loads the chat model on its first request.
- Listeners hear `(mode, allowed modes)` after start-up and after every switch. In subprocess mode the daemon forwards them to the desktop app as `__REPLY_MODE_STATE__:` events.
- The `replyMode` tool (`set` or `get`) is registered only when at least one cloud mode is allowed, so the default catalogue is unchanged. It is a routine (`SAFE`) action and is never offered to a cloud model (see Tool contract).
- Deterministic fast commands run before any bridge, so "go local" works in every mode without reaching the cloud.

## Per-mode settings

Each bridge has the same bounds and sharing switches under its own prefix (`codex_*`, `claude_*`), one `FieldMeta` entry each, so the user can share differently with each provider.

| Key suffix | Default | Meaning |
|------------|---------|---------|
| `_timeout_sec` | 90 | Deadline from the session start to a validated completion |
| `_queue_limit` | 1 | Requests waiting behind the active one. Further requests are refused as busy |
| `_share_recent_dialogue` | `true` | Include bounded recent dialogue |
| `_recent_dialogue_messages` | 6 | Maximum messages shared |
| `_share_desktop_referents` | `true` | Include Jarvis's own records of what it recently acted on (`memory/desktop_referents.spec.md`): windows (application, process, window handle, display, zone, state, last action and age), the device it controlled, the media player and the clipboard's content type. Never titles, paths, track names, clipboard contents or conversation text |
| `_share_foreground_window` | `true` | Include the window the user is looking at when a request is made at the PC (`memory/desktop_referents.spec.md`, Foreground window): application, process, window handle, display and state. Attached to every such request, not only after Jarvis acted on a window. Never the title. Phone requests carry none |
| `_share_long_term_memory` | `false` | Expose memory retrieval tools. Off means no diary, graph or meal content is ever disclosed |
| `_max_tool_calls` | 8 | Executed tool calls per request |

Size bounds are constants: utterance 4000 characters, context 4000 characters per message, tool arguments 8 KiB, tool result 4000 characters, final reply 2000 characters, protocol message 1 MiB. Anything longer is truncated with a marker (results, replies) or rejected (utterance, arguments, messages).

## Request lifecycle

States: `queued`, `submitted`, `active`, `awaiting_confirmation`, `completed`, `failed`, `cancelled`, `expired`. The last four are terminal.

| From | To | Trigger |
|------|----|---------|
| `queued` | `submitted` | Jarvis created the request's session |
| `queued` | `failed` | Preflight or session start failed (reason recorded) |
| `submitted` | `active` | Jarvis started the turn carrying the request |
| `active` | `awaiting_confirmation` | An executed call needs confirmation |
| `submitted`, `active` | `completed` / `failed` | The owning turn ended (see Delivery) |
| `awaiting_confirmation` | `completed` | The approved action ran; its outcome was delivered by the confirmation path |
| `awaiting_confirmation` | `cancelled` | Denied, expired, replaced or cancelled |
| `queued`, `submitted`, `active` | `cancelled` | User stop, shutdown or mode switch |
| non-terminal | `failed` | The child process ended (`process_exited`) |
| `queued`, `submitted`, `active` | `expired` | Deadline reached |

- Every transition is a compare-and-set under one lock. The first terminal transition wins; later events change nothing and deliver nothing.
- IDs are `secrets.token_urlsafe(24)`. An ID is a reference, never a credential.
- At most one request has a session at a time; at most `<mode>_queue_limit` wait, in order, and a queued request that reaches its deadline before its turn expires. A request beyond that is refused immediately with a busy message.
- Only the 16 most recent finished requests are kept (so a stale call is refused with its finished reason); older ones, with their utterance, context and results, are forgotten. A call for a forgotten request is refused as `unknown_request`.
- Cancellation invalidates the request at once and stops its turn. Later calls for it are refused with no side effect. A tool already running is not interrupted; its result is discarded and its outcome recorded as uncertain.
- The session and turn Jarvis creates are assigned to the request. Every tool call must belong to that session and turn and carry the active request ID; anything else is refused (`wrong_thread`, `wrong_turn`, `wrong_request`) and discloses nothing. Model arguments never choose a session.

## Tool contract

Every bridge offers the model exactly one host tool:

| Tool | Arguments | Behaviour |
|------|-----------|-----------|
| `jarvis_execute` | `request_id`, `tool_name`, `arguments` | Executes one allowed tool through the central path |

Its definition carries the request's allowed-tool snapshot and `tool_name` is an enum of the allowed names. Measured on codex-cli 0.159, a model reliably uses a catalogue read in a tool definition but skips tools listed only in the turn text or context. Where the catalogue goes depends on what the bridge's client passes to the model whole:

- Codex: the description lists each tool as `- name: description Arguments: <input schema>`.
- Claude: Claude Code shows a model only the first 2048 characters of an MCP tool description but the whole input schema (measured on 2.1.288 with a 44,000-character catalogue: only the first two tools were readable from the description). The description is a short fixed text, and the `arguments` schema has one `anyOf` entry per allowed tool whose `title` is the tool's name, carrying its description and input schema.

- The allowed-tool snapshot is taken when the request is created. The model cannot widen it. A name outside the snapshot, a tool disabled by its setting (for example `windows_tools_enabled`), malformed or oversized arguments are refused with a structured reason and no side effect. A refusal for malformed arguments (`invalid_arguments`) also carries `error`, the validator's message (unknown or missing argument keys, or a value that is not an object), so the model can correct the call; the message is bounded, goes only to the model and is never logged. Arguments are validated against the tool's real schema, so the placement fields of `appControl` and `windowControl` (`monitor`, `zone`, `state`) and the `displays` and `place` actions are available exactly as to the local model. A partial placement result is an `error` status whose text is the structured outcome.
- Execution goes through `run_tool_with_retries` on the query thread that owns the request, never on a reader thread, so central safety classification, denial and confirmation apply exactly as in the local path and tools keep their threading assumptions. The request's snapshot goes with each call (and with its confirmation, once approved), so a routine run through `jarvis_execute` uses only those tools (`routines/routines.spec.md`). The tools that hold personal data (the nutrition log tools and any memory retrieval tool) are in the snapshot only when `<mode>_share_long_term_memory` is true. The local router tools (`toolSearchTool`, `refreshMCPTools`), the reply-mode switch (`replyMode`) and the conversation-ending `stop` tool are never in it, so a cloud model can neither widen its catalogue, move the conversation to another provider nor end a turn without a reply. Dismissals are handled locally by the listener's stop handling and the intent judge before anything reaches a bridge. `activityLog` (the opt-in record of foreground applications and window titles, `memory/activity_log.spec.md`) is in the snapshot of every bridge only when `activity_log_share_with_cloud` is true (default false), whatever the mode's other sharing switches say; the gate is in `build_tool_snapshot`, so a call for it otherwise fails the snapshot check like any unknown name.
- The protocol call ID deduplicates: a repeat returns the stored result without executing. Identical tool and canonical arguments within one request also return the stored result. If a side effect may have happened before a result was stored, the call is recorded as `uncertain` and neither the same nor a repeated call executes again.
- The call count is capped by `<mode>_max_tool_calls`. Once the owning turn has ended no further calls run.
- Results carry `status` (`ok`, `error`, `refused`, `awaiting_confirmation`, `uncertain`) and redacted, bounded text labelled as data.
- A tool that hands the model images (`ToolExecutionResult.images`, today only `screenshot`, `tools/builtin/screenshot.spec.md`) has them delivered with that call's result in the form the bridge's model can see (Claude: image blocks in the result; Codex: steered into the turn right after the result), only when the status is `ok` and the tool has just run. A repeated call answered from the stored result carries no image. Images are never redacted, bounded by the text limits, stored by the broker or logged; they exist only in the response to that call.
- Every other request from the child (approvals, user input, elicitation and so on) is declined.

## Confirmation

- The question is spoken or shown by Jarvis. The exact proposed action stays in the existing `ConfirmationStore`; a model's statement that the user approved is never evidence.
- When a call needs confirmation, the request enters `awaiting_confirmation`, the call's answer tells the model to stop, the question is returned immediately, the turn is stopped and the session closed. The query lock is released while the user decides.
- `run_tool_with_retries` stores the request reference in the confirmation's execution context; `ConfirmationStore.pending_for_ref` finds it, and the store notifies observers once per request when it is approved and claimed, denied, expired, replaced or cancelled. Approval completes the request (the existing path executes the exact stored action once and delivers its outcome); every other event cancels it. A confirmation raised by a call whose request ended while the tool ran (stopped, switched away or past its deadline) is discarded at once and the call is refused (`not_active`), because nobody will hear the question and a later "yes" must not approve it.

## Delivery

- The answer is the turn's final assistant message, constrained to `{"status": "completed" | "needs_user_input" | "failed", "reply": "<concise British English answer>"}` (`ANSWER_SCHEMA`). Interim commentary, reasoning and progress are never delivered.
- The answer is delivered only when the owning turn ends successfully. A failed or interrupted turn, a stale event or the process ending never produce a success reply, whatever the model wrote. A successful turn whose final message is missing, not the answer object, has an unknown status or an empty reply is `no_answer`. The first terminal transition wins, so a request is delivered at most once.
- The delivered text is redacted again and handed once to the shared delivery helper (console, TTS for voice only, dialogue recording, hot window). Text replies are silent and share the same dialogue.
- `needs_user_input` is delivered as a question and the normal follow-up window opens. A `failed` answer is delivered as an honest failure reply.

## Reply path

Position in `run_reply_engine`: after redaction, pending-confirmation handling and the fast-command path, before recent-dialogue enrichment, tool routing, planning and the agentic loop. Deterministic commands still execute locally with no session.

1. The adapter builds the request: redacted utterance, language, origin (voice, chat or phone), the bounded recent dialogue when sharing is enabled (turns kept out of the diary because they quote the activity log are left out unless `activity_log_share_with_cloud` is a real `true`), the desktop records younger than the dialogue memory window when `<mode>_share_desktop_referents` is on, and the foreground window when `<mode>_share_foreground_window` is on and the origin is not `phone`. Fast-path turns are part of that dialogue, and their actions leave desktop records like any other route. Nothing else is gathered: no memory, window titles, application paths, configuration, repository content or database content.
2. The running bridge service of the active mode runs the session on the query thread until an outcome. With no running service of that mode the reply says so and how to return to local mode.
3. Failures return a short, actionable message through the same delivery helper, never fall back to the local model, never repeat side effects and never switch provider. Each message offers the switch to local mode. A stop command or the chat Stop button cancels the active request (`runtime.cancel_active_request`); the listener and UI state are restored whatever the outcome.

## Voice

Wake-word and hot-window engagement, echo rejection, stop handling and dictation pauses are unchanged, and only engaged, finalised, redacted requests reach a bridge. Raw audio never leaves the machine and ambient transcripts are never sent. While a bridge request is in flight (`runtime.request_active`), a stop addressed to Jarvis cancels it. The intent judge remains a local fast-tier call for other utterances, so automatic wake-word conversation in a cloud mode still includes one small local inference; a bridge removes the local planner, router, enrichment and chat loop, not the judge. The local chat model is never loaded in a cloud mode: the listener does not warm it, and local work that runs on every route (diary summary, graph extraction, meal logging, dictation clean-up) uses the background tier, which resolves to the fast model while a bridge writes replies (`llm/llm.spec.md`).

## Privacy and logging

- Redaction applies to the utterance, shared dialogue, desktop records, tool results, errors and replies.
- Long-term memory is disclosed only when `<mode>_share_long_term_memory` is true and only through the allowed tools. Embeddings and background memory work stay local.
- Logs use `debug_log` for lifecycle and outcomes (process generation, request ID prefix, state, reason code, turn status, declined request method). They never contain utterances, replies, tool arguments, results, window titles, paths or credentials. A child's stderr is discarded.
- No telemetry: each bridge turns the child's analytics off for that child only and nothing is sent anywhere except through the provider.

## Verification contract

Behaviours that automated tests assert with fake children and inert tools, for every bridge:

- Local mode starts no process and imports no bridge implementation.
- A cloud mode that is not allowed is never entered at start-up and never switched to; switching cancels the request in flight, stops the old bridge, starts the new one, persists the choice and notifies listeners; a failed start leaves local mode and persists nothing.
- Deterministic commands execute locally with no session; other requests skip the local planner, router and chat loop.
- One isolated session per request; no carry-over with dialogue and desktop-record sharing off; sessions closed afterwards.
- Ownership: wrong session, turn or request ID gets nothing. Stale calls from abandoned turns are refused.
- Unknown, disabled, malformed and out-of-snapshot tools produce no side effect; call-ID and canonical-argument dedupe; uncertain outcomes never replay; process death after a side effect neither replays nor claims success.
- A reply waits for a successful turn end and comes only from the owning turn's final answer object; failed and interrupted turns never deliver; a request is delivered once.
- Destructive calls wait for the existing confirmation; denial, expiry and cancellation prevent the action; approval executes the exact stored action once.
- Preflight failures are explicit and start no turn; deadline and cancellation stop the turn.
- Text is silent, voice speaks the accepted answer once, both share dialogue; memory disclosure defaults off; logs exclude content and secrets.

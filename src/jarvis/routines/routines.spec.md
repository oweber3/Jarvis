# Routines

A routine is a named chain of existing tool calls that runs in one request, such as "movie mode": put the TV on its HDMI input, set the PC volume to 30 % and open a music app. Routines are personal behaviour kept as data, never code: the general form of what `windows_workspaces` does for windows. A user creates one by doing the steps once and saying "save that as movie mode"; nobody has to edit JSON, although the configuration stays readable and editable.

The engine is platform-neutral core. It runs any registered tool, built-in or MCP, through the same central path as every other call, so a routine can do nothing that the user could not ask for one step at a time, and every safety rule still applies on every run.

## Layout

| Module | Responsibility |
|--------|----------------|
| `routines/definitions.py` | Structural loading of the `routines` configuration value, name and alias resolution, labels. Pure, no I/O |
| `routines/runner.py` | Validation of a whole routine against the live tool registry, then running its steps in order through `run_tool_with_retries` |
| `routines/store.py` | The live set of routines, loaded at start and replaced after every save; writing changes to `config.json` |
| `routines/journal.py` | The recent-actions record that saving builds from (in memory only) |
| `tools/builtin/routine_control.py` | The thin `routineControl` tool |
| `utils/names.py` | Name normalisation and the contested-name rule, shared with `windows_workspaces` |

`routines/` imports nothing from `platform/`, `desktop_app` or any model code. Tools reach it only through `routineControl`; the registry reaches it only to record executed calls in the journal.

## Configuration

`routines` maps a routine name to a definition. The default is `{}` and is not written. Unknown keys, at every level, are preserved: Jarvis changes only the routine it was asked to change.

```json
{
  "routines": {
    "movie mode": {
      "aliases": ["film night"],
      "steps": [
        {"tool": "tvControl", "args": {"action": "launch", "app": "HDMI 1"}, "label": "TV to the console"},
        {"tool": "systemVolume", "args": {"action": "set", "percent": 30}},
        {"tool": "appControl", "args": {"action": "open", "target": "Music"}}
      ]
    }
  }
}
```

- `aliases` is an optional list of other names. Names and aliases compare case-insensitively after Unicode (NFKC) normalisation, as workspace names do (`utils/names.py`). A name or alias that two routines both claim is offered for neither: a routine whose own name is contested is unavailable, and a contested alias is removed. Both are logged as counts.
- `steps` is a non-empty list of at most 20 steps. Each step has `tool` (a tool name as the tool catalogue spells it, `server__tool` for MCP tools), `args` (an object, default `{}`) and an optional `label`.
- A step's display label is its `label` when given. Otherwise it is derived: the tool name, then the `action` value, then the short text and number values of the other arguments in schema order, leaving out any value that looks like a path or a web address (it contains `/`, `\` or `:`, or starts with `~` or `%`). So `appControl open Music` and `systemVolume set 30`, but a path step reads only `openPath open`. Labels are what results, lists and read-backs show; arguments are never shown.
- Loading drops, with counts only logged, a routine that is not an object, has no usable name, has no steps or more than 20, or has a step that is not an object, has no text `tool`, has `args` that is not an object, or has a `label` that is not text. It is a structural check: whether the tools exist and the arguments fit them is decided when the routine runs, because the catalogue depends on the platform, settings and MCP servers of the moment.
- A routine may not call `routineControl`, and the conversation-control and routing tools (`stop`, `toolSearchTool`, `refreshMCPTools`) are never steps. Such a step fails validation; routines do not nest.

## Validation before running

Running a routine first validates every step against the live state, and runs nothing until all pass:

- The tool is registered now: a built-in tool in the catalogue (so a Windows tool on another platform, or with `windows_tools_enabled` off, is missing) or a discovered MCP tool whose server is configured.
- When the routine is run on behalf of a background reply bridge, every step's tool is in that request's allowed-tool snapshot (see Reply bridges).
- The arguments pass `tools/schema_validation.validate_arguments` against the tool's schema: an object, no unknown keys, no missing required keys.
- Central safety (`evaluate_safety`) does not classify the step `DENY`.

One failed check fails the whole routine with no side effect. The error names the step by its label and position and gives the reason ("step 2 of 3 (systemVolume set 30): this tool is not available here"). It never contains argument values.

## Running

Steps run in order on the calling thread, each through `run_tool_with_retries` with the request's language, so validation, central safety, confirmation, redaction, desktop referents and every other effect of a normal call apply to each step exactly as if it had been asked for alone.

- A process-wide lock admits one routine at a time. A second request while one runs fails at once as busy and runs nothing.
- The whole routine has a 120-second deadline. A step not started when it has passed is `skipped` with reason `out of time`; a step in progress is never interrupted.
- A stop addressed to Jarvis (the voice stop that cancels a reply in flight, or the chat Stop button) cancels the routine between steps: the step in progress finishes and later steps are `skipped` with reason `stopped`.
- A failed step does not stop the routine: it is reported `failed` and the next step runs, as a failed workspace item does. A step is never retried.
- Steps print nothing themselves; the routine prints one line per step through the tool's `user_print` (`✅`, `❌` or `⏭️`, label, reason), which quiet text entry suppresses as usual.

### Confirmation

Defining a routine never pre-approves anything. Each run is classified as a whole before any step runs:

- `routineControl run` takes the strictest tier of its steps (`SAFE`, `CONFIRM_VOICE`, `CONFIRM_DIALOG`). A routine of routine steps runs at once, with no question.
- Otherwise the run itself is the action to confirm. The question names the routine and every step that needs confirmation, with that step's own action and target ("run the routine tidy up, which will delete the file report.pdf. Say yes or no."). It goes through the existing `ConfirmationStore`, so `CONFIRM_DIALOG` steps need the desktop dialog and voice cannot authorise them.
- The confirmation is bound to the routine's name, a fingerprint of its steps and the steps the question names, each with its tool, tier, action, target and parameters. If the routine is changed, or a step's classification changes, before the answer, approval no longer matches: nothing runs and a fresh question is asked.
- Approval runs the routine once. The run's grants are taken from the approved question itself, never from classifying the steps again: each step it named is authorised exactly once, for exactly the tool, action, target and parameters the question named; nothing else is authorised and nothing outlives the run. Denial, expiry or replacement runs no step.
- If a step still asks for confirmation during an approved run (its classification changed), the routine stops there: that step is `failed` with reason `needs confirmation` and later steps are `skipped`. The step's own question stands, and approving it runs that step alone. So the user knows what they would approve, that question follows the report on its own line, worded and naming its target exactly as the same call alone would ask (redacted, not scrubbed); it is not part of the report. Where a single call's question goes, it goes: spoken or shown by Jarvis, and in a background bridge never to the provider, which gets only the awaiting-confirmation notice (`bridge/bridge.spec.md`).

### Result

`run` returns a short readable report, so it can be spoken from the fast path as well as read by a model:

- All steps done: `success`, "Movie mode: all 3 steps done."
- Otherwise `success: false` with the counts and one line per step that was not done: "Movie mode: 2 of 3 steps done. Step 3 (appControl open Music) failed: Application not found."
- Each step is `done`, `failed` (with the tool's error, redacted and with paths and web addresses replaced by `[path]` and `[address]`, or `needs confirmation`) or `skipped` (with its reason).
- The results of information steps (the weather, the time) are not part of the report: a routine is a chain of actions. The report contains labels, positions, statuses and reasons, never argument values.

## Recent actions (the journal)

Saving builds a routine from what Jarvis actually did, never from text a model writes. `routines/journal.py` keeps an in-memory record of executed tool calls:

- `run_tool_with_retries` records each call that ran and returned `success`, on every route (fast path, local loop, planner direct-exec, tool-model mode, background bridges, approved confirmations, steps inside a routine). A call that was refused, failed or is waiting for confirmation records nothing; once approved and run, it is recorded then.
- An entry holds a sequence number (unique for the process), the tool name, the arguments restricted to the keys the tool's schema declares (routing hints such as the fast path's `match` are left out) and the time. `routineControl`, `stop`, `toolSearchTool` and `refreshMCPTools` are never recorded.
- At most 20 entries are kept, newest first. Readers take only entries younger than the dialogue memory window (`dialogue_memory_timeout`), so "that" means this conversation. Entries are never written to disk, the diary, the database or logs, and disappear when Jarvis exits.

## Creating and changing routines

All changes go through `routineControl`, by voice or chat, in any reply mode.

- `recent` lists the journal: each entry's number and derived label, newest last. It is read-only.
- `save` with `name` builds a draft from the journal: the entries numbered in `steps`, in that order, or, without `steps`, every entry of this conversation, oldest first. An unknown or expired number, a repeated number or an empty journal fails with nothing changed ("I have not done anything in this conversation to save yet.").
- The draft is validated as a run would be (tools present, arguments valid, nothing `DENY`), so a routine that could not run is never saved.
- Saving under a name that is a different routine's alias fails. Saving under an existing routine's name replaces that routine, and the question says so.
- `delete` with `name` removes a routine. `rename` with `name` and `new_name` renames it, keeping its aliases and steps; a `new_name` already claimed by another routine fails.
- `save`, `delete` and `rename` are persistent changes, so they always confirm (`CONFIRM_VOICE`, through the existing store). The question reads the draft back step by step: "save the routine movie mode with 3 steps: 1. TV to the console; 2. systemVolume set 30; 3. appControl open Music (it replaces your existing movie mode). Say yes or no." The confirmation is bound to the exact resolved steps, so if the journal changes before the answer the draft no longer matches and a fresh question is asked. Nothing is written until the user says yes.
- Writing re-reads the `routines` value from `config.json`, changes only the named routine (removing any entry whose name normalises to the same key), and writes through `config.update_config_values` (atomic, unknown keys kept). Routines in the file that failed the structural check are kept untouched. The live set (`routines/store.py`) is then reloaded from the file, so the change takes effect on the next request with no restart, for the fast path, the tool and the model alike.

### Prompt-injection boundary

Only a direct request from the user may create or change a routine. Three rules hold this, none of which depends on language:

1. Steps come only from the journal: calls Jarvis executed, each having passed central safety when it ran. A model can choose which recorded calls to save and the name, nothing else.
2. A change is refused (`DENY`, with a message asking the user to say it again on its own) when, earlier in the same request, a tool that returns outside content has run: any MCP tool, and the built-in calls that return outside content (web search and page fetches, file reads and searches, screen text, UI Automation reads, PDF text, the activity log, the clipboard, window titles and what is playing). A tool declares this with `returns_outside_content`, or, when only some of its actions return outside content, per call through `returns_outside_content_for(args)`: `inputControl` for `clipboard_read`, `appControl` and `windowControl` for `list`, `mediaControl` for `now_playing`. `appControl open` with a placement also counts when it answers with candidate windows, whose titles it returns. The request scope is a context variable set by `run_reply_engine` for each request; an approved confirmation, which runs later on its own, is not in any request and is judged by rules 1 and 3.
3. Every change confirms with the full read-back, and only the user's own answer approves it (`reply.spec.md`, Action Safety).

Content seen in an earlier request can still lead a model to propose a change; rule 3 means it then only asks.

## Tool

`routineControl` is platform-neutral and always registered, because saving the first routine needs it. Its description is short so it does not crowd the router.

| Argument | Meaning |
|----------|---------|
| `action` | `run`, `list`, `recent`, `save`, `delete` or `rename` |
| `name` | The routine name or alias (`run`, `delete`, `rename`), or the new routine's name (`save`) |
| `steps` | `save` only: journal entry numbers, in the order to run them. Omitted means everything done in this conversation |
| `new_name` | `rename` only |

- `run`, `list` and `recent` are `SAFE` when the routine's steps are (see Confirmation); `save`, `delete` and `rename` are `CONFIRM_VOICE`.
- An unknown routine fails and lists the available names. `list` returns each routine's name, aliases and step labels, nothing else.
- Results pass through the usual redaction before any model reads them.

## Fast path

Routine names are user data, so running one by name is a deterministic fast command (`fastpath.spec.md`). `routineControl.fast_targets(cfg)` offers each routine's name and aliases from the live set; the matcher's `{routine}` slot resolves them with the same spelling tolerance and margin as application and workspace names. Names are matched as the user wrote them, in any language, and the route does not depend on `windows_tools_enabled`.

- The verb phrases live in `fastpath/phrases/<language>.json` ("start {routine}", "run {routine}", "run my {routine} routine", "start the {routine} routine"), plus the bare name ("{routine}" alone, "Jarvis, movie mode"), which is marked `needs_wake_word` so an overheard name in a hot-window follow-up never runs one. There is no English in code.
- "Start {routine}" and "start {app}" are both templates; when one utterance resolves to both, the matcher's single-match rule makes it fall through to the model. A routine named like a fixed phrase ("pause") does the same, so it never silently replaces that command.
- Only a routine whose run is `SAFE` is eligible, because the dispatcher offers only `SAFE` calls. A routine with a confirming step falls through to the model, which runs it and gets the confirmation question.
- A name containing a locale compound word ("and", "then") never fast-matches; the model path still runs it.
- Success uses the locale template ("{routine} done."); any other outcome returns the tool's report unchanged.
- Saving, deleting and renaming are never fast commands: they confirm, and they need the model to read the request.

## Reply bridges

`routineControl` is in the allowed-tool snapshot of every bridge (`bridge/bridge.spec.md`), with all its actions, so a user in Codex or Claude mode can run, save and manage routines the same way.

- A routine run through `jarvis_execute` may use only tools in that request's snapshot. The bridge's execution path passes the snapshot to `run_tool_with_retries`, which makes it available to the tool for the duration of the call; a step outside it fails validation as unavailable. A routine therefore never widens a cloud model's catalogue: a `replyMode` step, a memory tool without long-term-memory sharing, or `activityLog` without its cloud switch is unavailable there.
- The routine's steps run on the query thread inside that one `jarvis_execute` call and count as one call against `<mode>_max_tool_calls`. A routine that outlasts the bridge deadline is not interrupted; the bridge records the call as uncertain and never replays it.
- What reaches the provider is what any tool result carries: labels, positions, statuses and reasons. Step arguments stay in `config.json` and the in-memory journal; the save question carries labels only.

## Desktop record and dialogue

- Window steps record desktop referents through their own tools (`memory/desktop_referents.spec.md`), so "move it to the second monitor" after a routine acts on the window its last window step opened.
- A routine run is one tool call in the conversation; its report is what tool carryover sees.

## Privacy and logging

- Step arguments live only in the user's `config.json`; journal arguments only in memory.
- `debug_log` records, under `routines`: load counts (kept, dropped), validation failures as step position and reason code, each step's position, tool name and outcome, the run's outcome, busy and stop events, journal record counts, and saves, deletes and renames as outcomes. Never routine names, aliases, labels, arguments, paths, URLs or results.
- No network call is added. A routine reaches the network only through the tools it runs.

## Extension points

Stages that build on this contract and must not need it changed:

- A Settings editor for routines (and workspaces) uses `routines/store.py` to read, validate and write, and each tool's `inputSchema` to generate argument fields.
- Export writes one routine's definition as JSON; import validates it with the same structural check and run validation, previews every step with its label and arguments, and saves through the same confirmed path.
- Other triggers (a timer, a time of day) run a routine through `runner.run_routine`, under the same validation, confirmation and reporting.

## Verification contract

Behaviours that automated tests assert with inert fake tools and a temporary config file:

- Loading: malformed routines and steps are dropped with counts only; contested names and aliases follow the workspace rule; unknown keys survive a save.
- Validation: a missing tool, a tool outside the bridge snapshot, invalid arguments, a `DENY` step or a nested `routineControl` step fails the whole routine, names the step by label and position, and runs nothing.
- Running: steps run in order once each; a failed step is reported and the next runs; the deadline and a stop skip later steps; a second routine while one runs is refused as busy.
- Confirmation: a routine of routine steps runs without a question; a routine with a destructive step asks once before any step runs, naming that step; approval runs it once with only that step authorised, even when another step's classification changes after the approval matched (that step stops the routine); denial, a changed routine and a changed classification before the answer run nothing.
- Results contain labels and outcomes but no argument values, paths or URLs. A step that starts to ask mid-run adds its own question, naming the real target, for the user only: the report stays scrubbed and a bridge's provider never receives the question.
- Journal: successful calls on each route are recorded with schema keys only; failures, refusals and pending confirmations are not; entries expire with the dialogue window.
- Saving: the draft comes only from journal entries; the question reads every step back; nothing is written before approval; the saved routine is live for the fast path and the tool at once; a change after untrusted content in the same request is refused.
- Fast path: name, alias and spelling tolerance; the bare name needs the wake word; ambiguity with an application falls through; a routine with a confirming step falls through.

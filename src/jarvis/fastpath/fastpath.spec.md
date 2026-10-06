# Deterministic fast commands

## Contract

`matcher.match(text, language, targets=..., available_tools=...)` is pure. It
returns one `FastMatch` (command ID, family, tool name, normalised arguments,
optional reply template and display slots), or `None`. It neither discovers
applications nor calls tools, models or OS APIs.

Command-family phrases, fillers, folder names, vocabulary tables (settings
pages, power plans, number words, key names) and acknowledgement templates
are locale data in `phrases/<language>.json`. Only English is supplied. Voice
uses the detected language; text without a language signal uses English.
Unsupported languages fall through. `fast_commands_enabled` defaults to true;
`fast_commands_locales` defaults to `["en"]`. Disabled routing/locales leave the
conversational path available. Phrase tables are bundled with the desktop app.

## Slots

A locale phrase marks a variable part with `{name}`. Every slot a phrase may use is registered in `matcher.SLOTS`, and nothing else in the matcher names a slot: a registry entry holds the regular expression for the text it captures (built from the locale data) and a resolver that turns the captured text into arguments and display text for the reply template. A resolver that cannot make a valid value rejects the rule, which then falls through. Rules may rename a slot's argument with `slot_args` (the `{number}` slot of the desktop rule becomes `desktop`). A test checks that every slot in the locale file is registered.

| Slot | Captures | Becomes |
|------|----------|---------|
| `app`, `workspace` | An application or workspace name | `target`, resolved against the offered targets (see below) |
| `folder` | A common folder name | `target`, from the locale `folders` table |
| `percent` | A number from 0 to 100 | `percent` |
| `page` | A settings page name | `page` (a slug), from the locale `settings_pages` table |
| `plan` | A power plan name | `name` (a language-neutral key such as `high_performance`), from the locale `power_plans` table |
| `number` | Digits, or a number word from the locale `numbers` table, of at least 1 | a decimal string, renamed by the rule |
| `keys` | A chord as spoken or typed | `keys`, canonical (`ctrl+shift+esc`). Every word must be a locale `keys` word (or a letter, digit or F-key), the locale `key_joiners` words are skipped between two keys (elsewhere a joiner names its own key, so "control plus" is Ctrl and the plus key), and the chord must be a valid, sendable chord |
| `device` | A playback device name | `name`, with leading locale `article_prefixes` words removed |
| `tv_app` | An app or input name on the Roku TV | `app`, resolved against the TV's cached app list (see below) |
| `page_number` | Up to five digits, or a number word from the locale `numbers` table, of at least 1 | `page`, an integer |
| `routine` | A routine name or alias | `name`, resolved against the user's routines offered by `routineControl` (see below) |

Locale tables are in normalised form (case folded, no apostrophes) so lookups match the normalised utterance, and every value in them refers to something the tools know: slugs exist in the settings page table, plan keys are known power plan keys, key names are known to the input layer.

## Confidence and availability

- Case folding, Unicode normalisation, apostrophes and ordinary punctuation
  are tolerated. Politeness fillers are removed only at utterance boundaries.
  Signs, percent symbols and decimal separators are preserved for validation.
- A template must consume the entire utterance. Multiple distinct tool calls,
  conjunctions, command separators, extra tokens and unresolved slots yield no
  match. Questions about actions and compound requests use the conversational path.
- Percentages are numeric and between 0 and 100 before execution. The volume
  and brightness tools round to whole percentages and report the resulting device state.
- Application names come from the ready local application catalogue and user
  aliases. Discovery is never started or awaited by matching. Duplicate entries
  with the same executable share an identity. Names need a similarity score of
  at least 92 per word and a margin of at least 8 over the next distinct tool
  target. Spelling tolerance preserves word count and version digits. Every
  emitted launch name resolves to the same application under the tool's alias
  rules. Window actions require a discovered executable stem that identifies
  one installed application; stems shared by distinct versions fall through.
- The selected tool must be registered and enabled. Central `evaluate_safety`
  must classify the call as `SAFE`; destructive calls are not eligible.
- A rule marked `needs_wake_word` in the locale table is offered only to an
  addressed request: one spoken with the wake word, started from the orb, or
  typed. A voice request engaged only by the hot window does not get it and
  takes the conversational path. The fixed edit hotkeys (undo, redo, copy,
  paste, select all, save) carry the mark, because a phrase as short as
  "paste" is easily overheard and acts on whatever window has focus;
  "press {keys}" does not. The listener records whether the request it collects
  or dispatches was addressed and passes it to `run_reply_engine`.

## Command families

Time/date uses `getTime` with `local_only: true` (OS clock/timezone without
location lookup). Applications use `appControl` for open, focus and graceful
close. Windows use `windowControl` for minimise, maximise and restore. Volume
uses `systemVolume` for get, set, up, down, mute and unmute. Media uses `mediaControl`
for explicit play/pause, next and previous, and `now_playing` to say what is playing.
System queries use `systemInfo` for CPU, RAM, GPU and disk usage and the largest
CPU/RAM consumers. Common folders use `openPath` for
Downloads, Documents and Desktop. No arbitrary file operations are fast-routed.

System controls:

| Family | Tool and arguments | Notes |
|--------|--------------------|-------|
| Settings pages | `systemSettings` `open_page` with a slug | "open {page} settings" and close variants. Unknown pages fall through |
| Brightness | `systemSettings` `brightness` with `set` (a percent), `up`, `down` or `get` | Percentages outside 0 to 100 fall through |
| Power plan | `systemSettings` `power_plan` with `set` and a plan key, or `get` | A plan the locale does not know falls through; one that is not installed fails honestly in the tool |
| Audio output | `systemSettings` `audio_output` `set` with the spoken device name | The name is resolved by the tool against live devices and configured aliases (`windows_audio_aliases`); an unknown or ambiguous name fails with the available outputs |
| Virtual desktops | `windowControl` `desktop_switch` (a number, `next`, `previous`) and `desktop_new` | A target of zero, or a word that is not a number, falls through |
| Hotkeys | `inputControl` `hotkey` | "press {keys}" and a short list of fixed phrases (show the desktop, task view, undo, redo, copy, paste, select all, save; the edit phrases need the wake word, see Confidence and availability). A chord that closes or deletes is classified `CONFIRM_VOICE` by the central policy, so it is matched but never eligible: it falls to the conversational path, which asks for confirmation |

| PDF pages | `pdfNavigate` `goto` with a page | "go to page {page_number}", "page {page_number}" and similar. Offered only while the window the user is working in (Jarvis's own windows skipped) is a PDF viewer the tool knows: PDFgear, Edge with a PDF in its title, or Chrome whose address bar shows a local PDF. Anywhere else, or when the foreground cannot be read within 1 s, the request falls through to the model, which may mean a page in another application. A page beyond the document fails honestly in the tool |

These routes need their tool to be registered and enabled, so `windows_tools_enabled: false` disables all of them. A tool may also gate its routes on live state with `fast_available(cfg)`, which the dispatcher calls after a match; a false or failing check falls through.

Configured workspaces use `workspaceControl open` (`platform/windows/workspaces.spec.md`). The workspace names and aliases are config data offered by the tool (`fast_targets`), resolved by the matcher's `{workspace}` slot with the same similarity score and margin as application names; the verb phrases are locale data. A workspace phrase always contains a workspace-specific verb phrase ("open my {workspace} workspace", "set up {workspace}"); a bare "open {workspace}" is not a template, so workspace names never compete with application names. The route is available only when the tool is registered and `windows_tools_enabled` is on.

Reply modes use `replyMode set` with a fixed `mode` (`local`, `codex`, `claude`) for whole-utterance phrases such as "use Claude", "use ChatGPT" and "go local" (`bridge/bridge.spec.md`). The mode names are part of the locale phrases, not slots, and every phrase names the mode itself or says "mode", so "switch to Claude" stays an application focus request. The family is available only while `replyMode` is registered, which needs at least one cloud mode allowed in Settings; switching to a mode that is not allowed is refused by the tool. Because the fast path runs before any bridge, these phrases work in every reply mode and never reach the cloud.

Routines use `routineControl run` (`routines/routines.spec.md`). Routine names and aliases come from the live set of routines (`fast_targets`) and resolve with the same similarity score and margin as application names. The verb phrases ("start {routine}", "run my {routine} routine") are locale data; the bare name ("{routine}") is marked `needs_wake_word`. Routine names are offered together with application names, so an utterance that resolves to both ("start chrome" when a routine is called chrome) falls through, as does a routine named like a fixed phrase. Only a routine whose run is `SAFE` is eligible. The family does not depend on `windows_tools_enabled`.

TV commands use `tvControl` (`devices/roku.spec.md`). Every `tv` phrase names the TV ("turn off the TV", "TV volume down", "mute the TV", "pause the TV", "TV home"), so the PC phrases for volume, mute and media keep their meaning. "Put on {tv_app}" and "open {tv_app} on the TV" use the `tv_app` slot, which the matcher resolves against the `tv_apps` it is given (the app names the TV last reported, read from a cache without network access, never discovered or awaited by matching) with the same similarity score and margin as application names. An empty cache, an unknown name or an ambiguous one falls through to the model path. The family is available only while `tvControl` is registered, which needs `roku_host`, and does not depend on `windows_tools_enabled`.

A local extension can add phrase files in the same format (`extensions/extensions.spec.md`, Phrases). Their rules follow the built-in rules for that language, may name only tools the same extension added, and obey every rule here: whole-utterance matches, SAFE actions only, and a fall-through when unsure. They are available only while the extension's tools are registered and are removed when Jarvis stops.

Placement (`monitor`, `zone`, `state`, `windowControl displays` and `place`) is not a fast command. An utterance that adds a destination to an open, maximise or other command is not a whole-utterance match, so it falls through to the model path, which can discover displays and use the combined action.

## Execution and reply flow

Window-targeting routes (app focus/close and window minimise/maximise/restore) carry `match: process`, so the tool resolves only by owning process name or handle and never by window title; a missing application fails instead of acting on an unrelated window. Manual and model tool calls omit it and keep title matching.

Pending confirmations are processed before matching. Immediately after
redaction, `run_reply_engine` calls `dispatcher.match_command`. A match executes
exactly once through `run_tool_with_retries`, then uses shared reply delivery
and dialogue recording. Router, planner, enrichment and chat generation are
skipped. Routine OS results are not added to tool carryover. Every execution
still passes central validation, confirmation and denial, including safety
reclassification after matching. Failed actions and confirmation requests are
returned verbatim after redaction, never replaced by success acknowledgements
or silently retried through a model.

Successful application/window/folder actions use short locale templates.
Read-only, volume and media commands use the existing tool result. Output uses
the ordinary console/TTS path; quiet text entry suppresses tool output as well
as reply output. Fast turns record redacted user and assistant messages.

The listener checks only finalised transcripts, after echo/stop checks and
pending confirmation handling. A wake word or hot-window engagement is required.
A confident match cancels pending hot-window activation, marks its transcript
segment processed and calls `_dispatch_query` immediately, skipping judge and
collection. Existing collection fragments and transcripts captured during TTS
keep the existing judge path. No match preserves that path unchanged. Normal
dispatch retains the query lock, face state, TTS callbacks and
hot-window activation.

## Diagnostics

`debug_log` records `FAST_ROUTE` with the family, tool and redacted normalised
arguments (only the argument names for a tool whose arguments are the user's own
names, `routineControl`), or `LLM_ROUTE` for conversational engine requests. Voice bypass logs
the skipped judge/collection decision. Matcher failures fail open with a concise
exception type, without raw utterances or application paths.

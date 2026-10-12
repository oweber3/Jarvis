# Scheduler

The scheduler is one thread in the daemon that brings things up at a time: timers ("set a 10 minute pasta timer"), alarms ("wake me at 7"), reminders ("remind me at 2:30 to call the dentist") and scheduled routines ("run my morning briefing every weekday at 7"). It is local, needs no network and sends nothing anywhere. Jarvis says what came due through the normal output, so a stop or barge-in cuts it like any reply.

## Layout

| Module | Responsibility |
|--------|----------------|
| `scheduler/service.py` | `SchedulerService`: the thread, firing due items once, missed items at start, starting scheduled routines |
| `scheduler/store.py` | `ScheduleStore`: the `scheduled_items` table in the Jarvis database |
| `scheduler/phrases.py`, `scheduler/phrases/<language>.json` | Every sentence the scheduler says, and spoken durations and times |
| `scheduler/routine_runs.py` | Running a routine at its scheduled time under central safety |
| `scheduler/briefing.py` | Putting a scheduled briefing's step results into words with the local model |
| `scheduler/runtime.py` | Starting and stopping the service in the daemon; `get_service` for the tool |
| `tools/builtin/schedule_control.py` | The thin `scheduleControl` tool |

`scheduler/` imports nothing from `platform/` or `desktop_app`. The daemon hands it two things: `deliver_announcement` (how Jarvis says something on its own) and the shared query lock.

## Items: timers, alarms and reminders

An item has a kind (`timer`, `reminder`, `alarm`), a label (a timer's name or a reminder's message; empty for an unnamed timer, and always empty for an alarm), a due time, the language it was set in and a status (`pending`, `done`, `missed`, `cancelled`).

- A reminder is a timed item with a message; one set without a message is an alarm.
- Any number of items can be pending at once, including several timers with the same name or none.
- An item fires exactly once: the store moves it from `pending` to `done` before it is announced, so a second check, a second thread or a restart never repeats it.
- Times are wall-clock epoch seconds compared with the clock on every wake. The thread sleeps until the next item is due, at most 30 seconds, and is woken at once when an item is added or cancelled. A clock change or the computer sleeping never makes an item fire early, and an item that came due while the computer slept fires on the first check after it wakes.
- A failed announcement is logged and never stops later items.
- The longest timer, and the furthest reminder, is a year ahead.

### Storage

`ScheduleStore` creates and owns `scheduled_items` in the same SQLite file as the diary (`db_path`), with its own connection, as the activity log and web chat stores do. It is never read by retrieval, the diary or the graph. Pending items survive a restart. Finished items (done, missed or cancelled) are deleted 30 days after they finished, when the scheduler starts.

### Missed while Jarvis was off

When the scheduler starts, every pending item that came due more than 2 minutes earlier is marked `missed` and announced once, together, in one sentence ("While I was off, you missed the 14:30 reminder: call the dentist and your pasta timer."). An item that came due within the last 2 minutes fires normally on the first check. A missed item is never announced again and never fires late as if it were on time.

### Announcing

`deliver_announcement(text)` in `daemon.py`:

- prints the sentence (`⏰`) to the console;
- records it in the shared dialogue memory as an assistant message, so "what was that?" works;
- shows it in the chat: bundled chat views get it through `add_chat_result_listener`, and in subprocess mode the daemon emits a `notice` chat IPC event (`desktop_app/chat_window.spec.md`);
- speaks it through `VoiceListener.speak_announcement`, which uses the listener's normal reply output (`_speak_reply`), so a voice stop, the orb or barge-in interrupt it as they interrupt a reply, and the hot window opens after it for a follow-up. While the user is in the middle of a request or a reply is being generated, the announcement is only queued for speech after it: it never ends that engagement, changes the face or opens the hot window. Without a listener the TTS engine speaks it directly; with TTS off it is printed and shown only.

The text is redacted before it goes anywhere. An announcement is spoken once; alarms do not repeat.

## Tool: `scheduleControl`

Platform-neutral and always registered. Times arrive as arguments: the model turns "at 2:30", "tomorrow at 9" or "in 20 minutes" into an ISO time or a number of seconds, using the current date and time it is already given. The tool parses no words.

| Argument | Meaning |
|----------|---------|
| `action` | `set_timer`, `set_reminder`, `list` or `cancel` |
| `seconds` | `set_timer`: the duration. `set_reminder`: optional, that many seconds from now |
| `at` | `set_reminder`: a local ISO time. A clock time (`14:30`) means the next time it comes round (tomorrow when it has passed today) and may not name an offset; a date and time (`2026-10-13T09:00`) is local unless it names an offset |
| `label` | Optional. The timer's name or the reminder's message; omitted for a plain alarm. For `list` and `cancel`, which one |
| `kind` | Optional. `list` and `cancel` only items of this kind |

- `set_timer` and `set_reminder` reply with a confirmation sentence from the phrase file ("Pasta timer set for 10 minutes.", "I will remind you tomorrow at 09:00: email Sam."). A missing or invalid duration or time, a time that has passed, more than a year ahead or a label over 120 characters fails with nothing set.
- `list` gives one sentence per pending item, soonest first: the time left on a timer, the time of an alarm or reminder. With a `label` it lists only matching items; with nothing pending it says so (success).
- `cancel` cancels the single matching item. A label matches when it equals an item's label (ignoring case and spacing) or one contains the other. No match fails; more than one match fails, names them and cancels nothing. Without a label it cancels the only pending item of the kind.
- Every action is `SAFE`: setting and cancelling a timer is a routine local action.
- `log_arguments` is false and the tool is `personal_data`: reminder messages are the user's own words, so the fast path logs only argument names, and the reply bridges offer the tool only while long-term memory is shared with that mode (`bridge/bridge.spec.md`). Fast-path timers work in every reply mode.
- With no scheduler running (it failed to open the database, or outside the daemon) every action fails with "Timers and reminders are not available right now."

### Fast path

The obvious timer phrasings are deterministic fast commands (`fastpath/fastpath.spec.md`, the `timers` family): setting a timer with a duration and optional name ("set a timer for 5 minutes", "set a 10 minute pasta timer", "set a pasta timer for 10 minutes"), the time left ("how long left on the pasta timer"), cancelling ("cancel the pasta timer", "cancel the timer"; an unnamed "stop the timer", often said to quiet a timer that has just gone off, is not a fast command, so it never cancels a different running timer) and listing timers, reminders or alarms. The phrases, unit words, number words and fixed durations ("half an hour") are locale data. A duration the fast path sets is between one second and a day. Clock times ("remind me at 2:30", "wake me at 7") are never fast commands: they go to the model, which produces the ISO time. The replies are the tool's own sentences.

## Wording

Every sentence the scheduler says, and the words of spoken durations ("1 hour and 30 minutes"), come from `scheduler/phrases/<language>.json`, with the time and date formats. An item is announced in the language it was set in; the tool replies in the request's language; a scheduled routine uses the first `fast_commands_locales` language. A language without a file, or a key missing from one, falls back to English. Only English is supplied.

## Scheduled routines

A routine (`routines/routines.spec.md`) runs on its own only when its definition has a `schedule`; routines without one never do, so it is off by default for every routine. Setting or changing a schedule is a confirmed change (`routineControl schedule`).

- Once a check notices a scheduled time between the previous check and now (local time, on one of the schedule's days), the routine runs, once, on a separate worker so the thread keeps checking. A time noticed more than 10 minutes late (the computer was asleep or Jarvis was off) is skipped and logged, never run late. Times before Jarvis started never run.
- A scheduled run is a manual run with nobody to answer a question. It holds the shared query lock, so it never overlaps a voice or text reply. It validates and classifies the whole routine first (`runner.plan`, central safety included) and runs only when every step is `SAFE`. A routine with a step that needs confirmation does not run at all, no step of it, and no confirmation is left pending; Jarvis says it was not run because a step needs confirmation and that the user can ask to run it. A routine that does not validate (a missing tool, invalid arguments, a denied step) is not run and the reason is said.
- The steps then run through `runner.run_routine` with no grants, so every step passes through `run_tool_with_retries` and central safety exactly as in a manual run; a step whose classification changes mid-run stops the routine as it would in a manual run, and the question it asked is dropped at once, because nobody asked for this run and a stray "yes" must never approve it.
- Checks never step back in time: if the clock is moved backwards across a time that already ran, it does not run again.
- At shutdown a scheduled routine in flight is stopped between steps and waited for at most 5 seconds.
- An action routine that worked is printed, not spoken. One that did not is announced with its report (labels and outcomes only).

### Briefings

A routine with `"report": "results"` is a briefing: what its steps found is the point. A morning briefing is simply such a routine whose steps are information tools, for example `getWeather`, `getTime` and a `webSearch` for the news; it uses whatever tools it lists, so a calendar tool added later can be a step without any change here.

- Run by hand, `routineControl run` returns the report followed by each done step's label and result, and the reply model reads it out (briefings are therefore not fast commands).
- Run on schedule, the done steps' results go to the local model in one call (`briefing.py`), fenced as data, with the instruction to say them as a short spoken briefing in the routine's language. The model is the `BACKGROUND` tier, which is local in every reply mode, so a briefing's results never reach a cloud provider through this path. If the call fails or returns nothing, Jarvis says the briefing ran but could not be put into words. See `docs/llm_contexts.md`.

## Daemon

`scheduler.runtime.start(cfg, deliver_announcement, db=..., exclusive=query_lock)` runs after the voice listener starts, so missed items are spoken. It prints one `⏰` line with the pending item and scheduled routine counts. If the database cannot be opened it prints a warning and the rest of Jarvis runs without timers. `stop()` runs at shutdown. The smoke test never starts the scheduler: it runs against the user's real database, where starting would mark reminders missed, fire them into a headless process and run scheduled routines a second time beside the live Jarvis.

## Privacy and logging

- Labels and messages live only in the Jarvis database and in the announcement itself (console, chat, conversation, speech).
- `debug_log` under `scheduler` records kinds, counts, durations in seconds, outcomes and failure exception types. Never labels, messages, routine names or step results.
- No network call and no telemetry. A scheduled routine reaches the network only through the tools it runs.

## Verification contract

Behaviours that automated tests assert with a fake clock, a temporary database and inert fake tools:

- Items: a timer fires once when due, several named timers run side by side, a cancelled item never fires, reminders and alarms say their message or the time, pending items survive a restart, items missed while off are announced once as missed and never again, an item due just before start fires normally, a failing announcement does not stop later items, finished items are pruned after 30 days, the background thread fires a timer on its own.
- Tool: named timers set and fire; time left; listing; clock times later today, passed today (tomorrow), relative seconds, full dates and offsets; invalid input sets nothing; cancel by name, the only one, ambiguity cancels nothing, no match fails; no scheduler fails honestly; every action is `SAFE`.
- Fast path: the timer phrasings with durations and names, time left, cancel and list; uncertain requests, clock times and durations over a day fall through; a fast timer runs through the dispatcher and fires.
- Scheduled routines: schedules load with every day by default, malformed ones are ignored with the routine kept; a routine starts at its time on its days only, once a day; a time slept through is skipped; a run goes through central safety; a step needing confirmation means nothing runs and nothing is left pending; an invalid routine is reported; failures are announced; the shared query lock is held; a briefing speaks the composed text or says it could not be put into words; scheduling reads back and writes only after yes.
- Announcements reach the listener's reply path, the chat (both modes), the console and the conversation.
- Routing evals (`evals/test_scheduler_routing.py`) and the briefing eval (`evals/test_briefing_compose.py`) run against a local model.

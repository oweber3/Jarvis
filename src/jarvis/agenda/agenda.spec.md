# Calendar (read only)

Lets the user ask "what's on today?", "what have I got tomorrow?", "anything this week?" or "when is my next meeting?". Jarvis reads the user's own calendars and never adds, changes, accepts or deletes an event. It is off by default.

## Boundaries

- Nothing runs until `calendar_enabled` is true: the `calendar` tool is not in the catalogue and no calendar is fetched.
- Read only. No source has a write path: an ICS address is fetched with GET, and classic Outlook is only read through COM (properties read, nothing set or saved).
- Offline first. Nothing is required from a cloud service; with no network the last saved copy answers and the result says so. Classic Outlook on this PC needs no network at all.
- Secret calendar addresses live in Windows Credential Manager (`credentials.account_key("calendar_url", name)`), never in `config.json`, logs, tool results or terminal output.
- No telemetry. Debug logs (tag `agenda`) carry counts, reason codes and exception type names, never titles, places, addresses or calendar names.
- Event text is outside content (anyone can send an invitation that lands in a calendar), so it is fenced as untrusted data.

## Layout

| Module | Responsibility |
|--------|----------------|
| `agenda/ics.py` | Bounded RFC 5545 reader: content lines, VEVENT, VTIMEZONE, `occurrences(calendar, start, end)` |
| `agenda/rrule.py` | Recurrence expansion (RRULE) as naive wall times |
| `agenda/sources.py` | `IcsSource` (fetch, size cap, saved copy, offline fallback) and `OutlookCalendarSource` (COM) |
| `agenda/service.py` | Sources from config, named ranges, `read_agenda` (parallel, merged, a failing source never hides others) |
| `agenda/cli.py`, `agenda/__main__.py` | `python -m jarvis.agenda setup | list | test [name] | remove <name>` |
| `platform/windows/outlook.py` | Which Outlook is installed; a bounded call into classic Outlook's MAPI namespace on a COM worker thread |
| `tools/builtin/calendar_tool.py` | The `calendar` tool |

## Sources

`calendar_sources` is a list of `{"name": ..., "type": "ics" | "outlook"}`; names are unique (ignoring case). Unknown types, duplicate names and extra keys are dropped when settings load, so a pasted address never stays in the config.

| Type | What it reads | Setup |
|------|---------------|-------|
| `ics` | A calendar's secret ICS address: Google Calendar's "Secret address in iCal format", an Outlook.com or Microsoft 365 published calendar's ICS link, or any other `.ics` / `webcal://` subscription address | The address is typed hidden and stored in the credential store |
| `outlook` | The default calendar of classic Outlook on this PC (COM, `olFolderCalendar`) | Classic Outlook installed and set up |

### ICS addresses

- `webcal://` and `webcals://` are fetched as `https://`; any scheme other than http(s) is refused. Private network addresses are allowed (a self-hosted calendar server).
- One GET with a 15 second timeout and an 8 MB cap. A response that is not an iCalendar file (`BEGIN:VCALENDAR` near the start) is `not_a_calendar` and is never saved.
- The fetched text is saved next to the database (`calendar_cache/calendar-<hash of name>.ics`, written atomically). A saved copy younger than `calendar_cache_minutes` is reused without a fetch. When a fetch fails (`unreachable`, `http_error`, `too_large`, or `not_a_calendar` such as a Wi-Fi login page) and a copy exists, the copy answers with `offline_copy`, its save time and the reason; without a copy the source fails with the reason. Text is decoded as UTF-8 unless the server names a charset (a byte-order mark is dropped). Sources are kept for the process, so a parsed copy is reused until the file changes.
- Removing a source deletes its stored address and its saved copy.

### Reading ICS

- Lines are unfolded, parameters may be quoted, text is unescaped and kept to one line of at most 300 characters. Only `SUMMARY` and `LOCATION` are read for display; descriptions, attendees, organisers and attachments are never read.
- Times: `...Z` is UTC; a `TZID` is resolved as an IANA name (including `/mozilla.org/.../Area/City` forms), then from the calendar's own `VTIMEZONE` STANDARD and DAYLIGHT rules (the offset in force is that of the latest change before the time, however long ago), then as a Windows zone name Outlook writes (`Eastern Standard Time`); anything else, and floating times, are local. All-day `VALUE=DATE` events cover their local days.
- Recurrence: `FREQ` DAILY, WEEKLY, MONTHLY and YEARLY with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (with ordinals such as `2TU`, `-1FR`), `BYMONTHDAY` (negative counts from the month's end), `BYMONTH`, `BYSETPOS` and `WKST`. Instances keep the event's wall time across daylight-saving changes. `DTSTART` is always the first instance. `RDATE` adds instances, `EXDATE` removes them, a `RECURRENCE-ID` override replaces its instance (shown at its new time even when the original was outside the window) and a cancelled override removes it. `STATUS:CANCELLED` events are left out.
- Unfolding is linear in the file size, and one malformed event (or override) is skipped without hiding the rest of the calendar; an unreadable override leaves its original instance in place. `BYMONTHDAY` in a yearly rule without `BYMONTH` repeats every month.
- An unsupported rule (`HOURLY`, `BYWEEKNO`, `BYYEARDAY`, ...) keeps the first instance only. Expansion is bounded (60 000 periods, 2 000 instances per read, 20 000 events per file); rules without `COUNT` skip whole periods before the window.

### Classic Outlook

- `platform/windows/outlook.install_state()` is `classic` when the `Outlook.Application` COM class is registered, `new_only` when only the new Outlook (`olk.exe`) is present, otherwise `none`. The new Outlook has no COM interface, so a source of type `outlook` then fails with `outlook_new_only` and the tool says to use the calendar's ICS address instead.
- Calls run on a COM worker thread with a 20 second limit. Jarvis attaches to a running Outlook or starts it in the background. Items are read with `IncludeRecurrences` and a `Restrict` on the window, written in the user's own short date and time format (Windows `GetDateFormat`/`GetTimeFormat`), falling back to scanning at most 5 000 sorted items when Outlook rejects it; at most 500 events per read. COM times are Outlook's local wall times and are read as local.

## calendar tool

Registered by `registry.configure_calendar_tool(cfg)` only when `calendar_enabled` is true. SAFE (read only). `returns_outside_content` is true. No argument is required, so a routine step `{"tool": "calendar", "args": {}}` gives today's agenda (a morning briefing).

| Argument | Meaning |
|----------|---------|
| `action` | `agenda` (default) or `next` |
| `range` | For `agenda`: `today` (default), `tomorrow`, `week` (today and the next six days), as whole local days |
| `start`, `end` | For `agenda` on other days: ISO 8601 dates (or times); `end` defaults to `start` and is included. At most 62 days |

- `next` searches the next 30 days for the first timed event that has not started, and lists timed events happening now. All-day events are not meetings and are skipped.
- The result is a header line (JSON: `now`, `range` or `searched_days`, and per calendar `name`, `ok`, and when relevant `offline_copy` with `saved_at`, or a `note` explaining a failure and how to fix it) followed by a note and the events inside the calendar fence (`web_fence.fence(..., "calendar")`, markers `<<<BEGIN UNTRUSTED CALENDAR DATA>>>` / `<<<END UNTRUSTED CALENDAR DATA>>>`, contents defanged). Each event has `calendar`, `title`, `weekday`, either `start` and `end` (local, minute precision) or `all_day`, `date` and `until` for several days, and `location` when set. At most 60 events are listed, with a count of the rest.
- When no calendar can be read the tool fails with each calendar's reason; when some can, the others' notes are in the header. No calendars configured fails with the setup command.
- The turn is private (`memory/activity_log.spec.md` per-turn mark): it stays out of the diary and the console log, and its dialogue is not shared with a cloud model unless allowed (Cloud boundary).

## Cloud boundary

`calendar_share_with_cloud` (default false; only a real `true` counts) decides whether Codex and Claude are offered `calendar` (its entry in `bridge.tools.CLOUD_GATED_TOOLS`). Private dialogue turns are shared with them only when at least one private source is shared and every one that is set up is shared (`bridge.settings.share_private_turns`, see `bridge/bridge.spec.md`); the calendar counts as set up while it is on or while it still has sources configured.

## Setup command

`python -m jarvis.agenda setup` asks which kind of calendar to add (Google, Outlook.com or Microsoft 365, another ICS address, classic Outlook), a name, and for ICS the address (hidden input, with where to find it). It tests the source (events in the next seven days), stores the address only when the test passes, writes the entry and turns `calendar_enabled` on. Names cannot contain `#`. `list` shows each source and whether its address is stored (never the address). `test [name]` fetches fresh and fails when only the saved copy answers. `remove <name>` deletes the entry, address and saved copy, and turns the calendar off when none is left. Output lines start with an emoji and are indented by level. Jarvis needs a restart to pick up changes.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `calendar_enabled` | `false` | Offer the `calendar` tool |
| `calendar_sources` | `[]` | `{"name", "type"}` entries, added by the setup command |
| `calendar_cache_minutes` | 15 | Minutes a fetched calendar is reused (0 fetches every time) |
| `calendar_share_with_cloud` | `false` | Whether Codex and Claude may read the calendar |

Settings rows sit under Privacy > Calendar and email.

## Language

Nothing matches words. Turning "this Friday" into dates is the reply model's job; `range` covers the common requests deterministically. The weekday names in results are data for the reply model, which answers in the user's language.

## Verification contract

Automated tests (no network beyond loopback, no real calendar or Outlook profile):

- ICS reading: timed and all-day events, windows and overlaps, folding and escapes, IANA, VTIMEZONE and Windows zone names across daylight saving, weekly, monthly (ordinal weekday, last working day via BYSETPOS), yearly and missing-day rules, COUNT and UNTIL, EXDATE, moved and cancelled overrides, cancelled events, unsupported rules, long series bounded, non-calendar text refused.
- ICS source against a loopback server: fetch, reuse within the TTL, offline copy with its time after a restart, failure without a copy, non-calendar pages not saved, HTTP errors, missing address, webcal and refused schemes.
- Outlook source with a fake MAPI namespace: local times, recurrences included, new-Outlook-only and not-installed reasons, failures reported not raised.
- Tool: registration only when on, SAFE, no required argument, today's agenda, fencing against a hostile title, next meeting, explicit dates, offline copy in the header, setup hint, private turn, logs without titles, addresses or names, the cloud gate and the private-dialogue rule.
- Setup command: secret storage, nothing saved on a failed test, list, test and remove.

# Lists and notes

Named lists (shopping, to-do, any name the user says) and quick notes, kept in
the Jarvis database on this PC. Two built-in tools expose them: `listControl`
and `noteControl`. Both are always registered and need no setting.

## Storage

`ListsNotesStore` (`memory/lists_notes.py`) owns the tables `user_lists`,
`user_list_items` and `user_notes` in the same SQLite file as the diary
(`db_path`), like `ActivityStore` and `ChatStore`. It opens its own connection
with `secure_delete` (removed text is overwritten, not left in free pages) and
foreign keys on, and creates its tables when missing. The tables are never read
by retrieval, the diary, the graph, issue reports or the activity log.

- A list has a display name (what the user said, whitespace collapsed, at most
  80 characters) and a key: its words (`\w+`, Unicode, NFKC and case folded)
  joined without spaces, so "To-do", "to do" and "todo" are one list.
- An item has its text (at most 200 characters), a key built the same way, a
  done flag and its position. A list never holds two items with the same key.
- A note has its text (at most 2000 characters) and the time it was taken.

### Finding a list by name

A name resolves to the list with the same key. Otherwise it resolves to the one
list whose words begin with all the words of the name, or whose words are all
at the start of the name ("shopping list" finds "shopping" and "shopping"
finds "shopping list"). With no match, or more than one, the name does not
resolve. No word of any language is special.

## `listControl`

One tool with an `action` enum. The model fills `list` and `items` from what the
user said; nothing in code parses the utterance.

| Action | Arguments | Effect | Safety |
|--------|-----------|--------|--------|
| `add` | `list`, `items` | Adds the items, creating the list when the name does not resolve. An item already on the list stays once; a done item added again is open again | SAFE |
| `remove` | `list`, `items` | Removes those items | SAFE |
| `check` | `list`, `items` | Marks those items done (checked off) | SAFE |
| `show` | `list` | Returns the list's items in order with their done flags | SAFE |
| `lists` | none | Returns every list's name with its open and done counts | SAFE |
| `clear` | `list` | Removes every item and keeps the list | CONFIRM_VOICE |
| `delete` | `list` | Removes the list and its items | CONFIRM_VOICE |

- `items` is an array of strings; at most 50 per call. Items are matched by key.
- Results are JSON: the list's display name, what changed (`added`,
  `already_there`, `reopened`, `removed`, `checked`, `not_found`, or
  `removed_count` for `clear`) and the list's items after the change. Single-item changes never confirm.
- `remove`, `check`, `show`, `clear` and `delete` on a name that does not
  resolve fail with the names of the existing lists, so the model can ask or
  pick the intended one.
- The confirmation question names the resolved list ("clear the list
  Shopping"), and the confirmed call acts on exactly that list. A `clear` or
  `delete` whose name does not resolve is SAFE and fails as above, so nothing
  is asked for a list that does not exist.

## `noteControl`

| Action | Arguments | Effect | Safety |
|--------|-----------|--------|--------|
| `add` | `text` | Saves the note | SAFE |
| `search` | `query`, optional `limit` | Notes matching the query, best first | SAFE |
| `recent` | optional `limit` (default 1) | The newest notes, newest first | SAFE |
| `delete` | optional `id` | Deletes that note, or the newest one without `id` | CONFIRM_VOICE |

- Search is plain local text search, no model and no embeddings: the query's
  words of three or more characters (all its words when it has none that long)
  are looked for in each note's folded text. A note matches when it holds at
  least one; notes holding more of the words rank first, then newer notes.
  `limit` is at most 20 (default 5).
- Results are JSON with each note's `id`, `text` and local ISO `created` time,
  so the model can name a note for `delete`.
- The delete confirmation quotes the start of the note (at most 60 characters)
  so the user knows which note goes. A delete whose note does not exist is SAFE
  and fails.

## Memory viewer

The memory viewer (`desktop_app/memory_viewer.py`) has a read-only **Lists &
Notes** tab: every list with its items (done items struck through), then up to
500 notes, newest first, from `/api/lists_notes`. It changes nothing; lists and
notes are changed through Jarvis, where the confirmations above apply.

## Privacy

- List items and note text never appear in logs or console lines. Debug logs
  and progress lines carry only actions and counts.
- A Codex or Claude reply mode gets both tools exactly when that mode's
  `<mode>_share_lists_reminders` is true (default true), the same switch as
  `scheduleControl` and independent of `<mode>_share_long_term_memory`, which
  governs the diary, memory graph and meal log (`bridge/bridge.spec.md`).
  While it is on, a list or note the model reads is sent to that provider;
  with it off, no list or note content reaches a cloud model through Jarvis.
- Read results (`show`, `lists`, `search`, `recent`) count as outside content
  for routines (`routines/routines.spec.md`): a list item or note may have been
  copied from a web page, so routines cannot be changed later in that request.
- Nothing is sent anywhere; the feature works fully offline.

## Not included

- No fast-path phrases: list and note requests carry free text (several items
  joined by "and", note text), which the fast path's whole-utterance slots do
  not capture (`fastpath/fastpath.spec.md`), so they take the model path.
- No reminders or due dates, and no sync with other apps.

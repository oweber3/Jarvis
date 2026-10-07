# Local files (`localFiles`)

`localFiles` finds, reads and changes the user's files: `find`, `list`, `read`, `write`, `append`, `delete`, `move`, `copy` and `rename`. Opening a file or folder in its program is `openPath` (`platform/windows/apps_paths.spec.md`); `openPath` can take a full path that `find` returned.

## Paths

- `path` and `destination` accept an absolute path, a `~` path, or a path relative to the home folder. A relative path is never resolved against the process's working directory.
- `Desktop`, `Documents`, `Downloads`, `Pictures`, `Music` and `Videos` (case-insensitive), alone or as the first part of a relative path (`Documents/Taxes`), name the user's real folders. On Windows they come from `SHGetKnownFolderPath`, so OneDrive redirection, moved folders and localised names are respected. Elsewhere they are `~/<Name>`.
- **Home boundary.** `list`, `read`, `write`, `append`, `delete`, `move`, `copy` and `rename` act only inside the home folder, after symbolic links are resolved. A path outside it is refused before anything is touched. `find` only reports names, dates and sizes. Its scope can be any existing folder, and without a scope it reaches what the local index holds, as `openPath`'s name lookup does.

## Find

`find` lists the files and folders that match every given criterion. It never opens, reads or changes anything.

- `name`: words that must all appear in the item's name. They are split into letter and digit tokens exactly as for `openPath`'s name lookup, so no user text reaches an index as anything but a quoted token.
- `type`: a category or a comma-separated list of extensions (`pdf`, `docx, xlsx`). The categories are `document`, `spreadsheet`, `presentation`, `image`, `video`, `audio`, `archive`, `program` and `folder`. `folder` returns folders only. Any other type returns files only. Without `type`, both files and folders are returned. The extension sets are data in `platform/file_find.py`.
- `when`: `today`, `yesterday`, `this_week`, `last_week`, `this_month`, `last_month`, `this_year`, `last_7_days` or `last_30_days`. Ranges are computed from the computer's local clock and time zone. Weeks start on Monday. `after` (on or after) and `before` (strictly before) take a local date `YYYY-MM-DD` or date and time `YYYY-MM-DDTHH:MM`. `when` cannot be combined with `after` or `before`, and a range that ends before it starts is an error.
- An item's **date** is the later of its creation time at its location and its last modification. A file that arrived recently (a download, a copy) and a file edited recently both count as recent. Date filters and sorting use this date.
- `path`: the folder to search, including everything below it. Without it the search covers the whole index.
- `sort`: `newest` (default), `oldest` or `largest`. Ties break by name. `limit`: how many items to return, from 1 to 50, default 10.
- A call with no `name`, `type`, date or `path` is refused, so a find is never an unbounded listing.
- **Backends** (`platform/file_find.py`, with the index backends in `platform/windows/file_search.py`):
  - With a scope: the voidtools Everything SDK when Everything is running and its library is present (as for `openPath`), otherwise a direct scan of the folder. The scan is always current, so a file that finished downloading a moment ago is found.
  - Without a scope: Everything, otherwise the Windows Search index (`SystemIndex`). If neither answers, the tool says that searching everywhere is unavailable and that naming a folder works. On other platforms there is no index, and a find without a scope scans the home folder.
  - Each backend runs on a bounded worker with the eight-second search limit, and a backend that fails or times out falls through to the next. Windows Search receives the name tokens, the extensions, the date range (converted to UTC) and the scope, and results are filtered again locally. Everything receives the tokens, extensions, file or folder kind and scope, sorted by modification date, and the date range is applied locally.
  - The scan skips caches and package folders (the same names `openPath`'s lookup skips), does not follow symbolic links or junctions, ignores folders it cannot read, and stops after 50,000 entries or its deadline.
  - Results exclude the same noise locations as `openPath`'s lookup (caches, package folders, the recycle bin, and the Windows, Program Files and ProgramData folders). Programs are valid results, since "the installer I downloaded" is a real search.
- **Result** (JSON): `action: found`, `scope` (the folder searched, or `everywhere`), `range` (`after` and `before` as local `YYYY-MM-DD HH:MM`, when a date was given), `count` (how many items matched), `complete` (false when a backend's result cap or the scan's budget was reached, so `count` is a lower bound), and `items`. Each item has `name`, `path` (full), `kind` (`file` or `folder`), `date` (local `YYYY-MM-DD HH:MM`) and, for files, `size` in bytes. When nothing matches, `count` is 0 and `items` is empty. That is a successful find, not an error.

## List, read, write, append, delete

- `list` lists a folder's direct contents, or with `recursive` everything below it, filtered by `glob` (default `*`). It shows at most 50 entries, alphabetically, and says how many more there are.
- `read` returns a text file's content as UTF-8, with undecodable bytes replaced. It is cut off after 10,000 characters, with a note saying so.
- `write` creates or replaces a file with `content` and creates missing parent folders. `append` adds `content` to the end of a file and creates it if needed.
- `delete` permanently removes one file, only while file deletion is allowed (see Safety). Folders are not deleted.

## Move, copy, rename

- `move` and `copy` take the item's full `path` (a file or folder) and a `destination` folder. The destination folder is created if it does not exist. Optional `new_name` gives the item a new name at the same time. `rename` takes `path` and `new_name` and keeps the item in its folder.
- `new_name` is a single name: no folder separators, not `.` or `..`, none of `<>:"/\|?*`, no trailing dot or space, and not a reserved Windows device name (`CON`, `NUL`, `COM1` and so on). When a file's `new_name` has no extension (a final dot followed by up to ten letters or digits), the file keeps its own: `invoice` renames `scan.pdf` to `invoice.pdf`.
- Nothing is ever replaced. If an item with the final name already exists, the call fails and says so. The exception is a rename that only changes letter case. Moving a folder into itself, and moving an item to where it already is, are refused.
- A move within one drive is a rename on disk and is instant. A copy, or a move to another drive, is refused when it would copy more than 2 GiB or 10,000 items, so a reply never waits on a long transfer. Counting stops as soon as the cap is passed.
- **Result** (JSON): `action` (`moved`, `copied` or `renamed`), `kind`, `from` and `to` (full paths).

## Safety

Classification goes through the central policy (`tools/confirmation.py`).

- `find`, `list` and `read` are safe.
- **Deletion switch.** `delete` works only while `file_delete_enabled` is exactly `true` (default `false`; Settings → Windows Control → Allow File Deletion). Otherwise it is classified `DENY` with a reason saying deletion is turned off and where to turn it on, so it is refused before any confirmation question and no reply can lead to a deletion. `run` checks the switch again, so a delete confirmed before the switch was turned off does not run either. The switch applies on every route (local, Codex, Claude, phone, routines), because they all reach the tool through the central path. Other operations are unaffected.
- `write` of a new file and `append` are safe. Replacing an existing file with `write` needs voice confirmation. `delete`, when allowed, needs voice confirmation.
- `move`, `copy` and `rename` are routine and need no confirmation, because they never replace anything. The item's current path (`move`, `rename`) or its final path (`copy`) is the policy target. When the source of a move or rename, or the final path, is in an important location (system folders, startup folders, `.ssh` and similar), desktop confirmation is required.
- The central policy raises any mutating operation in an important location to desktop confirmation, and a bulk or folder-wide delete likewise.

## Privacy and logging

- Searches use only local indexes and the local disk. Nothing is sent anywhere.
- Debug logs record the operation, the backend, counts and exception types. They never contain names, paths, search words or file content.
- Tool results are local data. In the cloud reply modes they pass through the shared redaction before reaching the model (`bridge/bridge.spec.md`).

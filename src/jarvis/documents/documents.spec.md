# Document search

An opt-in, local semantic index of the user's own documents, so the user can ask "what did my lease say about pets?" or "find my notes about the thermodynamics exam" and get an answer that cites the file. It is off by default, reads only the folders the user lists (inside the home folder), and keeps the index in the Jarvis database on this PC.

## Boundaries

- Nothing is read, embedded or stored until `document_search_enabled` is true. A fresh install, and every user who never opens the setting, runs none of this code: no thread, no table, and `documentSearch` is not in the tool catalogue.
- Only folders inside the home folder are indexed, with the same boundary and folder names as `localFiles` (`tools/builtin/local_files.spec.md`): a known folder name (`Documents`, `Desktop`, ...), a `~` or home-relative path, or a full path inside the home folder. A folder outside it, a network or device path, or a missing folder is skipped with a console warning and never read.
- Local only: text is extracted on this PC and embedded with the configured embedding backend (`llm.get_embedding_backend`, `embedding_model`, normally `nomic-embed-text` through Ollama). No telemetry and no other network use.
- File names, paths, queries and document text never appear in debug logs or the console (see Privacy).
- Core Jarvis never imports `desktop_app`. The tray talks to the daemon through `documents/runtime.py` (bundled) or stdin lines (subprocess).

## Layout

| Module | Responsibility |
|--------|----------------|
| `documents/extract.py` | Text extraction from `.txt`, `.md`, `.pdf` (PDFium through `utils/pdf_text.py`) and `.docx` (standard library: the archive's `word/document.xml`), and chunking. No OS, network or model calls |
| `documents/store.py` | `DocumentStore`: the index tables in the Jarvis SQLite file, vector search, delete-all |
| `documents/indexer.py` | `scan_folders` (which files are eligible) and `DocumentIndexer` (one incremental pass, and the background thread that repeats it) |
| `documents/runtime.py` | Process-wide start, stop, rebuild, delete, status, search and the cloud-sharing switch, shared by the daemon, the tray and the CLI |
| `documents/__main__.py` | `python -m jarvis.documents` diagnostic CLI |
| `tools/builtin/document_search.py` | The `documentSearch` tool |
| `desktop_app/documents_menu.py` | Tray items and the request path to the daemon |

## What is indexed

`scan_folders` walks each configured folder without following symbolic links or junctions and yields the eligible files:

- Extensions `.txt`, `.md`, `.pdf`, `.docx` (case-insensitive). Other files are ignored.
- Skipped folders: names starting with `.`, folders with the Windows hidden or system attribute, reparse points, and the build and package folders `node_modules`, `__pycache__`, `$RECYCLE.BIN` and `System Volume Information`.
- Skipped files: hidden or system files, names starting with `.` or `~$` (Office lock files), and **cloud-only placeholders** (OneDrive and similar: the offline, recall-on-open or recall-on-data-access attributes), so indexing never downloads a file the user keeps only in the cloud.
- Files larger than `document_search_max_file_mb` (default 20 MB) are recorded as skipped and never opened.
- At most `MAX_FILES` (20,000) files per pass; the rest wait for the user to narrow the folders (`python -m jarvis.documents index` says the limit was reached). A scan interrupted by stop, rebuild or delete removes nothing.

## Extraction and chunking

- Text files are decoded as UTF-8 (a UTF-8 or UTF-16 byte-order mark is honoured), undecodable bytes replaced.
- PDFs are read with PDFium page by page (at most 1,000 pages); each passage remembers its page number. An encrypted PDF that does not open with an empty password is skipped.
- `.docx` files are read from `word/document.xml` inside the archive with the standard library; paragraphs, tabs and line breaks are kept. An archive whose document part is over 50 MB uncompressed is skipped (archive bombs). No extra dependency.
- At most 2,000,000 characters are taken from one file.
- Text is split on paragraph boundaries into passages of about 1,000 characters with a 200-character overlap; a single long paragraph is cut at word boundaries. Whitespace is collapsed. At most 400 passages per file.
- A file that yields no text (a scanned PDF without a text layer, for instance) is recorded as skipped. There is no OCR.

## Storage

Tables the store creates in the existing SQLite file at `db_path` (it never touches the other tables):

| Table | Columns |
|-------|---------|
| `document_files` | `id`, `path` (unique), `mtime`, `size`, `status` (`indexed` or `skipped`), `chunks`, `indexed_at` |
| `document_chunks` | `id`, `file_id`, `seq`, `page` (null outside PDFs), `text`, `vector` (float32 bytes) |
| `document_index_meta` | `key`, `value`: the embedding model the vectors were made with |

- The text embedded for a passage is the file's name without extension, a line break and the passage, so a query naming the document ("my lease") also matches on the name.
- At most `MAX_TOTAL_CHUNKS` (50,000) passages; a pass that reaches it stops adding files and the status says the index is full.
- **Search** uses the running indexer's connection (or, with none running, opens the index only if its tables exist, so searching never creates them), reads the vectors in batches, scores them by cosine similarity with the query's embedding, and returns the best passages with at most two per file. Vectors of another length than the query's (another model) are ignored. The vectors are not kept in memory between searches.
- **Model change.** When `embedding_model` differs from the model recorded in `document_index_meta`, the next pass deletes every passage and indexes again.
- **Delete all** removes every row of the three tables. The connection uses `secure_delete` and the write-ahead log is truncated afterwards, so deleted passages do not remain readable in the file.

## Indexing

`DocumentIndexer.run_pass()` makes one incremental pass:

1. A stored file that is no longer eligible (deleted, moved out, its folder removed from the list, now too large or cloud-only) is removed with its passages.
2. A file whose modification time or size differs from the stored row (or that has no row) is extracted, chunked, embedded passage by passage and written in one transaction, replacing its old passages. Unchanged files are not opened.
3. Files are taken in a stable order, so an interrupted pass resumes where it stopped: finished files are already stored.

Low priority and bounded:

- The pass waits while Jarvis is busy (any assistant state other than idle or asleep: listening, thinking, speaking, dictating), so indexing never competes with a reply for the embedding server.
- A short pause separates files, and on Windows the indexer thread runs at the lowest thread priority.
- Each embedding call is time-bounded (15 s). When the embedding backend gives no vector (Ollama not running, model not installed), the pass stops, keeps what it has, and the next attempt waits `RETRY_SEC` (5 minutes).
- The background thread (a daemon thread named `document-indexer`) runs a pass at start, then every `document_search_rescan_minutes` (default 60). Stop ends it between passages.
- Unreadable files (permissions, corruption) are recorded as skipped and retried only when they change.

## documentSearch tool

| Argument | Meaning |
|----------|---------|
| `query` | What to look for, in the user's words |
| `limit` | Optional: passages to return, 1 to 8 (default 5) |

- Returns the best passages, each with its file name, page (PDFs) and full path, between the markers `<<<BEGIN UNTRUSTED DOCUMENT TEXT>>>` and `<<<END UNTRUSTED DOCUMENT TEXT>>>`, after a note that says the text is data from the user's own files and never instructions, and that the answer should cite the file. Document text and names pass through `web_fence.defang` first, so nothing inside a document can spell or close a marker. Each passage is at most 1,200 characters.
- After the block, one line gives the index size and whether indexing is still running, so "no matches yet" can be told apart from "nothing relevant".
- When the index is empty the tool says whether indexing is still running or found nothing; when the embedding backend is unavailable it fails with a message saying so. Raw data only: the reply model writes the answer.
- `returns_outside_content` is true (routines.spec.md, prompt-injection boundary). It is a routine, read-only action (`SAFE`).
- Registered only when `document_search_enabled` is true (`registry.configure_document_search_tool`), so the default catalogue is unchanged. The first 120 characters of the description say it searches the text inside the user's documents by meaning; a "NOT for" clause points finding files by name or date to `localFiles`, the web to `webSearch` and the PDF on screen to `pdfNavigate`.

## Privacy

- **Cloud boundary.** In Codex and Claude reply modes the tool is withheld from the request's tool snapshot unless `document_search_share_with_cloud` is true (default false; only a real `true` counts) and sharing has not been stopped since start (`runtime.shared_with_cloud`). The gate lives in the shared `bridge.tools.build_tool_snapshot`, so it applies to both modes, and a call for a withheld tool is refused like any name outside the snapshot. When sharing is on, the passages the model asks for (with file names and paths) are sent to the provider like any other tool result; Settings says so. The local model always has the tool while document search is on.
- **Private turns.** A turn that used `documentSearch` is a private turn, exactly like one that used `activityLog` (`memory/activity_log.spec.md`, Diary and graph): the tool marks it, its messages stay in the hot window for follow-ups but out of the diary and therefore out of the knowledge graph, its reply is not printed to the console log, and follow-ups answered from the carried-over result are private too.
- **Shared dialogue.** Private turns stay out of the recent dialogue sent to Codex or Claude unless at least one opt-in private source (the activity log, document search, the calendar, email) allows cloud sharing and every one that is set up does (`bridge.settings.share_private_turns`, `bridge/bridge.spec.md`): with document search on and its sharing off, no private turn is shared, whatever the other sources' switches say.
- **Logs** record counts and reason codes (files indexed, skipped, removed, passages returned, why a pass stopped), never names, paths, queries or text. The tag is `documents`.
- Turning document search off stops indexing and removes the tool at the next start. The index stays until deleted from the tray or the CLI.

## Tray

- `Rebuild Document Index`: deletes the index and starts a fresh pass. Disabled, with a pointer to Settings, while document search is off.
- `Delete Document Index…`: asks for confirmation (No is the default) and deletes every stored passage. Available while document search is off, so an old index can still be removed. With document search on, the next pass builds it again.
- Both only request the change: bundled mode calls `jarvis.daemon.rebuild_document_index` and `delete_document_index` on a worker thread; subprocess mode writes `__DOCUMENTS__:{"action": "rebuild" | "delete" | "stop_sharing"}` to the daemon's stdin; with no daemon running, delete changes the database directly and rebuild waits for the next start.
- Saving Settings with `Share With Codex and Claude` switched off sends `stop_sharing` while Jarvis runs. Switching it on takes effect at the next start.
- Voice and text cannot rebuild or delete: the tool is read only.

## CLI

`python -m jarvis.documents <status | index | search QUERY | delete>` with optional `--folder PATH` (repeatable) and `--db PATH` overriding the configured folders and database, for checking indexing on a test folder. `index` runs one pass in the foreground and prints counts; `search` prints the passages; `delete` asks for no confirmation (it is a developer tool). It works with document search off.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `document_search_enabled` | `false` | Master switch; only a real `true` opts in. Takes effect when Jarvis starts |
| `document_search_folders` | `["Documents"]` | Folders to index, inside the home folder |
| `document_search_share_with_cloud` | `false` | Whether Codex and Claude reply modes are offered `documentSearch` |
| `document_search_max_file_mb` | 20 | Larger files are skipped (at least 1) |
| `document_search_rescan_minutes` | 60 | Minutes between passes (at least 5) |

Each has a Settings row in the Document search section of the Privacy page.

## Language

No language patterns decide anything. Matching is by embedding similarity, so queries and documents in any language the embedding model covers work; nothing is stemmed or keyword-matched.

## Limits

- No OCR: scanned PDFs and images are not searchable.
- `.doc`, `.odt`, `.rtf` and spreadsheets are not indexed.
- The first pass over a large folder takes as long as embedding every passage on this PC; answers improve as it progresses.
- Retrieval quality depends on the embedding model.
- A private turn does not record which source it quoted, and document search or the activity log, once turned off, is not consulted by `share_private_turns`. A web chat reopened from the archive can therefore hold private turns from one of them after it has been turned off, and they are shared when every source still set up is shared.

## Verification contract

Automated tests (no real documents, no network) assert:

- Extraction of each format from generated sample files (text with byte-order marks, a PDF with pages, a `.docx` built in the test), size and page bounds, an oversized `.docx` part refused, chunk size, overlap and per-file caps.
- Scanning: extensions, hidden and dot folders, build folders, Office lock files, cloud-only and hidden attributes (with a fake attribute reader), the file count cap, the home boundary for configured folders.
- Store: own tables beside existing ones, replace and remove, cosine ranking, at most two passages per file, other-length vectors ignored, delete-all leaving no readable text in the file.
- Indexer with a fake embedder: first pass, unchanged files not reopened, changed files re-embedded, deleted files removed, oversized and empty files skipped, embedding failure stops and resumes, model change rebuilds, busy waits, the total cap, stop between passages, logs without names or text.
- Tool: registration only when enabled, fenced results with file and page, marker spoofing defanged, empty-index and unavailable-embedding messages, the private-turn mark, limit bounds.
- Cloud boundary: withheld from the Codex and Claude snapshots by default and for anything but a real `true`; offered when on; runtime stop; shared private turns follow `share_private_turns`.
- Config defaults and bounds; Settings rows; tray items, confirmation default, request paths and IPC lines.
- Evals (`evals/test_document_search_routing.py`, live local model): document-content questions route to `documentSearch`; finding files by name, web questions and the open PDF route to their own tools.

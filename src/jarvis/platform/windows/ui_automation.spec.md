# UI Automation and PDF navigation

`uiControl` reads any application's accessibility tree through Microsoft UI Automation (UIA) and acts on its controls by name, so Jarvis can drive applications that have no dedicated tool. `pdfNavigate` finds and shows pages of the PDF open in the user's viewer. Both follow the two-layer rule of `windows.spec.md`: OS functions in `jarvis.platform.windows` know nothing about tools, replies or models; thin adapters in `jarvis.tools.builtin.windows` turn them into the strings the model sees.

## Boundaries

- `platform/windows/ui_automation.py` (UIA client), `platform/windows/pdf_viewer.py` (viewer, file and page box) and `utils/pdf_document.py` (pure `pypdf` reading) import nothing from `jarvis.tools`, `jarvis.reply` or `jarvis.llm`. OS libraries are imported inside functions.
- UIA is used through `comtypes` and the system `UIAutomationCore` type library. The `uiautomation` package is not used: importing it changes the process-wide DPI awareness, which Qt owns, and initialises COM on the importing thread.
- Every OS call runs on a daemon worker with a hard deadline (`run_bounded`, twelve seconds per tool call). COM is initialised on that worker for its lifetime and every UIA object is released before the apartment is left. The UIA client sets a two-second connection timeout and a three-second transaction timeout, so a hung application fails one call instead of the whole deadline.
- Actions use UIA control patterns only: Invoke, Value, RangeValue, Toggle, SelectionItem, ExpandCollapse, Scroll and Text. Jarvis never synthesises mouse or keyboard input, never moves the pointer and never focuses or activates a window for this tool. An action that would need focus or typed keys returns an honest failure that says so.
- Both tools are registered with the other Windows tools: Windows only, `windows_tools_enabled` (default `true`). A disabled tool returns a failure naming the key and touches nothing.
- Results pass through the shared redaction function before model consumption. Debug logs carry actions, counts, control types and exception types, never names, values, titles, paths or page text.

## Which window

- `window` empty: the window the user is working in. This is the foreground window, unless it belongs to Jarvis itself (its own process, or its parent when the parent runs the same executable, such as the desktop app), in which case it is the topmost visible, titled, non-cloaked window of another process.
- `window` given: an application name, window title or decimal handle, resolved like `windowControl` (`resolve_window` over visible top-level windows; several matches ask for a handle).
- When the chosen window has an enabled modal dialog open (`GetLastActivePopup`), the dialog is used, so "click Save in this dialog" addresses the dialog the user sees.

## uiControl

One tool with an `action` enum. Arguments: `action`, `window`, `element` (a snapshot id such as `e3`, or the control's visible name) and `value` (text to type, option to pick, menu path, toggle state or scroll direction).

| Action | Behaviour |
|--------|-----------|
| `snapshot` | Compact list of the window's interactive elements with short ids (`e1`, `e2` ...): type, name, value (never for password fields), state (`on`/`off`, `expanded`/`collapsed`, `selected`) and `enabled: false` when disabled, plus up to eight static texts (dialog messages) and the window's handle, process and title |
| `click` | Invokes the element: Invoke, else Toggle, else SelectionItem select, else ExpandCollapse expand/collapse. `invoke` is accepted as an alias |
| `set_text` | Replaces the element's text through the Value pattern and reads it back. Read-only fields fail |
| `select` | Selects the element (a list, tab or tree item), or, with `value`, the item of that name inside the element (a list, combo box or tab strip). A collapsed combo box is expanded, the item selected and the box collapsed again. The selection is read back from the control: Win32 list and combo boxes change selection through UIA without sending the application its selection-change notification, so an application that only reacts to that notification may not notice |
| `toggle` | Toggles a check box or toggle button; with `value` `on` or `off`, toggles only until it is in that state |
| `expand` | Expands the element (tree item, combo box, menu item, group) |
| `menu` | `value` is a path such as `File > Save As`. See Menus |
| `read` | The element's text: Text pattern (first 4000 characters), else its value, else its name. Without an element, the window's static texts and document text up to the same limit |
| `scroll` | `value` `up`, `down`, `left` or `right` scrolls one page; `top` or `bottom` jumps to the end. The element, or without one the first scrollable element, must offer the Scroll pattern. The resulting scroll percentages are returned |

### Snapshots

- A window whose process runs as administrator while Jarvis does not cannot be read or driven: Windows' user interface privilege isolation hides its controls, so a walk would find nothing. `snapshot` and every action check the window's process token first and fail saying the window runs as administrator, rather than reporting an empty window. A token that cannot be read counts as not elevated.
- Elements are read from the UIA control view in document order, one cross-process call per parent with the needed properties cached. The walk is depth-limited (twelve levels), visits at most 600 nodes and stops at a five-second budget; a cut-short snapshot says `truncated: true`.
- Interactive elements are buttons, check boxes, radio buttons, combo boxes, edits, hyperlinks, list, tree, tab, menu and data items, sliders, spinners, split buttons, documents, and any element offering an action pattern. Window chrome (title bar, scroll bars, thumbs, separators, tooltips) is never a target, and the internals of combo boxes and edits are not walked. An unnamed element carries its automation id as `hint` when that is not just a number.
- At most 60 elements are listed. On-screen elements are preferred; elements UIA reports as off-screen fill the remaining places. Names, values and texts are truncated to bound the result for an 8192-token context.
- The model-facing snapshot carries a note that nothing has changed yet and that acting means calling `uiControl` again with an id, so a reply never reports a snapshot as the action itself.
- Requests that name a control are acted on by name in one call; the tool description puts the visible name first and keeps snapshots for controls whose names are unknown, so a model does not invent ids it has not seen.
- Ids are valid only for the latest snapshot. A snapshot replaces the previous one; the store keeps each element's child-index path and runtime id, never a COM object. An id is followed by its path and verified against its runtime id; when the tree has changed it is searched for by runtime id, and a vanished element is an error that asks for a new snapshot.

### Elements by name

- A name matches after case folding, ignoring access-key ampersands, a trailing colon and trailing ellipses. Exact matches win; otherwise an element whose name contains every word of the request matches. Matching uses Unicode word characters and no language-specific rules.
- Several matching elements are never resolved arbitrarily: the result lists them with fresh ids (this becomes the latest snapshot) so the next call can pick one. Enabled, on-screen elements are preferred only to break ties between otherwise identical matches.

### Menus

- A classic Win32 menu bar (`GetMenu`) is walked by item text with the same name normalisation (accelerator text after a tab is ignored). A command item is executed by posting its `WM_COMMAND` to the window, so nothing opens on screen and no focus is needed. A disabled item is refused. A path ending at a submenu, or a window without a Win32 menu (WinUI, WPF, Office ribbons), uses UIA: each intermediate item is expanded and the next is searched in the window and in the popup menus of the same process; the last item is invoked, or expanded when it is itself a submenu ("open the Format menu").
- When an item is not found the result names the items that exist at that level.

### Safety

`windows.spec.md` makes Windows tools routine. `uiControl` is the exception; its `classify_safety` declares:

| Tier | When |
|------|------|
| DENY | `set_text` or `read` on a password field (`IsPassword`) |
| CONFIRM_VOICE | `click`, `toggle` or `menu` on a control whose name (or any menu path segment) matches the irreversible-action table: send, submit, delete, buy, pay, confirm, uninstall and the like; or on a generic acceptance (yes, OK, continue) in a window whose static text matches that table |
| SAFE | everything else, including `snapshot`, `read`, `scroll`, `select`, `expand` and `set_text` on ordinary fields |

- The table is locale data in `tools/builtin/windows/control_words/<language>.json` (`irreversible` and `generic_accept` word lists). All shipped languages are used together, because an application's interface language is independent of the language spoken. Matching is on whole words and word sequences, never substrings.
- The element is looked up for classification: an id from the latest snapshot, or, for a name, a bounded UIA lookup (three seconds) of the element and its window's static texts. If the lookup fails, the requested name is classified. Execution independently refuses to type into or read a password field.
- A menu path is classified by its real item names as well as the requested words: a bounded lookup (three seconds) reads the names without opening any menu (a classic menu bar whole, otherwise only items already on screen), so "File > file" reaching "Delete file" asks first, and the confirmation names the real item. Items that only appear once a menu is opened are checked again at execution: an item whose real name is irreversible, reached by requested words that are not, is refused with its full name and nothing runs. Asking for it by that name is then classified, and confirmed, as what it is.
- A confirmation names the control and the application (`click "Send" in outlook`). The central policy still applies: a control named for restarting, shutting down or uninstalling escalates to the desktop dialog.

### Desktop referents

Successful `click`, `set_text`, `select`, `toggle`, `expand`, `menu` and `scroll` record the window's top-level owner as a desktop referent with `last_action: control` (`memory/desktop_referents.spec.md`). `snapshot` and `read` record nothing.

## pdfNavigate

A separate tool rather than a `uiControl` action group: page and chapter requests ("go to page 42", "jump to the chapter on heat transfer") route to a tool whose description is about PDFs, and its schema (`page`, `query`) is far smaller than `uiControl`'s, which matters for small models choosing arguments. In `evals/test_pdf_navigate_routing.py` the separate tool routes and fills arguments at least as well as the action-group alternative with `qwen3.5:9b` and better with `qwen3.5:0.8b`, and it keeps `uiControl`'s schema unchanged.

| Action | Behaviour |
|--------|-----------|
| `goto` | Shows physical page `page` (1-based) of the open PDF |
| `find` | Searches the outline titles and the page text for `query`. When exactly one outline entry contains every word of the query, its page is shown immediately. Otherwise up to eight candidates are returned (outline entries first, then pages with text hits, each with a snippet) and nothing moves; the reply model picks one and calls `goto` |
| `outline` | The document's bookmarks: title, page and level, at most 80 |

"Go to page {number}" and "page {number}" are also fast commands (`fastpath/fastpath.spec.md`), offered only while a PDF viewer is the window the user is working in (`pdf_viewer.foreground_viewer`), so no model is involved.

`file` (optional) selects a PDF by path, for example one of several candidates the tool returned. It must be a path on this PC: a network (UNC) path is refused without being touched, because probing it would make Windows connect to that host with the user's credentials. Whether the file exists is checked on the bounded worker.

### Which PDF

- The viewer is the foreground PDF viewer (PDFgear, process `pdfeditor`, ProgId `PdfGear.App.1`; or Google Chrome or Microsoft Edge showing a PDF from this PC), chosen like `uiControl`'s window.
- A browser window counts only while its active tab shows a local PDF. Edge's title shows the file name, so a `.pdf` in the title is enough. Chrome's title shows the document's own title instead, so its address bar is read through UIA: a `file:` URL or local path ending in `.pdf`. A browser window the user is working in that shows no PDF is skipped rather than refused, unless it was named. The remaining windows are searched topmost first, reading the address bar of at most four Chrome windows.
- Chrome and Edge: the file comes from the address bar's `file:` URL or local path, read through UIA.
- PDFgear and others: the file name comes from the window title. It is resolved through the user's Recent items (`%APPDATA%\Microsoft\Windows\Recent`, shortcut targets) and the Desktop, Documents and Downloads folders (two levels deep, bounded). Exactly one existing file is used; several are returned as candidates with their paths and the user is asked; none is a failure asking for the path. Nothing is guessed.

### Navigation

1. The viewer's page box (PDFgear and Chrome): among the window's edits and spinners, the one whose numeric value lies within the document's page range (one whose automation id mentions a page is preferred). Its RangeValue or Value is set to the page and read back after a short settle. Success reports `method: page_box`. Whether the view itself moved cannot be observed through UIA; it was checked by hand for PDFgear and for Chrome, whose PDF viewer (`pageSelector`) moves to the page when its value is set.
2. Otherwise (no usable page box, Edge, whose page box changes its number but not the view without Enter, or the read-back differs) the file is opened at `file:///...#page=N`: in Chrome as a new tab when Chrome was the viewer (`method: opened_in_chrome`), otherwise in Microsoft Edge (`method: opened_in_edge`), so the reply can say so.
3. With `file` given and no viewer showing it, the file is opened in Edge at the page.

### Document data

- `utils/pdf_document.py` reads the outline (with destination page numbers) and page text with `pypdf`, locally and deterministically. Documents are cached per path, size and modification time (three most recent).
- Text search tokenises with Unicode word characters, case-folded. A query word matches a text word that equals it or, for words of four or more characters, extends it or is extended by it by at most three characters ("transformers" finds "transformer"), with no language rules. A page matches when it contains every query word; pages are ranked by hit count. Snippets are about 160 characters around the first hit.
- Extraction runs within the tool's deadline: pages are extracted in order until a budget is spent, extracted text is kept, and a background worker continues extracting the same document after the call returns. A partial search says `pages_searched` of `page_count` so the reply can say the rest is still being indexed.

### Delegation and privacy

The outline and keyword hits resolve most requests with no model. For vague requests the tool returns candidate pages with snippets and the reply model chooses. In local mode nothing leaves the PC. In Codex or another cloud reply mode the cloud model does that choosing, so the snippets and snapshots it requests are sent to that provider as tool results, only because the user chose a cloud mode (`codex_bridge.spec.md`).

## Evals

`evals/test_ui_control_choices.py` replays recorded UIA snapshots (`evals/fixtures/uia/*.json`) as tool results and checks the reply model's next `uiControl` call (action, element id, value), and checks one-call requests that name a control through the router, offline: no window is touched. The Notepad, Save As, Settings, Apple Music and PDFgear fixtures are hand-written approximations until captured; `win32_controls` is a real capture of the test fixture window. `evals/test_pdf_navigate_routing.py` checks routing of page and chapter requests. `qwen3.5:0.8b` is unlikely to drive multi-step UI reliably; its measured scores are reported with the change. `scripts/capture_uia_snapshot.py` records a fixture from a real window (Notepad, Settings, Apple Music, PDFgear), redacted, for the user to run.

## Dependencies

`comtypes` (already required) for UIA and shell links; `pypdf` (BSD-3-Clause) for PDF reading.

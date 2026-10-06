# Windows workspaces

A workspace is a named desk set-up: a list of items (browser windows, applications) that are opened and placed on the user's displays in one request, such as "open my research workspace". Workspaces are configuration data. Adding one means editing `config.json`; no code or phrase table changes.

`platform/windows/workspaces.py` holds validation and orchestration over the existing placement code (`displays.py`, `windows_mgmt.py`, `apps.py`). It imports nothing from tools, replies or models. `tools/builtin/windows/workspace_control.py` is the thin `Tool` adapter. Everything in `apps_paths.spec.md` about displays, zones, FancyZones, verified placement and "one launch, never relaunch" applies unchanged.

## Configuration

`windows_workspaces` maps a workspace name to a definition. Defaults are not written and unknown keys are preserved. There is no settings screen; it is edited in `config.json`.

```json
{
  "windows_workspaces": {
    "research": {
      "aliases": ["study"],
      "items": [
        {"kind": "browser_window", "label": "papers",
         "urls": ["C:\\Papers\\paper.pdf", "C:\\Papers\\notes.pdf"],
         "monitor": "primary", "zone": "left"},
        {"kind": "browser_window", "label": "ChatGPT",
         "urls": ["https://chatgpt.com"],
         "monitor": "primary", "zone": "right"}
      ]
    }
  }
}
```

- `aliases` is an optional list of other names. Names and aliases compare case-insensitively after Unicode (NFKC) normalisation, by the rules in `utils/names.py` that routines share. A name or alias that two workspaces both claim is offered for neither: a workspace whose own name is contested is unavailable, and a contested alias is removed. Both are logged as counts.
- Every item has `kind`, optional `label` and the placement fields below. `label` is how the item is named in results and in the desktop record; without one it is derived from the kind and position (`browser window 2`), or the application name for `app`.
- `browser_window`: `urls` (a non-empty list of `http(s)` URLs and/or local file paths, opened as tabs of one new window) and optional `browser`. Local paths may use `%ENVVARS%` and `~`.
- `app`: `target`, an application name resolved like `appControl open` (aliases from `windows_app_aliases` apply).
- Placement on every item: `monitor` (required; any value `windowControl place` accepts: device, alias, number, `primary`, `left`, `right`), optional `zone` and optional `state` (`restore`, the default, or `maximise`).
  - `zone` is a name or number (configured zone or FancyZones zone for that display) or an inline `[x, y, width, height]` of fractions of the work area for when no named zone fits. `maximise` cannot be combined with a zone.
- Loading drops, with counts only logged, a workspace that is not an object, has no usable name, has no items, or has an item of unknown kind or the wrong shape (a missing or non-text `monitor`, an empty `urls`, a `zone` that is neither text nor four numbers). It is a structural check; whether files exist and displays resolve is decided when the workspace is opened.
- `folder` and `file` items are not part of this version.

## Validation before launching

Opening a workspace first validates every item against the live state, and launches nothing until all pass:

- Local files exist (a file, not a folder) and are not executable or script types (the `openPath` rule); URLs are `http` or `https` with a host; any other scheme, including `file:`, `javascript:` and `data:`, is rejected.
- The browser resolves to an installed executable.
- Each `app` resolves in the application catalogue (an unready catalogue fails the whole workspace as "still loading").
- Each `monitor` resolves on the connected displays, each zone resolves for that display (FancyZones read once for the whole workspace), each rectangle and state passes `windows_mgmt.validate_placement`.

One failed check fails the whole workspace with no side effect. The error names the item (its label and position) and the reason. It never contains paths, file names or URLs: a missing file is reported as "file 2 of 2 was not found". Reasons from operating-system errors drop the file name those errors may carry.

## Browsers

- `browser` omitted: the default browser for `https` links when it is Chrome, Edge, Brave or Vivaldi, otherwise Chrome. Explicit values are the names of those browsers; any other value is a validation error. The executable comes from the browser's App Paths registration or its usual install folder, never from user text.
- Each `browser_window` item launches the browser once with `--new-window` and the item's URLs (file paths converted to `file:` URLs).
- Launching a browser window and finding and placing it (`open_browser_window`) is shared with `openWebsite`, which opens one web address the same way.

## Launch and placement

Items run in order inside one monotonic deadline (45 seconds for the whole workspace; the tool's outer limit sits just above it). A process-wide lock admits one workspace launch at a time, and is shared with placed `openWebsite` launches (`apps_paths.spec.md`, Websites); a concurrent request fails at once as busy and launches nothing.

- **browser_window**: before launching, the visible top-level windows of the browser's process are snapshotted. After launching, polling finds windows of that process that were not in the snapshot; the target is the one that stays unchanged for half a second. The wait for an item is at most 15 seconds and never exceeds the overall deadline. Exactly one new window is placed with `place_window` (restore, move, verify). Several new windows are never resolved arbitrarily: the item is `failed` with an ambiguity reason.
- **app**: the application's open windows are listed. Exactly one existing window is placed (`placed_existing`, no launch). Several existing windows fail that item as ambiguous. None means launch once and place the new window as `appControl open` with a monitor does (`opened_and_placed`).
- A launch is never repeated, whatever happens afterwards. A placement failure or an unverified placement is a `failed` item recorded as launched, and the next item still runs. When the deadline expires, items not yet started are `failed` with reason `not started: out of time` and launch nothing.
- Item outcomes: `opened_and_placed`, `placed_existing` or `failed`. A successful item reports `hwnd`, `process`, `monitor`, `zone` (the resolved label, when a zone was used), the verified `rectangle` and `state`. A failed item reports `reason`, and `launch: accepted` when something was launched.

## Tool

`workspaceControl` is a separate tool so that `appControl` and `windowControl` keep their short, router-sensitive descriptions. It is registered only on Windows with `windows_tools_enabled` and only when `windows_workspaces` has at least one usable workspace. It is a routine local action: no confirmation (central safety classifies it `SAFE`).

| Argument | Meaning |
|----------|---------|
| `action` | `open` or `list` |
| `target` | A workspace name or alias for `open`; empty for `list` |

- `open` returns `action: workspace_opened` when every item succeeded, otherwise a failure (`success: false`) whose text is the same structure: the canonical `workspace` name and `items`, each with `label`, `kind` and the outcome fields above. The result never contains paths, file names or URLs.
- An unknown workspace fails and lists the available names. `list` returns each workspace's name, aliases and item labels and kinds, nothing else.
- Results pass through the usual redaction before model consumption, in local and Codex mode alike.

## Desktop record

Each placed or reused window is recorded in the desktop referents (`memory/desktop_referents.spec.md`) as `application` = the item's label, `process`, `hwnd`, `monitor`, `zone`, `state` and `last_action`, so "move the ChatGPT one to monitor 2" resolves to that window afterwards. A failed item whose window was found but not placed is recorded with its handle only. An item whose window was never found records nothing.

## Fast path

Workspace names are config data, so opening one by name is a deterministic fast command (`fastpath.spec.md`). `workspaceControl.fast_targets(cfg)` offers each workspace's name and aliases; the matcher resolves a `{workspace}` slot with the same spelling tolerance and margin as application names. The verb phrases ("open my {workspace} workspace", "set up {workspace}", "start the {workspace} workspace") live in `fastpath/phrases/<language>.json`; there is no English in code. A bare "open {workspace}" is not a template, so it cannot collide with application names. Anything unclear (an ambiguous name, an extra clause, a destination) falls through to the model, which can call `workspaceControl list` and `open`. Success uses a short locale template; a failure returns the tool's structured result unchanged.

## Privacy

- File paths and URLs live only in the user's `config.json`. Debug logs record counts, item kinds and outcomes, never paths, file names, URLs, labels or window titles.
- Tool results and errors, which go to OpenAI in Codex mode, carry labels, kinds and outcomes only.
- The only information sent anywhere is what a browser sends when it loads the user's own URLs. Jarvis adds no network call.

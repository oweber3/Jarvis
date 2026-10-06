"""Named workspaces: browser windows and applications opened and placed in one request.

Validation and orchestration over the existing placement code. Imports nothing from tools,
replies or models. See ``workspaces.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from . import apps, displays
from ...debug import debug_log
from ...utils import names

KINDS = ('browser_window', 'app')
DEADLINE_SEC = 45.0
BROWSER_WINDOW_WAIT_SEC = 15.0
_POLL_SEC = 0.1
_STABLE_SEC = 0.5  # a new window must stop changing, so a transient window is not mistaken for it
_launch_lock = threading.Lock()


class WorkspaceError(apps.PartialPlacementError):
    """A workspace was opened but not every item completed. ``data`` is the structured outcome."""


# --- configuration -------------------------------------------------------------------

def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_item(raw, position: int):
    """The item with its shape checked, or ``None``. Whether files exist and displays resolve is
    decided when the workspace is opened."""
    if not isinstance(raw, dict) or raw.get('kind') not in KINDS or not _text(raw.get('monitor')):
        return None
    zone, state, label = raw.get('zone'), raw.get('state'), raw.get('label')
    if zone is not None and not (_text(zone) or (isinstance(zone, list) and len(zone) == 4 and all(map(_number, zone)))):
        return None
    if state is not None and not isinstance(state, str) or label is not None and not isinstance(label, str):
        return None
    item = {'kind': raw['kind'], 'monitor': raw['monitor'].strip()}
    if zone is not None:
        item['zone'] = zone.strip() if isinstance(zone, str) else list(zone)
    if state is not None:
        item['state'] = state.strip()
    if raw['kind'] == 'browser_window':
        urls, browser = raw.get('urls'), raw.get('browser')
        if not (isinstance(urls, list) and urls and all(_text(url) for url in urls)):
            return None
        if browser is not None and not _text(browser):
            return None
        item['urls'] = [url.strip() for url in urls]
        if browser is not None:
            item['browser'] = browser.strip()
        item['label'] = (label or '').strip() or f'browser window {position}'
    else:
        if not _text(raw.get('target')):
            return None
        item['target'] = raw['target'].strip()
        item['label'] = (label or '').strip() or item['target']
    return item


def load_workspaces(value) -> dict[str, dict]:
    """Keep only well-formed workspaces. Names and aliases that two workspaces claim are not offered.

    Dropped entries are counted in the debug log, never named; the user's file is untouched."""
    if not isinstance(value, dict):
        return {}
    loaded, dropped = {}, 0
    for name, raw in value.items():
        items = raw.get('items') if isinstance(raw, dict) else None
        parsed = [_load_item(item, number) for number, item in enumerate(items, 1)] if isinstance(items, list) else []
        if not _text(name) or not parsed or None in parsed:
            dropped += 1
            continue
        aliases = raw.get('aliases')
        aliases = [alias.strip() for alias in aliases if _text(alias)] if isinstance(aliases, list) else []
        loaded[name.strip()] = {'aliases': aliases, 'items': parsed}
    claimed, contested = names.drop_contested({name: definition['aliases'] for name, definition in loaded.items()})
    dropped += contested
    kept = {name: {'aliases': aliases, 'items': loaded[name]['items']} for name, aliases in claimed.items()}
    if dropped:
        debug_log(f'Ignored {dropped} invalid or duplicate workspace entries.', 'config')
    return kept


def resolve_workspace(name: str, workspaces: dict) -> tuple[str, dict]:
    """The canonical name and definition for a name or alias, ignoring case."""
    available = ', '.join(sorted(workspaces)) or 'none configured'
    if not (isinstance(name, str) and names.key(name)):
        raise ValueError(f'A workspace name is required. Available workspaces: {available}')
    found = names.resolve(name, {workspace: definition['aliases'] for workspace, definition in workspaces.items()})
    if found is None:
        raise ValueError(f'Unknown workspace. Available workspaces: {available}')
    return found, workspaces[found]


def list_workspaces(workspaces: dict) -> dict:
    """Names, aliases and item labels and kinds, and nothing else from the configuration."""
    return {'workspaces': [
        {'name': name, 'aliases': list(definition['aliases']),
         'items': [{'label': item['label'], 'kind': item['kind']} for item in definition['items']]}
        for name, definition in workspaces.items()]}


# --- browsers ------------------------------------------------------------------------

@dataclass(frozen=True)
class Browser:
    name: str
    process: str  # executable stem of its windows
    executable: str


# name: (process stem, executable name, default-browser ProgId prefix, usual install locations)
_BROWSERS = {
    'chrome': ('chrome', 'chrome.exe', 'ChromeHTML',
               (r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe',
                r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe',
                r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe')),
    'edge': ('msedge', 'msedge.exe', 'MSEdgeHTM',
             (r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe',
              r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe')),
    'brave': ('brave', 'brave.exe', 'BraveHTML',
              (r'%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe',
               r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe')),
    'vivaldi': ('vivaldi', 'vivaldi.exe', 'VivaldiHTM',
                (r'%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe',
                 r'%PROGRAMFILES%\Vivaldi\Application\vivaldi.exe')),
}


def _default_browser_key() -> str | None:
    """The Chromium browser the user's default for https links, or ``None`` for any other."""
    try:
        import winreg
        path = r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            prog_id = winreg.QueryValueEx(key, 'ProgId')[0]
    except (ImportError, OSError):
        return None
    return next((name for name, spec in _BROWSERS.items() if str(prog_id).startswith(spec[2])), None)


def _locate_browser(key: str) -> str | None:
    """The installed executable, from its App Paths registration or a usual install location."""
    _, executable, _, locations = _BROWSERS[key]
    candidates = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}') as entry:
                    candidates.append(os.path.expandvars(str(winreg.QueryValueEx(entry, '')[0]).strip('"')))
            except OSError:
                continue
    except ImportError:
        pass
    candidates.extend(os.path.expandvars(location) for location in locations)
    return next((candidate for candidate in candidates if candidate and os.path.isfile(candidate)), None)


def find_browser(name: str | None = None) -> Browser:
    """The named Chromium browser, or the default browser when it is one, else Chrome."""
    if name:
        key = names.key(name)
        if key not in _BROWSERS:
            raise ValueError('Unknown browser. Use one of: ' + ', '.join(_BROWSERS) + '.')
        located = _locate_browser(key)
        if not located:
            raise ValueError(f'The {key} browser was not found.')
        return Browser(key, _BROWSERS[key][0], located)
    default = _default_browser_key()
    for key in (default, 'chrome'):
        located = _locate_browser(key) if key else None
        if located:
            return Browser(key, _BROWSERS[key][0], located)
    raise ValueError('No supported browser was found.')


def browser_for_executable(executable: str) -> Browser | None:
    """The supported Chromium browser ``executable`` is, or ``None`` for any other program."""
    stem = Path(executable).stem.casefold()
    return next((Browser(key, spec[0], executable) for key, spec in _BROWSERS.items() if spec[0] == stem), None)


def _spawn(command: list[str]) -> None:
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- validation ----------------------------------------------------------------------

@dataclass(frozen=True)
class _Step:
    number: int
    label: str
    kind: str
    monitor: displays.Monitor
    rectangle: displays.Rectangle | None
    zone: str | None
    state: str
    urls: tuple[str, ...] = ()
    browser: Browser | None = None
    target: str = ''


def _browser_url(text: str, number: int, total: int) -> str:
    """An http(s) URL as given, or a local document as a ``file:`` URL. Nothing else is opened."""
    from .files import is_executable_file  # OS libraries load only when a workspace is opened
    text = text.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        parsed = None
    scheme = parsed.scheme.casefold() if parsed else ''
    if scheme in ('http', 'https'):
        if not parsed.hostname or any(char.isspace() for char in text):
            raise ValueError(f'url {number} of {total} is not a valid http(s) URL.')
        return text
    drive_letter = len(scheme) == 1 and len(text) > 2 and text[1] == ':'
    if parsed is None or (scheme and not drive_letter):
        raise ValueError(f'url {number} of {total} must be an http(s) URL or a local file path.')
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_file():
        raise ValueError(f'file {number} of {total} was not found.')
    path = path.resolve()
    if is_executable_file(path):
        raise ValueError(f'file {number} of {total} is an executable or script, not a document.')
    return path.as_uri()


def plan_workspace(name: str, definition: dict, *, monitors, monitor_aliases: dict, zones: dict,
                   fancy: Callable[[], dict], applications: Callable[[], list], app_aliases: dict) -> list[_Step]:
    """Check every item against the live displays, files, browsers and applications.

    Raises a ``ValueError`` naming the first item that fails; nothing has been launched by then."""
    from . import windows_mgmt as wm
    fancy_sets: list[dict] = []
    steps = []
    for number, item in enumerate(definition['items'], 1):
        try:
            monitor = displays.resolve_monitor(item['monitor'], monitors, monitor_aliases)
            zone, rectangle = item.get('zone'), None
            label = None
            if isinstance(zone, str):
                if not fancy_sets:
                    fancy_sets.append(fancy() or {})
                label, rectangle = displays.resolve_zone(zones, monitor, zone, fancy_sets[0].get(monitor.device))
            elif zone is not None:
                rectangle = displays.zone_rectangle(monitor, zone)
            state = item.get('state') or 'restore'
            wm.validate_placement(monitor, rectangle, state)
            common = dict(number=number, label=item['label'], kind=item['kind'], monitor=monitor,
                          rectangle=rectangle, zone=label, state=state)
            if item['kind'] == 'browser_window':
                total = len(item['urls'])
                urls = tuple(_browser_url(url, position, total) for position, url in enumerate(item['urls'], 1))
                steps.append(_Step(**common, urls=urls, browser=find_browser(item.get('browser'))))
            else:
                apps.resolve_application(item['target'], applications(), app_aliases)  # fail early, before any launch
                steps.append(_Step(**common, target=item['target']))
        except ValueError as exc:
            raise ValueError(f'Workspace "{name}", item {number} ("{item["label"]}"): {exc}') from None
    return steps


# --- launching -----------------------------------------------------------------------

def _reason(exc: Exception) -> str:
    """An error's explanation without the file name an operating system error may carry."""
    if isinstance(exc, OSError) and exc.filename:
        return exc.strerror or 'The request failed.'
    return str(exc) or type(exc).__name__


def _failed(step: _Step, reason: str, *, launched: bool = False, hwnd: int | None = None,
            process: str = '') -> dict:
    result = {'label': step.label, 'kind': step.kind, 'outcome': 'failed', 'reason': reason}
    if launched:
        result['launch'] = 'accepted'
    if hwnd is not None:
        result.update(hwnd=hwnd, process=process)
    return result


def _succeeded(step: _Step, outcome: str, hwnd: int, process: str, placed: dict) -> dict:
    result = {'label': step.label, 'kind': step.kind, 'outcome': outcome, 'hwnd': hwnd, 'process': process,
              'monitor': placed['monitor'], 'rectangle': placed['rectangle'], 'state': placed['state']}
    if step.zone:
        result['zone'] = step.zone
    return result


def _await_new_window(process: str, known: set, limit: float) -> list:
    """New windows of ``process`` once they stop changing, or whatever exists at ``limit``."""
    from . import windows_mgmt as wm
    last_ids, stable_since = None, 0.0
    while True:
        now = time.monotonic()
        new = sorted((window for window in wm.list_windows()
                      if window.process.casefold() == process and window.hwnd not in known),
                     key=lambda window: window.hwnd)
        ids = tuple(window.hwnd for window in new)
        if ids:
            if ids != last_ids:
                last_ids, stable_since = ids, now
            elif now - stable_since >= _STABLE_SEC:
                return new
        else:
            last_ids = None
        if now >= limit:
            return new
        time.sleep(max(0.0, min(_POLL_SEC, limit - now)))


class BrowserWindowError(apps.PartialPlacementError):
    """The browser was launched but its new window was not placed. ``data`` holds ``launch: accepted``,
    ``placement`` (``unverified``, ``ambiguous`` or ``failed``), ``reason`` and, once found, the window."""


def open_browser_window(browser: Browser, urls, monitor, rectangle, state: str, deadline: float) -> dict:
    """Launch ``browser`` once with a new window of ``urls``, find that window and place it.

    Returns the placed ``hwnd``, ``process``, ``monitor``, ``rectangle`` and ``state``. A launch the
    system refuses raises ``OSError`` (nothing opened); after an accepted launch every other outcome
    raises ``BrowserWindowError`` and nothing is launched again."""
    from . import windows_mgmt as wm
    known = {window.hwnd for window in wm.list_windows() if window.process.casefold() == browser.process}
    _spawn([browser.executable, '--new-window', *urls])
    windows = _await_new_window(browser.process, known,
                                min(deadline, time.monotonic() + BROWSER_WINDOW_WAIT_SEC))
    accepted = {'launch': 'accepted'}
    if not windows:
        raise BrowserWindowError('No new window appeared in time.', {
            **accepted, 'placement': 'unverified', 'reason': 'No new window appeared in time.'})
    if len(windows) > 1:
        raise BrowserWindowError('Several new windows appeared; none was chosen.', {
            **accepted, 'placement': 'ambiguous', 'reason': 'Several new windows appeared; none was chosen.'})
    window = windows[0]
    try:
        placed = wm.place_window(window.hwnd, monitor, rectangle, state, deadline=deadline)
    except (OSError, ValueError) as exc:
        raise BrowserWindowError(_reason(exc), {**accepted, 'placement': 'failed', 'reason': _reason(exc),
                                                'hwnd': window.hwnd, 'process': window.process}) from None
    return {**placed, 'hwnd': window.hwnd, 'process': window.process}


def _run_browser_window(step: _Step, deadline: float) -> dict:
    try:
        placed = open_browser_window(step.browser, step.urls, step.monitor, step.rectangle, step.state, deadline)
    except BrowserWindowError as exc:
        return _failed(step, exc.data['reason'], launched=True, hwnd=exc.data.get('hwnd'),
                       process=exc.data.get('process', ''))
    return _succeeded(step, 'opened_and_placed', placed['hwnd'], placed['process'], placed)


def _run_app(step: _Step, deadline: float, app_aliases: dict) -> dict:
    from . import windows_mgmt as wm
    app = apps.resolve_application(step.target, apps.APP_INDEX.applications(), app_aliases)
    existing = [window for window in wm.list_windows() if apps.window_belongs(app, window)]
    if len(existing) > 1:
        return _failed(step, 'Several windows of this application are open; none was chosen.')
    if existing:
        window = existing[0]
        try:
            placed = wm.place_window(window.hwnd, step.monitor, step.rectangle, step.state, deadline=deadline)
        except (OSError, ValueError) as exc:
            return _failed(step, _reason(exc), hwnd=window.hwnd, process=window.process)
        return _succeeded(step, 'placed_existing', window.hwnd, window.process, placed)
    try:
        opened = apps.open_application_placed(step.target, app_aliases, step.monitor, step.rectangle, step.state,
                                              deadline=deadline)
    except apps.PartialPlacementError as exc:
        return _failed(step, exc.data.get('reason') or _reason(exc), launched=True, hwnd=exc.data.get('hwnd'),
                       process=app.name)
    return _succeeded(step, 'opened_and_placed', opened['hwnd'], opened['process'], opened)


def _run_step(step: _Step, deadline: float, app_aliases: dict) -> dict:
    if time.monotonic() >= deadline:
        return _failed(step, 'Not started: out of time.')
    try:
        if step.kind == 'browser_window':
            return _run_browser_window(step, deadline)
        return _run_app(step, deadline, app_aliases)
    except (OSError, ValueError) as exc:
        return _failed(step, _reason(exc))
    except Exception as exc:  # noqa: BLE001 - one item never stops the others
        debug_log(f'Workspace item failed unexpectedly ({type(exc).__name__}).', 'windows')
        return _failed(step, 'The item could not be completed.')


def open_workspace(name: str, definition: dict, *, monitors, monitor_aliases: dict, zones: dict,
                   fancy: Callable[[], dict], applications: Callable[[], list], app_aliases: dict,
                   deadline_sec: float = DEADLINE_SEC) -> dict:
    """Validate every item, then launch and place them in order within one deadline.

    Nothing launches unless the whole workspace validates. Each item is launched once and never
    again; a failed item is reported and the next one still runs."""
    if not _launch_lock.acquire(blocking=False):
        raise ValueError('A workspace is already opening. Wait for it to finish.')
    try:
        steps = plan_workspace(name, definition, monitors=monitors, monitor_aliases=monitor_aliases, zones=zones,
                               fancy=fancy, applications=applications, app_aliases=app_aliases)
        kinds = ', '.join(f'{sum(step.kind == kind for step in steps)} {kind}' for kind in KINDS)
        debug_log(f'Workspace validated; opening {len(steps)} item(s): {kinds}.', 'windows')
        deadline = time.monotonic() + deadline_sec
        items = []
        for step in steps:
            items.append(_run_step(step, deadline, app_aliases))
            debug_log(f'Workspace item {step.kind} finished: {items[-1]["outcome"]}.', 'windows')
    finally:
        _launch_lock.release()
    done = sum(item['outcome'] != 'failed' for item in items)
    debug_log(f'Workspace finished: {done} of {len(items)} item(s) succeeded.', 'windows')
    if done == len(items):
        return {'action': 'workspace_opened', 'workspace': name, 'items': items}
    raise WorkspaceError(f'{len(items) - done} of {len(items)} workspace items did not complete.',
                         {'action': 'workspace_partial', 'workspace': name, 'items': items})

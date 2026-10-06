"""Installed application discovery and friendly-name resolution."""
from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import threading
import time

from ...debug import debug_log
from .steam import steam_applications


@dataclass(frozen=True)
class Application:
    name: str
    target: str
    executable: str = ''


def name_tokens(value: str) -> set[str]:
    return set(re.findall(r'\w+', value.casefold(), re.UNICODE))


def resolve_application(name: str, applications: list[Application], aliases: dict) -> Application:
    """Resolve an exact name or whole-token short name; never guess ambiguities."""
    query = name.strip().casefold()
    if not query:
        raise ValueError('An application name is required.')
    alias_map = {key.casefold(): value for key, value in aliases.items()}
    query = alias_map.get(query, query).strip().casefold()
    exact = [app for app in applications if app.name.casefold() == query or app.target.casefold() == query]
    launchers = [app for app in applications if app.executable and PureWindowsPath(app.executable).stem.casefold() == query]
    matches = exact or launchers or [app for app in applications if name_tokens(query) and
                                   name_tokens(query) <= name_tokens(app.name)]
    unique = {app.target.casefold(): app for app in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if not unique:
        raise ValueError(f'Application not found: {name}')
    raise ValueError('Ambiguous application: ' + ', '.join(sorted(app.name for app in unique.values())))


def discover_applications() -> list[Application]:
    """The full catalogue: Start Menu, App Paths, AppsFolder IDs, and installed Steam games."""
    applications = _discover_system_applications()
    try:
        games = steam_applications()
    except Exception as exc:  # noqa: BLE001 - Steam is optional and its files are foreign input
        debug_log(f'Steam discovery unavailable ({type(exc).__name__}).', 'windows')
        games = []
    known = {app.name.casefold() for app in applications}
    applications.extend(game for game in games if game.name.casefold() not in known)
    unique = {(app.name.casefold(), app.target.casefold()): app for app in applications}
    debug_log(f'Application index contains {len(unique)} entries.', 'windows')
    return list(unique.values())


def _discover_system_applications() -> list[Application]:
    """Read local Start Menu shortcuts, both App Paths views, and AppsFolder IDs."""
    import winreg
    applications = []
    for variable, suffix in [('APPDATA', 'Microsoft/Windows/Start Menu/Programs'),
                             ('PROGRAMDATA', 'Microsoft/Windows/Start Menu/Programs')]:
        root = os.environ.get(variable)
        if root:
            for shortcut in (Path(root) / suffix).rglob('*.lnk'):
                applications.append(Application(shortcut.stem, str(shortcut)))
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths',
                                    0, winreg.KEY_READ | view) as parent:
                    for i in range(winreg.QueryInfoKey(parent)[0]):
                        key = winreg.EnumKey(parent, i)
                        try:
                            with winreg.OpenKey(parent, key) as entry:
                                path, kind = winreg.QueryValueEx(entry, '')
                            if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(path, str):
                                path = os.path.expandvars(path).strip('"')
                                if Path(path).is_file():
                                    applications.append(Application(Path(key).stem, path, path))
                        except OSError:
                            continue
            except OSError:
                continue
    # Fixed script, never interpolate user text. This includes packaged applications.
    script = """[Console]::OutputEncoding=[Text.Encoding]::UTF8
$shell = New-Object -ComObject WScript.Shell
$roots = @([Environment]::GetFolderPath('StartMenu'), [Environment]::GetFolderPath('CommonStartMenu'))
$links = @(foreach ($root in $roots) {
    Get-ChildItem -LiteralPath $root -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        $shortcut = $shell.CreateShortcut($_.FullName)
        @{Path=$_.FullName; Executable=$shortcut.TargetPath}
    }
})
@{Apps=@(Get-StartApps); Links=$links} | ConvertTo-Json -Depth 4 -Compress
"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, encoding='utf-8', timeout=8, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        catalogue = json.loads(result.stdout.lstrip('\ufeff') or '{}')
        executables = {entry['Path'].casefold(): entry.get('Executable', '') for entry in catalogue.get('Links', [])}
        applications = [Application(app.name, app.target, executables.get(app.target.casefold(), app.executable))
                        for app in applications]
        entries = catalogue.get('Apps', [])
        # Shortcuts take precedence so the same app is not ambiguous across sources.
        names = {app.name.casefold() for app in applications}
        for entry in entries:
            if entry.get('Name') and entry.get('AppID') and entry['Name'].casefold() not in names:
                applications.append(Application(entry['Name'], 'shell:AppsFolder\\' + entry['AppID']))
                names.add(entry['Name'].casefold())
    except (OSError, subprocess.SubprocessError, ValueError):
        debug_log('Packaged application discovery unavailable; using shortcuts and App Paths.', 'windows')
    return applications


class ApplicationIndex:
    """A background-built local catalogue shared by application tool calls."""
    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._started = False
        self._applications = []
        self._error = None

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._build, name='windows-app-index', daemon=True).start()

    def _build(self):
        try:
            self._applications = discover_applications()
        except Exception as exc:
            self._error = exc
            debug_log('Application discovery failed.', 'windows')
        finally:
            self._ready.set()

    def applications(self):
        self.start()
        if not self._ready.wait(10):
            raise OSError('Application index is still loading. Try again shortly.')
        if self._error:
            raise OSError('Application discovery failed.') from self._error
        return list(self._applications)

    def snapshot(self):
        """Return the ready catalogue without starting discovery or waiting."""
        if not self._ready.is_set() or self._error:
            return None
        return tuple(self._applications)


APP_INDEX = ApplicationIndex()


def open_application(name: str, aliases: dict) -> dict:
    app = resolve_application(name, APP_INDEX.applications(), aliases)
    os.startfile(app.target)
    debug_log('Application launch requested.', 'windows')
    return {'action': 'open_requested', 'application': app.name}


# One deadline covers launch, window discovery and placement. It sits inside the tools'
# twelve-second outer limit so a structured partial failure can be returned.
_PLACE_BUDGET_SEC = 10.0
_POLL_SEC = 0.1
_STABLE_SEC = 0.5  # new windows must stop changing, so a splash screen is not mistaken for the app
_REUSE_GRACE_SEC = 2.0  # how long a single-instance app may take to show a new window


class PartialPlacementError(OSError):
    """A launch was accepted but the window could not be placed, or placement was not verified.

    ``data`` is the raw structured outcome returned to the model."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


def window_belongs(app: Application, window) -> bool:
    """Whether a window plausibly belongs to the application: executable stem when the
    catalogue knows it, otherwise the name's tokens within the title or process."""
    if app.executable:
        return PureWindowsPath(app.executable).stem.casefold() == window.process.casefold()
    wanted = name_tokens(app.name)
    return bool(wanted) and wanted <= (name_tokens(window.title) | name_tokens(window.process))


def track_launch(name: str, aliases: dict):
    """Note the application's open windows just before a plain launch.

    Returns ``(process, resolve)``: the executable stem ('' when unknown) and a function that,
    called later, lists the windows again and returns ``{'hwnd', 'process', 'monitor'}`` for the
    one window of the application that was not open before, or ``None``. Never guesses."""
    from . import displays, windows_mgmt as wm
    app = resolve_application(name, APP_INDEX.applications(), aliases)
    known = {w.hwnd for w in wm.list_windows() if window_belongs(app, w)}

    def resolve():
        new = [w for w in wm.list_windows() if window_belongs(app, w) and w.hwnd not in known]
        if len(new) != 1:
            return None
        window = new[0]
        try:
            monitor = displays.monitor_device_for_window(window.hwnd)
        except OSError:
            monitor = ''
        return {'hwnd': window.hwnd, 'process': window.process, 'monitor': monitor}

    return (PureWindowsPath(app.executable).stem if app.executable else ''), resolve


def _candidates(windows) -> list[dict]:
    return [{'hwnd': w.hwnd, 'title': w.title, 'process': w.process, 'pid': w.pid} for w in windows]


def _await_window(app: Application, known: set, deadline: float):
    """Poll for the application's window. Returns ``(windows, reused)``; several windows
    mean ambiguity and none means nothing suitable appeared. Never selects arbitrarily."""
    from . import windows_mgmt as wm
    started = time.monotonic()
    last_ids, stable_since = None, 0.0
    while True:
        now = time.monotonic()
        current = [w for w in wm.list_windows() if window_belongs(app, w)]
        new = sorted((w for w in current if w.hwnd not in known), key=lambda w: w.hwnd)
        existing = [w for w in current if w.hwnd in known]
        ids = tuple(w.hwnd for w in new)
        if ids:
            if ids != last_ids:
                last_ids, stable_since = ids, now
            elif now - stable_since >= _STABLE_SEC:
                return new, False
        else:
            last_ids = None
            if len(existing) == 1 and now - started >= _REUSE_GRACE_SEC:
                return existing, True
        if now >= deadline:
            return (new or existing), not new
        time.sleep(max(0.0, min(_POLL_SEC, deadline - now)))


def open_application_placed(target: str, app_aliases: dict, monitor, rectangle=None, state: str = 'restore',
                            *, deadline: float | None = None) -> dict:
    """Launch an application once, find its window and place it on ``monitor``.

    The destination is validated before launching. A timeout or ambiguity never
    relaunches; the outcome is reported as a ``PartialPlacementError``."""
    from . import windows_mgmt as wm
    wm.validate_placement(monitor, rectangle, state)
    if deadline is None:
        deadline = time.monotonic() + _PLACE_BUDGET_SEC
    app = resolve_application(target, APP_INDEX.applications(), app_aliases)
    known = {w.hwnd for w in wm.list_windows() if window_belongs(app, w)}
    os.startfile(app.target)
    debug_log('Application launch requested; waiting to place its window.', 'windows')
    base = {'launch': 'accepted', 'application': app.name}
    windows, reused = _await_window(app, known, deadline)
    if not windows:
        debug_log('No window appeared for the launched application; placement unverified.', 'windows')
        raise PartialPlacementError('The launch was accepted but no matching window appeared in time.',
                                    {**base, 'placement': 'unverified',
                                     'reason': 'No matching window appeared in time.'})
    if len(windows) > 1:
        debug_log(f'{len(windows)} candidate windows for the launched application; not choosing.', 'windows')
        raise PartialPlacementError('Several windows match; specify one by handle with windowControl place.',
                                    {**base, 'placement': 'ambiguous', 'candidates': _candidates(windows),
                                     'reason': 'Several windows match the application.'})
    window = windows[0]
    try:
        placed = wm.place_window(window.hwnd, monitor, rectangle, state, deadline=deadline)
    except (OSError, ValueError) as exc:
        debug_log(f'Placement after launch failed ({type(exc).__name__}).', 'windows')
        raise PartialPlacementError(str(exc), {**base, 'placement': 'failed', 'hwnd': window.hwnd,
                                               'reason': str(exc)}) from exc
    return {'action': 'opened_and_placed', 'application': app.name, 'hwnd': window.hwnd,
            'process': window.process, 'pid': window.pid, 'monitor': placed['monitor'],
            'rectangle': placed['rectangle'], 'state': placed['state'], 'reused_window': reused}

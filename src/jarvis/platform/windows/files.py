"""Open local paths and known folders using Windows file associations."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import unicodedata
from urllib.parse import urlsplit
import uuid

from ...debug import debug_log
from . import file_search


KNOWN_FOLDERS = {
    'desktop': 'B4BFCC3A-DB2C-424C-B029-7FE99A87C641',
    'documents': 'FDD39AD0-238F-46AF-ADB4-6C85480369C7',
    'downloads': '374DE290-123F-4565-9164-39C4925E467B',
    'pictures': '33E28130-4E1E-4676-835A-98395C3BC3BB',
    'music': '4BD8D571-6D19-48D3-BE97-422220080E43',
    'videos': '18989B1D-99B5-455B-841C-AB7C74E4DDFC',
}


def known_folder(name: str) -> str:
    """Respect Windows folder redirection and localisation."""
    guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(KNOWN_FOLDERS[name.casefold()]).bytes_le)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                         ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    output = ctypes.c_void_p()
    result = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(output))
    if result < 0:
        raise OSError(f'Known folder lookup failed (HRESULT {result:#x}).')
    try:
        return ctypes.wstring_at(output)
    finally:
        ole.CoTaskMemFree(output)


_EXECUTABLE_SUFFIXES = {'.exe', '.com', '.bat', '.cmd', '.ps1', '.ps1xml', '.ps2', '.ps2xml',
                        '.psc1', '.psc2', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.hta',
                        '.msi', '.msp', '.mst', '.lnk', '.url', '.scr', '.cpl', '.pif', '.scf',
                        '.reg', '.inf', '.application', '.appref-ms', '.psm1', '.sct',
                        '.py', '.pyw', '.pyc', '.pyo', '.rb', '.rbw', '.pl', '.sh', '.bash',
                        '.jar', '.gadget', '.appx', '.appxbundle', '.msix', '.msixbundle'}


def is_executable_file(path: Path) -> bool:
    """Whether ``path`` is an existing file of an executable or script type (including PATHEXT)."""
    suffixes = _EXECUTABLE_SUFFIXES | {suffix.casefold() for suffix in os.environ.get('PATHEXT', '').split(';') if suffix}
    return path.suffix.casefold() in suffixes and path.is_file()


def _shell_open(target: str):
    os.startfile(target)


class AmbiguousTargetError(ValueError):
    """Several items match a name; ``data`` lists them so the user can say which one."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


def _looks_like_path(target: str) -> bool:
    """Separators, a drive letter, or a home/environment/relative prefix mean a path, which is
    never searched. A bare name (with or without an extension) is looked up instead."""
    return (any(char in target for char in '\\/') or target[1:2] == ':' or
            target.startswith(('~', '%', '.')))


def _search(target: str, searcher) -> str:
    """Resolve a bare name through the local file index to exactly one path, or say why not."""
    outcome = (searcher or file_search.find)(target)
    if outcome.chosen is not None:
        return outcome.chosen.path
    if outcome.candidates:
        raise AmbiguousTargetError(
            'Several items match; ask which one is meant.',
            {'action': 'candidates', 'query': target,
             'candidates': [{'name': match.name, 'path': match.path, 'kind': match.kind}
                            for match in outcome.candidates]})
    if outcome.programs_excluded:
        raise ValueError('That matches an application, not a file or folder; use appControl to open it.')
    raise FileNotFoundError(f'Nothing found matching: {target}')


def _alias_key(name: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', name).casefold().split())


def resolve_path(target: str, searcher=None, aliases=None) -> tuple:
    """The existing file or folder ``target`` names: ``(path, kind, source)``. Nothing is opened.

    ``aliases`` maps saved names to paths and wins over the file index for a bare name. ``searcher``
    replaces the file index (tests); it takes the name and returns a ``Resolution``."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError('A file or folder is required.')
    target = target.strip()
    parsed = urlsplit(target)
    source = 'path'
    if parsed.scheme.casefold() in ('http', 'https') or target.casefold().startswith('www.'):
        raise ValueError('Web addresses open with openWebsite, not openPath.')
    # Drive letters are paths; other URI schemes are not path-opening operations.
    if parsed.scheme and not (len(parsed.scheme) == 1 and len(target) > 2 and target[1] == ':'):
        raise ValueError('Only local files and folders are supported.')
    if target.casefold() in KNOWN_FOLDERS:
        target = known_folder(target.casefold())
    elif not _looks_like_path(target):
        saved = {_alias_key(name): path for name, path in (aliases or {}).items()}.get(_alias_key(target))
        if saved is not None:
            target, source = saved, 'alias'
        else:
            target, source = _search(target, searcher), 'search'
    path = Path(os.path.expandvars(target)).expanduser()
    if not path.exists():
        raise FileNotFoundError(f'Path not found: {target}')
    path = path.resolve()
    if is_executable_file(path):
        raise ValueError('Use appControl for installed applications; executable files are not documents.')
    return path, 'folder' if path.is_dir() else 'file', source


def _opened(path: Path, kind: str, source: str, action: str = 'open_requested') -> dict:
    result = {'action': action, 'kind': kind, 'target': str(path)}
    return {**result, 'source': source} if source != 'path' else result


def open_path(target: str, searcher=None, aliases=None) -> dict:
    """Open a path or a known folder; a bare name is a saved alias, else searched for in the local index."""
    path, kind, source = resolve_path(target, searcher, aliases)
    _shell_open(str(path))
    debug_log(f'Windows {kind} open requested ({source}).', 'windows')
    return _opened(path, kind, source)


# --- opening and placing -------------------------------------------------------------

# One deadline covers opening, finding the window and placing it, inside the tools' twelve-second limit.
PLACE_BUDGET_SEC = 10.0


def association_executable(path: Path):
    """The executable Windows opens ``path`` with: File Explorer for a folder, else the association's
    executable; ``None`` when the association names none (a packaged app)."""
    if path.is_dir():
        return os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'explorer.exe')
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    # ASSOCF_NONE, ASSOCSTR_EXECUTABLE
    if ctypes.windll.shlwapi.AssocQueryStringW(0, 2, path.suffix or '.', None, buffer, ctypes.byref(size)) != 0:
        return None
    return buffer.value if buffer.value and os.path.isfile(buffer.value) else None


def _await_any_new_window(known: set, deadline: float) -> list:
    """New top-level windows of any process once they stop changing, or whatever exists at ``deadline``."""
    import time
    from . import apps, windows_mgmt as wm
    last, stable_since = None, 0.0
    while True:
        now = time.monotonic()
        new = sorted((w for w in wm.list_windows() if w.hwnd not in known), key=lambda w: w.hwnd)
        ids = tuple(w.hwnd for w in new)
        if ids and ids == last and now - stable_since >= apps._STABLE_SEC:
            return new
        if ids != last:
            last, stable_since = ids, now
        if now >= deadline:
            return new
        time.sleep(max(0.0, min(apps._POLL_SEC, deadline - now)))


def _await_program_window(program, known: set, path: Path, deadline: float):
    """The program's window showing the item: ``(windows, reused)``.

    A new window wins once it stops changing. After the reuse grace an existing window is accepted when
    it is the only one or the only one titled with the item, so a program that shows the item in a window
    it already had (a new tab) is not waited out to the deadline."""
    import time
    from . import apps, windows_mgmt as wm
    started, last, stable_since = time.monotonic(), None, 0.0
    while True:
        now = time.monotonic()
        current = [w for w in wm.list_windows() if apps.window_belongs(program, w)]
        new = sorted((w for w in current if w.hwnd not in known), key=lambda w: w.hwnd)
        ids = tuple(w.hwnd for w in new)
        if ids and ids == last and now - stable_since >= apps._STABLE_SEC:
            return new, False
        if ids != last:
            last, stable_since = ids, now
        existing = [w for w in current if w.hwnd in known]
        if not ids and existing and now - started >= apps._REUSE_GRACE_SEC:
            shown = _titled_with(existing, path)
            if len(shown) == 1:
                return shown, True
        if now >= deadline:
            return (new, False) if new else (existing, True)
        time.sleep(max(0.0, min(apps._POLL_SEC, deadline - now)))


def _titled_with(windows: list, path: Path) -> list:
    """The one candidate whose title shows the item's name (full, else without its extension), or all of
    them when that does not single one out."""
    for name in dict.fromkeys((path.name, path.stem)):
        named = [w for w in windows if name and name.casefold() in w.title.casefold()]
        if len(named) == 1:
            return named
    return windows


def open_path_placed(target: str, monitor, rectangle=None, state: str = 'restore', searcher=None,
                     aliases=None) -> dict:
    """Open the item once and place the window showing it. See ``apps_paths.spec.md`` (Paths).

    The item and destination are checked before anything opens. A file a Chromium browser opens gets a
    new browser window; anything else opens through its association and its program's window is found
    as ``appControl open`` finds one. After an accepted open, any other outcome is a
    ``PartialPlacementError`` and nothing is opened again."""
    import time
    from . import apps, windows_mgmt as wm, workspaces
    path, kind, source = resolve_path(target, searcher, aliases)
    wm.validate_placement(monitor, rectangle, state)
    executable = association_executable(path)
    stem = Path(executable).stem if executable else ''
    deadline = time.monotonic() + PLACE_BUDGET_SEC
    base = {'launch': 'accepted', 'kind': kind, 'application': stem}
    browser = workspaces.browser_for_executable(executable) if kind == 'file' and executable else None
    if browser is not None:
        if not workspaces._launch_lock.acquire(blocking=False):
            raise ValueError('A browser window is already opening. Wait for it to finish.')
        try:
            debug_log(f'Opening a {kind} in a new {browser.name} window to place it.', 'windows')
            try:
                placed = workspaces.open_browser_window(browser, [path.as_uri()], monitor, rectangle, state,
                                                        deadline)
            except workspaces.BrowserWindowError as exc:
                raise apps.PartialPlacementError(str(exc), {**base, **exc.data}) from None
        finally:
            workspaces._launch_lock.release()
        return {**_opened(path, kind, source, 'opened_and_placed'), 'application': stem, **placed,
                'reused_window': False}
    if stem:
        program = apps.Application(stem, str(path), executable)
        known = {w.hwnd for w in wm.list_windows() if apps.window_belongs(program, w)}
        _shell_open(str(path))
        debug_log(f'Windows {kind} opened; waiting to place its window.', 'windows')
        windows, reused = _await_program_window(program, known, path, deadline)
    else:
        known = {w.hwnd for w in wm.list_windows()}
        _shell_open(str(path))
        debug_log(f'Windows {kind} opened by a packaged app; waiting for its new window.', 'windows')
        windows, reused = _await_any_new_window(known, deadline), False
    if len(windows) > 1:
        windows = _titled_with(windows, path)
    if not windows:
        raise apps.PartialPlacementError('The item opened but no window showing it appeared in time.',
                                         {**base, 'placement': 'unverified',
                                          'reason': 'No window showing it appeared in time.'})
    if len(windows) > 1:
        raise apps.PartialPlacementError('Several windows could be showing it; specify one with windowControl place.',
                                         {**base, 'placement': 'ambiguous', 'candidates': apps._candidates(windows),
                                          'reason': 'Several windows could be showing it.'})
    window = windows[0]
    try:
        placed = wm.place_window(window.hwnd, monitor, rectangle, state, deadline=deadline)
    except (OSError, ValueError) as exc:
        raise apps.PartialPlacementError(str(exc), {**base, 'placement': 'failed', 'hwnd': window.hwnd,
                                                    'reason': str(exc)}) from None
    debug_log(f'Windows {kind} opened and placed.', 'windows')
    return {**_opened(path, kind, source, 'opened_and_placed'), 'application': stem or window.process,
            'hwnd': window.hwnd, 'process': window.process, 'monitor': placed['monitor'],
            'rectangle': placed['rectangle'], 'state': placed['state'], 'reused_window': reused}

"""The PDF the user is viewing, and showing one of its pages.

OS layer for ``pdfNavigate``: identifies the viewer window (PDFgear, Google Chrome or Microsoft Edge),
resolves its file without guessing, and shows a page through the viewer's page box or, failing that, by
opening the file in the browser at the page. See ``ui_automation.spec.md`` (pdfNavigate).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence
from urllib.parse import quote, unquote, urlparse

from ...debug import debug_log

_VIEWERS = {'pdfeditor': 'pdfgear', 'pdflauncher': 'pdfgear', 'msedge': 'edge', 'chrome': 'chrome'}
_BROWSERS = ('edge', 'chrome')
# Reading an address bar walks the window's UIA tree, so a search reads only the topmost few.
_MAX_ADDRESS_READS = 4
_PDF_NAME_RE = re.compile(r'[^\\/:*?"<>|]+?\.pdf\b', re.IGNORECASE)
_FOLDER_DEPTH = 2
_SCAN_LIMIT = 5000


class AmbiguousDocumentError(ValueError):
    """Several files could be the open document; ``candidates`` holds their paths."""

    def __init__(self, candidates: Sequence[str]):
        super().__init__('Several PDF files match the open document; say which one.')
        self.candidates = list(candidates)


@dataclass(frozen=True)
class Viewer:
    kind: str  # 'pdfgear', 'edge', 'chrome'
    hwnd: int
    process: str
    title: str


def viewer_kind(process: str) -> Optional[str]:
    return _VIEWERS.get(str(process or '').casefold())


def title_file_names(title: str) -> List[str]:
    """PDF file names a viewer's title shows; a bare document name gets the ``.pdf`` extension."""
    title = str(title or '').strip()
    if not title:
        return []
    names = []
    for match in _PDF_NAME_RE.findall(title):
        # A title joins the file name and the application with " - ", and a file name may contain one.
        parts = match.strip().split(' - ')
        names += [' - '.join(parts[start:]).strip() for start in range(len(parts))]
    if names:
        return list(dict.fromkeys(names))
    first = title.split(' - ')[0].strip()
    return [first + '.pdf'] if first else []


def _address_pdf(hwnd: int) -> Optional[Path]:
    """The local PDF a browser window's address bar shows, else ``None``."""
    from .ui_automation import address_bar_values
    for value in address_bar_values(hwnd):
        path = path_from_address(value)
        if path and Path(path).is_file():
            return Path(path)
    return None


def _shows_pdf(kind: str, hwnd: int, title: str) -> bool:
    """Whether a viewer window shows a PDF. Edge titles carry the file name; Chrome titles carry the
    document's own title, so Chrome's address bar is read instead."""
    if kind == 'edge':
        return '.pdf' in title.casefold()
    if kind == 'chrome':
        return _address_pdf(hwnd) is not None
    return True


def find_viewer(window: str = '') -> Optional[Viewer]:
    """The PDF viewer the user is looking at: the addressed window, else the topmost viewer window.

    A browser counts only while it shows a PDF. The window the user is working in is skipped when it
    does not (a named window is refused), and only the topmost few Chrome windows have their address
    bar read."""
    from .ui_automation import resolve_hwnd, window_info
    try:
        info = window_info(resolve_hwnd(window))
    except ValueError:
        if window:
            raise
        info = None
    kind = viewer_kind(info['process']) if info else None
    if kind and _shows_pdf(kind, info['hwnd'], info['title']):
        return Viewer(kind, info['hwnd'], info['process'], info['title'])
    if window:
        raise ValueError('That window is not showing a PDF.')
    from .windows_mgmt import list_windows
    reads = 0
    for candidate in list_windows():
        kind = viewer_kind(candidate.process)
        if not kind or (info and candidate.hwnd == info['hwnd']):
            continue
        if kind == 'chrome':
            if reads >= _MAX_ADDRESS_READS:
                continue
            reads += 1
        if _shows_pdf(kind, candidate.hwnd, candidate.title):
            return Viewer(kind, candidate.hwnd, candidate.process, candidate.title)
    return None


def foreground_viewer() -> Optional[Viewer]:
    """The PDF viewer when it is the window the user is working in (Jarvis's own windows skipped), else None.

    A browser counts only while it shows a PDF, so a web page in Edge or Chrome is not a viewer."""
    from .ui_automation import resolve_hwnd, window_info
    try:
        info = window_info(resolve_hwnd(''))
    except ValueError:
        return None
    kind = viewer_kind(info['process'])
    if kind is None or not _shows_pdf(kind, info['hwnd'], info['title']):
        return None
    return Viewer(kind, info['hwnd'], info['process'], info['title'])


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _recent_dir() -> Path:
    return Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Recent'


def _known_folders() -> List[Path]:
    from .files import known_folder
    folders = []
    for name in ('Desktop', 'Documents', 'Downloads'):
        try:
            folders.append(Path(known_folder(name)))
        except Exception:  # noqa: BLE001 - a missing known folder is skipped
            continue
    return folders


def _shortcut_target(link: Path) -> Optional[str]:
    """The target path of a shell link, read on a worker with its own COM apartment."""
    box: dict = {}

    def read():
        import comtypes
        import comtypes.client
        from comtypes.persist import IPersistFile
        from comtypes.shelllink import ShellLink, IShellLinkW, SLGP_UNCPRIORITY
        from .ui_automation import com_apartment
        with com_apartment():
            shortcut = comtypes.client.CreateObject(ShellLink, interface=IShellLinkW)
            shortcut.QueryInterface(IPersistFile).Load(str(link), 0)
            path = shortcut.GetPath(SLGP_UNCPRIORITY)
            box['path'] = path[0] if isinstance(path, tuple) else path
            del shortcut

    worker = threading.Thread(target=read, name='pdf-shortcut', daemon=True)
    worker.start()
    worker.join(2.0)
    return box.get('path') or None


def _scan(folder: Path, wanted: set, depth: int, budget: List[int]) -> Iterable[Path]:
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        budget[0] -= 1
        if budget[0] <= 0:
            return
        try:
            if entry.is_file() and entry.name.casefold() in wanted:
                yield Path(entry.path)
            elif depth > 0 and entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                yield from _scan(Path(entry.path), wanted, depth - 1, budget)
        except OSError:
            continue


def find_files(names: Sequence[str], recent_dir: Optional[Path] = None,
               folders: Optional[Sequence[Path]] = None) -> List[str]:
    """Existing files with one of ``names``: Recent items' shortcut targets, then the known folders."""
    wanted = {name.casefold() for name in names if name}
    if not wanted:
        return []
    recent = _recent_dir() if recent_dir is None else Path(recent_dir)
    found: List[Path] = []
    for name in names:
        link = recent / f'{name}.lnk'
        if link.exists():
            target = _shortcut_target(link)
            if target and Path(target).is_file() and Path(target).name.casefold() in wanted:
                found.append(Path(target))
    budget = [_SCAN_LIMIT]
    for folder in (_known_folders() if folders is None else folders):
        found.extend(_scan(Path(folder), wanted, _FOLDER_DEPTH, budget))
    unique = {}
    for path in found:
        unique.setdefault(os.path.normcase(str(path.resolve())), str(path.resolve()))
    return list(unique.values())


def path_from_address(value: str) -> Optional[str]:
    """A local PDF path from a browser address (``file:///...`` or a bare path), else ``None``."""
    text = str(value or '').strip()
    if text.casefold().startswith('file:'):
        parsed = urlparse(text)
        path = unquote(parsed.path)
        if re.match(r'^/[A-Za-z]:', path):
            path = path[1:]
        if parsed.netloc:
            path = f'//{parsed.netloc}{path}'
        text = path.replace('/', os.sep)
    elif not re.match(r'^[A-Za-z]:[\\/]', text):
        return None
    text = text.split('#', 1)[0]
    return text if text.casefold().endswith('.pdf') else None


def document_for(viewer: Viewer, *, recent_dir: Optional[Path] = None,
                 folders: Optional[Sequence[Path]] = None) -> Path:
    """The file the viewer shows. Several candidates raise ``AmbiguousDocumentError``; none, ``ValueError``."""
    if viewer.kind in _BROWSERS:
        path = _address_pdf(viewer.hwnd)
        if path is None:
            raise ValueError('This browser tab is not showing a PDF file from this PC.')
        return path
    names = title_file_names(viewer.title)
    found = find_files(names, recent_dir=recent_dir, folders=folders)
    debug_log(f'PDF file resolution: {len(found)} candidate(s).', 'windows')
    if len(found) == 1:
        return Path(found[0])
    if found:
        raise AmbiguousDocumentError(found)
    raise ValueError('The open PDF was not found in Recent items, Desktop, Documents or Downloads; '
                     'give its path.')


def shows_file(viewer: Optional[Viewer], path: Path) -> bool:
    if viewer is None:
        return False
    if viewer.kind in _BROWSERS:
        try:
            return document_for(viewer) == Path(path)
        except ValueError:
            return False
    return Path(path).name.casefold() in {name.casefold() for name in title_file_names(viewer.title)}


# ---------------------------------------------------------------------------
# Showing a page
# ---------------------------------------------------------------------------

def set_viewer_page(hwnd: int, page: int, page_count: int) -> dict:
    from .ui_automation import set_page_box
    return set_page_box(hwnd, page, page_count)


def edge_page_url(path: Path, page: int) -> str:
    absolute = Path(path).resolve().as_posix()
    return 'file:///' + quote(absolute.lstrip('/'), safe='/:') + f'#page={int(page)}'


def edge_executable() -> str:
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe') as key:
                value, _ = winreg.QueryValueEx(key, '')
        except OSError:
            continue
        if value and Path(value).is_file():
            return value
    raise OSError('Microsoft Edge was not found.')


def chrome_executable() -> str:
    from .workspaces import find_browser
    try:
        return find_browser('chrome').executable
    except ValueError as exc:
        raise OSError(str(exc)) from None


def _launch(argv: List[str]) -> None:
    subprocess.Popen(argv, close_fds=True)


def open_in_browser(path: Path, page: int, browser: str = 'edge',
                    launcher: Optional[Callable[[List[str]], None]] = None) -> dict:
    """Open the file at ``page`` as a new tab in Chrome (``browser='chrome'``) or Edge."""
    executable = chrome_executable() if browser == 'chrome' else edge_executable()
    (launcher or _launch)([executable, edge_page_url(path, page)])
    debug_log(f'PDF opened in {browser} at a page.', 'windows')
    return {'page': int(page), 'method': f'opened_in_{browser}'}


def show_page(viewer: Optional[Viewer], path: Path, page: int, page_count: int,
              launcher: Optional[Callable[[List[str]], None]] = None) -> dict:
    """Show ``page``: through the viewer's page box (PDFgear, Chrome), else by opening the file at the
    page in Chrome when Chrome was the viewer, otherwise in Edge."""
    reason = ''
    if viewer is not None and viewer.kind != 'edge':
        try:
            return {**set_viewer_page(viewer.hwnd, page, page_count), 'viewer': viewer.kind}
        except LookupError as exc:
            reason = str(exc)
            debug_log('PDF viewer page box unusable; opening the file at the page.', 'windows')
    browser = 'chrome' if viewer is not None and viewer.kind == 'chrome' else 'edge'
    result = open_in_browser(path, page, browser, launcher)
    if reason:
        result['reason'] = reason
    return result

"""Find files and folders by name for "open my report" style requests.

Two local backends, tried in order: the voidtools **Everything** SDK when Everything is running
and its SDK library is installed, otherwise the **Windows Search index** (``SystemIndex``, over
OLE DB). Both are read-only and local; nothing is sent anywhere.

Resolution never guesses. One exact name match, or a single remaining result, is chosen.
Anything else is returned as candidates (exact names first, then recency) for the caller to
ask about. Programs and scripts are never results (applications go through ``appControl``), and
caches, package folders, the recycle bin and system folders are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _datetime
import os
from pathlib import PureWindowsPath
import re
from typing import Callable, Protocol, Sequence

from ...debug import debug_log
from ..file_find import FindCriteria, Found, ScanBackend, SearchUnavailable, is_noise
from ._bounded import run_bounded

MAX_CANDIDATES = 5
_FETCH_LIMIT = 100
SEARCH_TIMEOUT_SEC = 8.0

_PROGRAM_SUFFIXES = {'.exe', '.com', '.bat', '.cmd', '.ps1', '.psm1', '.vbs', '.vbe', '.js', '.jse', '.wsf',
                     '.wsh', '.hta', '.msi', '.msp', '.lnk', '.url', '.scr', '.cpl', '.reg', '.inf', '.py',
                     '.pyw', '.appx', '.msix', '.dll', '.sys'}


@dataclass(frozen=True)
class FileMatch:
    path: str
    kind: str = 'file'
    modified: float = 0.0

    @property
    def name(self) -> str:
        return PureWindowsPath(self.path).name


@dataclass
class Resolution:
    chosen: FileMatch | None = None
    candidates: list[FileMatch] = field(default_factory=list)
    programs_excluded: bool = False


class SearchBackend(Protocol):
    name: str

    def search(self, tokens: list[str], limit: int) -> list[FileMatch]: ...


def query_tokens(text: str) -> list[str]:
    """Letter and digit runs, case-folded. Underscores and punctuation separate words."""
    return re.findall(r'[^\W_]+', str(text or '').casefold(), re.UNICODE)


def _normalised_name(value: str) -> str:
    return ' '.join(query_tokens(value))


def _is_program(match: FileMatch) -> bool:
    return match.kind == 'file' and PureWindowsPath(match.path).suffix.casefold() in _PROGRAM_SUFFIXES


def _is_exact(query: str, match: FileMatch) -> bool:
    path = PureWindowsPath(match.path)
    wanted = _normalised_name(query)
    return wanted in (_normalised_name(path.name), _normalised_name(path.stem))


def _whole_words(tokens: list[str], match: FileMatch) -> bool:
    return set(tokens) <= set(query_tokens(match.name))


def resolve(query: str, backends: SearchBackend | Sequence[SearchBackend]) -> Resolution:
    """Search and decide: choose one item, or return ordered candidates, or nothing."""
    tokens = query_tokens(query)
    if not tokens or sum(len(token) for token in tokens) < 2:
        raise ValueError('A file or folder name is required.')
    sources = list(backends) if isinstance(backends, (list, tuple)) else [backends]
    results = None
    for backend in sources:
        try:
            results = run_bounded(lambda backend=backend: backend.search(tokens, _FETCH_LIMIT), SEARCH_TIMEOUT_SEC)
            debug_log(f'File search via {backend.name} returned {len(results)} raw result(s).', 'windows')
            break
        except (SearchUnavailable, TimeoutError, OSError) as exc:
            debug_log(f'File search backend {backend.name} unavailable ({type(exc).__name__}).', 'windows')
    if results is None:
        raise SearchUnavailable('File search is not available right now.')
    unique = {match.path.casefold(): match for match in results if match.path and not is_noise(match.path)}
    kept = [match for match in unique.values() if not _is_program(match)]
    outcome = Resolution(programs_excluded=len(kept) < len(unique) and not kept)
    exact = [match for match in kept if _is_exact(query, match)]
    if len(exact) == 1:
        outcome.chosen = exact[0]
    elif len(kept) == 1 and not exact:
        outcome.chosen = kept[0]
    else:
        ordered = sorted(kept, key=lambda match: (not _is_exact(query, match), not _whole_words(tokens, match),
                                                   -match.modified, len(match.name)))
        outcome.candidates = ordered[:MAX_CANDIDATES]
    return outcome


# --- Windows Search ------------------------------------------------------------------

def windows_search_sql(tokens: list[str], limit: int) -> str:
    """Tokens are letter/digit runs, so quoting only needs the apostrophe rule as defence in depth."""
    conditions = ' AND '.join("System.ItemName LIKE '%" + token.replace("'", "''") + "%'" for token in tokens)
    return (f'SELECT TOP {int(limit)} System.ItemPathDisplay, System.DateModified, System.IsFolder '
            f'FROM SystemIndex WHERE {conditions} ORDER BY System.DateModified DESC')


_FIND_ORDER = {'newest': 'System.DateModified DESC', 'oldest': 'System.DateModified ASC',
               'largest': 'System.Size DESC'}


def _utc_literal(timestamp: float) -> str:
    """The index compares dates in UTC."""
    moment = _datetime.datetime.fromtimestamp(timestamp, _datetime.timezone.utc)
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def windows_find_sql(criteria: FindCriteria, limit: int) -> str:
    """Tokens are letter/digit runs and extensions letter/digit suffixes; quoting is defence in depth.

    The date range applies to the later of created and modified: at least one is on or after ``after``
    and both are before ``before``. Results are filtered again locally."""
    def quoted(text: str) -> str:
        return text.replace("'", "''")

    conditions = [f"System.ItemName LIKE '%{quoted(token)}%'" for token in criteria.tokens]
    if criteria.extensions:
        conditions.append('(' + ' OR '.join(f"System.FileExtension = '{quoted(extension)}'"
                                            for extension in sorted(criteria.extensions)) + ')')
    if criteria.kind:
        conditions.append(f"System.IsFolder = {'TRUE' if criteria.kind == 'folder' else 'FALSE'}")
    if criteria.after is not None:
        after = _utc_literal(criteria.after)
        conditions.append(f"(System.DateModified >= '{after}' OR System.DateCreated >= '{after}')")
    if criteria.before is not None:
        before = _utc_literal(criteria.before)
        conditions.append(f"System.DateModified < '{before}' AND System.DateCreated < '{before}'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ''
    return (f'SELECT TOP {int(limit)} System.ItemPathDisplay, System.DateModified, System.IsFolder, '
            f'System.DateCreated, System.Size FROM SystemIndex{where} ORDER BY {_FIND_ORDER[criteria.sort]}')


def _timestamp(value) -> float:
    if isinstance(value, _datetime.datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    return 0.0


def _execute_windows_search(sql: str) -> list[tuple]:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise SearchUnavailable('The Windows Search index needs pywin32.') from exc
    pythoncom.CoInitialize()
    connection = recordset = None
    try:
        connection = win32com.client.Dispatch('ADODB.Connection')
        connection.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        recordset = win32com.client.Dispatch('ADODB.Recordset')
        recordset.Open(sql, connection)
        rows = []
        while not recordset.EOF:
            rows.append(tuple(recordset.Fields.Item(index).Value for index in range(recordset.Fields.Count)))
            recordset.MoveNext()
        return rows
    except Exception as exc:  # noqa: BLE001 - COM raises many types; the index may be disabled
        raise SearchUnavailable('The Windows Search index could not be queried.') from exc
    finally:
        for resource in (recordset, connection):
            try:
                if resource is not None:
                    resource.Close()
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()


class WindowsSearchBackend:
    name = 'windows-search'

    def __init__(self, execute: Callable[[str], list[tuple]] | None = None):
        self._execute = execute or (lambda sql: _execute_windows_search(sql))

    def search(self, tokens: list[str], limit: int) -> list[FileMatch]:
        rows = self._execute(windows_search_sql(tokens, limit))
        return [FileMatch(str(path), 'folder' if is_folder else 'file', _timestamp(modified))
                for path, modified, is_folder, *_ in rows if path]

    def find(self, criteria: FindCriteria, limit: int) -> tuple[list[Found], bool]:
        rows = self._execute(windows_find_sql(criteria, limit))
        items = [Found(str(path), 'folder' if is_folder else 'file', _timestamp(modified), _timestamp(created),
                       None if is_folder or size is None else int(size))
                 for path, modified, is_folder, created, size in rows if path]
        return items, len(rows) < limit


# --- Everything ------------------------------------------------------------------------

class EverythingBackend:
    """The Everything SDK, through an adapter exposing the SDK calls plus result accessors."""
    name = 'everything'
    _SORT_SIZE_DESCENDING = 6
    _SORT_DATE_MODIFIED_ASCENDING = 13
    _SORT_DATE_MODIFIED_DESCENDING = 14
    _REQUEST_FULL_PATH_AND_DATE = 0x00000004 | 0x00000040  # full path name | date modified
    _REQUEST_SIZE_AND_CREATED = 0x00000010 | 0x00000020  # size | date created
    _FIND_SORT = {'newest': _SORT_DATE_MODIFIED_DESCENDING, 'oldest': _SORT_DATE_MODIFIED_ASCENDING,
                  'largest': _SORT_SIZE_DESCENDING}

    def __init__(self, sdk):
        self._sdk = sdk

    def _query(self, text: str, limit: int, sort: int, flags: int) -> int:
        sdk = self._sdk
        sdk.Everything_SetSearchW(text)
        sdk.Everything_SetMax(limit)
        sdk.Everything_SetSort(sort)
        sdk.Everything_SetRequestFlags(flags)
        if not sdk.Everything_QueryW(True):
            raise SearchUnavailable('Everything did not answer the query.')
        return sdk.Everything_GetNumResults()

    def search(self, tokens: list[str], limit: int) -> list[FileMatch]:
        sdk = self._sdk
        count = self._query(' '.join(f'"{token}"' for token in tokens), limit, self._SORT_DATE_MODIFIED_DESCENDING,
                            self._REQUEST_FULL_PATH_AND_DATE)
        return [FileMatch(sdk.full_path(index), 'folder' if sdk.is_folder(index) else 'file', sdk.modified(index))
                for index in range(count)]

    def find(self, criteria: FindCriteria, limit: int) -> tuple[list[Found], bool]:
        """Tokens, extensions, kind and scope go to Everything; the date range is applied locally."""
        terms = [f'"{token}"' for token in criteria.tokens]
        if criteria.extensions:
            terms.append('ext:' + ';'.join(sorted(extension.lstrip('.') for extension in criteria.extensions)))
        if criteria.kind:
            terms.append(f'{criteria.kind}:')
        if criteria.scope:
            terms.append('"' + criteria.scope.rstrip('\\') + '\\"')
        sdk = self._sdk
        count = self._query(' '.join(terms), limit, self._FIND_SORT[criteria.sort],
                            self._REQUEST_FULL_PATH_AND_DATE | self._REQUEST_SIZE_AND_CREATED)
        items = []
        for index in range(count):
            folder = sdk.is_folder(index)
            items.append(Found(sdk.full_path(index), 'folder' if folder else 'file', sdk.modified(index),
                               sdk.created(index), None if folder else sdk.size(index)))
        return items, count < limit


def _everything_running() -> bool:
    import ctypes
    return bool(ctypes.WinDLL('user32').FindWindowW('EVERYTHING', None))


def _everything_sdk():
    """The Everything SDK library from a standard install folder, adapted, or ``None``."""
    import ctypes
    bits = 'Everything64.dll' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'Everything32.dll'
    folders = [os.environ.get(name) for name in ('ProgramFiles', 'ProgramFiles(x86)')]
    folders = [os.path.join(folder, 'Everything') for folder in folders if folder]
    local = os.environ.get('LOCALAPPDATA')
    if local:
        folders.append(os.path.join(local, 'Programs', 'Everything'))
    for folder in folders:
        path = os.path.join(folder, bits)
        if os.path.isfile(path):
            try:
                return _EverythingLibrary(ctypes.WinDLL(path))
            except OSError:
                continue
    return None


class _EverythingLibrary:
    def __init__(self, dll):
        import ctypes
        from ctypes import wintypes
        self._dll, self._ctypes, self._wintypes = dll, ctypes, wintypes
        dll.Everything_SetSearchW.argtypes = [wintypes.LPCWSTR]
        dll.Everything_SetMax.argtypes = [wintypes.DWORD]
        dll.Everything_SetSort.argtypes = [wintypes.DWORD]
        dll.Everything_SetRequestFlags.argtypes = [wintypes.DWORD]
        dll.Everything_QueryW.argtypes = [wintypes.BOOL]
        dll.Everything_QueryW.restype = wintypes.BOOL
        dll.Everything_GetNumResults.restype = wintypes.DWORD
        dll.Everything_GetResultFullPathNameW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD]
        dll.Everything_IsFolderResult.argtypes = [wintypes.DWORD]
        dll.Everything_IsFolderResult.restype = wintypes.BOOL
        dll.Everything_GetResultDateModified.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.FILETIME)]
        dll.Everything_GetResultDateModified.restype = wintypes.BOOL
        dll.Everything_GetResultDateCreated.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.FILETIME)]
        dll.Everything_GetResultDateCreated.restype = wintypes.BOOL
        dll.Everything_GetResultSize.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_longlong)]
        dll.Everything_GetResultSize.restype = wintypes.BOOL

    def __getattr__(self, name):
        return getattr(self._dll, name)

    def full_path(self, index: int) -> str:
        buffer = self._ctypes.create_unicode_buffer(1024)
        self._dll.Everything_GetResultFullPathNameW(index, buffer, len(buffer))
        return buffer.value

    def is_folder(self, index: int) -> bool:
        return bool(self._dll.Everything_IsFolderResult(index))

    def _date(self, getter, index: int) -> float:
        stamp = self._wintypes.FILETIME()
        if not getter(index, self._ctypes.byref(stamp)):
            return 0.0
        ticks = (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime
        return max(0.0, ticks / 10_000_000 - 11644473600)

    def modified(self, index: int) -> float:
        return self._date(self._dll.Everything_GetResultDateModified, index)

    def created(self, index: int) -> float:
        return self._date(self._dll.Everything_GetResultDateCreated, index)

    def size(self, index: int):
        value = self._ctypes.c_longlong()
        return int(value.value) if self._dll.Everything_GetResultSize(index, self._ctypes.byref(value)) else None


def _everything(everything_running: Callable[[], bool] | None = None,
                everything_sdk: Callable[[], object | None] | None = None) -> list:
    """The Everything backend when Everything is running and its SDK library is present."""
    try:
        if (everything_running or _everything_running)():
            sdk = (everything_sdk or _everything_sdk)()
            if sdk is not None:
                return [EverythingBackend(sdk)]
    except Exception as exc:  # noqa: BLE001 - Everything is optional
        debug_log(f'Everything is not usable ({type(exc).__name__}).', 'windows')
    return []


def choose_backends(everything_running: Callable[[], bool] | None = None,
                    everything_sdk: Callable[[], object | None] | None = None) -> list[SearchBackend]:
    """Everything first when it is running and its SDK library is present, then Windows Search."""
    return [*_everything(everything_running, everything_sdk), WindowsSearchBackend()]


def index_backends(scoped: bool, everything_running: Callable[[], bool] | None = None,
                   everything_sdk: Callable[[], object | None] | None = None) -> list:
    """The indexes a find asks: Everything, and Windows Search when searching everywhere. A named folder
    is scanned instead of asking Windows Search, which can lag behind a fresh download or skip the folder."""
    return [*_everything(everything_running, everything_sdk), *([] if scoped else [WindowsSearchBackend()])]


def find_backends(scoped: bool, **index_options) -> list:
    """Backends for a find, in order: the indexes, then a scan of the named folder."""
    return [*index_backends(scoped, **index_options), *([ScanBackend()] if scoped else [])]


def find(query: str) -> Resolution:
    """Resolve a name against the best available local index."""
    return resolve(query, choose_backends())

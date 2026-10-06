"""Find files and folders by name words, type and date, for "the PDF I downloaded yesterday".

Portable: the criteria, date ranges, ranking and a bounded folder scan. Index backends (Everything,
Windows Search) live in ``platform/windows/file_search.py`` and answer the same ``find`` call. Every
backend is read-only and local; nothing is sent anywhere.

An item's date is the later of its creation time at its location and its last modification, so a file
that just arrived (a download, a copy) and a file just edited both count as recent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from pathlib import PureWindowsPath
import re
import time
from typing import Optional, Protocol, Sequence

from ..debug import debug_log
from .windows._bounded import run_bounded

SEARCH_TIMEOUT_SEC = 8.0
# The scan gives up a little before the search limit, so it reports what it has rather than timing out.
SCAN_DEADLINE_SEC = 6.0
SCAN_MAX_ENTRIES = 50_000
FETCH_LIMIT = 1000
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

NOISE_COMPONENTS = frozenset({'appdata', 'node_modules', '.git', '$recycle.bin', 'site-packages', '.mamba_env',
                              '__pycache__', '.venv', 'venv'})

CATEGORIES = {
    'document': frozenset({'.pdf', '.doc', '.docx', '.odt', '.rtf', '.txt', '.md', '.pages', '.tex', '.epub'}),
    'spreadsheet': frozenset({'.xls', '.xlsx', '.xlsm', '.ods', '.csv', '.numbers'}),
    'presentation': frozenset({'.ppt', '.pptx', '.odp', '.key'}),
    'image': frozenset({'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.heif', '.tif', '.tiff', '.svg',
                        '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng'}),
    'video': frozenset({'.mp4', '.mov', '.mkv', '.avi', '.wmv', '.webm', '.m4v', '.flv', '.mpg', '.mpeg'}),
    'audio': frozenset({'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.wma', '.opus', '.aiff', '.alac'}),
    'archive': frozenset({'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.iso'}),
    'program': frozenset({'.exe', '.msi', '.msix', '.msixbundle', '.appx', '.appxbundle'}),
}
WHEN = ('today', 'yesterday', 'this_week', 'last_week', 'this_month', 'last_month', 'this_year', 'last_7_days',
        'last_30_days')
SORTS = ('newest', 'oldest', 'largest')

_EXTENSION = re.compile(r'\.?([^\W_]{1,15})', re.UNICODE)


class SearchUnavailable(OSError):
    """No search backend could answer."""


@dataclass(frozen=True)
class Found:
    path: str
    kind: str = 'file'
    modified: float = 0.0
    created: float = 0.0
    size: Optional[int] = None

    @property
    def name(self) -> str:
        return PureWindowsPath(self.path).name

    @property
    def date(self) -> float:
        return max(self.modified, self.created)


@dataclass(frozen=True)
class FindCriteria:
    """``tokens`` must all appear in the name; ``extensions`` are lower-case with the dot; ``kind`` is
    ``file``, ``folder`` or ``None`` for both; ``after``/``before`` are timestamps (``before`` exclusive)."""
    tokens: tuple = ()
    extensions: frozenset = frozenset()
    kind: Optional[str] = None
    after: Optional[float] = None
    before: Optional[float] = None
    scope: Optional[str] = None
    sort: str = 'newest'
    limit: int = DEFAULT_LIMIT

    @property
    def narrowed(self) -> bool:
        return bool(self.tokens or self.extensions or self.kind or self.after is not None or self.before is not None)


@dataclass
class FindOutcome:
    items: list = field(default_factory=list)
    count: int = 0
    complete: bool = True
    backend: str = ''


class FindBackend(Protocol):
    name: str

    def find(self, criteria: FindCriteria, limit: int) -> tuple[list[Found], bool]: ...


def name_tokens(text: str) -> tuple:
    """Letter and digit runs, case-folded, as ``openPath``'s name lookup splits them."""
    return tuple(re.findall(r'[^\W_]+', str(text or '').casefold(), re.UNICODE))


def is_noise(path: str) -> bool:
    parts = {part.casefold() for part in PureWindowsPath(path).parts}
    if parts & NOISE_COMPONENTS:
        return True
    folded = path.casefold()
    for variable in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData'):
        root = os.environ.get(variable)
        if root and (folded == root.casefold() or folded.startswith(root.casefold().rstrip('\\') + '\\')):
            return True
    return False


# --- criteria ------------------------------------------------------------------------------

def parse_type(text: Optional[str]) -> tuple[frozenset, Optional[str]]:
    """A category or comma-separated extensions -> (extensions, kind)."""
    if text is None:
        return frozenset(), None
    folded = str(text).strip().casefold()
    if folded == 'folder':
        return frozenset(), 'folder'
    if folded in CATEGORIES:
        return CATEGORIES[folded], 'file'
    extensions = set()
    for part in folded.split(','):
        match = _EXTENSION.fullmatch(part.strip())
        if not match:
            raise ValueError(f'Unsupported type: {text}. Use a category ({", ".join([*CATEGORIES, "folder"])}) '
                             f'or extensions such as "pdf" or "docx, xlsx".')
        extensions.add('.' + match.group(1))
    return frozenset(extensions), 'file'


def _parse_local(text: str, field_name: str) -> datetime:
    value = str(text).strip()
    for pattern in ('%Y-%m-%d', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    raise ValueError(f'{field_name} must be a local date YYYY-MM-DD or date and time YYYY-MM-DDTHH:MM.')


def _month_start(year: int, month: int) -> datetime:
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    return datetime(year, month, 1)


def date_range(when: Optional[str] = None, after: Optional[str] = None, before: Optional[str] = None,
               now: Optional[datetime] = None) -> tuple[Optional[float], Optional[float]]:
    """Timestamps for a named range or explicit local dates. ``now`` is a naive local datetime (tests)."""
    if when is not None and (after is not None or before is not None):
        raise ValueError('Use when, or after and before, not both.')
    now = now or datetime.now()
    day = datetime(now.year, now.month, now.day)
    if when is not None:
        ranges = {
            'today': (day, day + timedelta(days=1)),
            'yesterday': (day - timedelta(days=1), day),
            'this_week': (day - timedelta(days=day.weekday()), day + timedelta(days=7 - day.weekday())),
            'last_week': (day - timedelta(days=day.weekday() + 7), day - timedelta(days=day.weekday())),
            'this_month': (_month_start(now.year, now.month), _month_start(now.year, now.month + 1)),
            'last_month': (_month_start(now.year, now.month - 1), _month_start(now.year, now.month)),
            'this_year': (datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1)),
            'last_7_days': (now - timedelta(days=7), None),
            'last_30_days': (now - timedelta(days=30), None),
        }
        key = str(when).strip().casefold()
        if key not in ranges:
            raise ValueError(f'Unsupported when value: {when}. Use one of: {", ".join(WHEN)}.')
        start, end = ranges[key]
        # Naive local datetimes: timestamp() applies the local zone's rules for that date (DST included).
        return start.timestamp(), end.timestamp() if end is not None else None
    start = _parse_local(after, 'after').timestamp() if after is not None else None
    end = _parse_local(before, 'before').timestamp() if before is not None else None
    if start is not None and end is not None and end <= start:
        raise ValueError('The date range ends before it starts.')
    return start, end


# --- matching and ranking ------------------------------------------------------------------

def _inside(path: str, scope: str) -> bool:
    folded, root = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(scope))
    return folded.startswith(root.rstrip(os.sep) + os.sep)


def matches(item: Found, criteria: FindCriteria) -> bool:
    name = item.name.casefold()
    if any(token not in name for token in criteria.tokens):
        return False
    if criteria.kind and item.kind != criteria.kind:
        return False
    if criteria.extensions and PureWindowsPath(item.path).suffix.casefold() not in criteria.extensions:
        return False
    if criteria.after is not None and item.date < criteria.after:
        return False
    if criteria.before is not None and item.date >= criteria.before:
        return False
    return not (criteria.scope and not _inside(item.path, criteria.scope))


def _noisy(path: str, scope: Optional[str]) -> bool:
    """Noise below a named folder; a folder the user names is searched even when it is itself in one."""
    if not scope:
        return is_noise(path)
    if not _inside(path, scope):
        return False
    relative = os.path.relpath(os.path.normpath(path), os.path.normpath(scope))
    return bool({part.casefold() for part in PureWindowsPath(relative).parts} & NOISE_COMPONENTS)


def _order(items: list[Found], sort: str) -> list[Found]:
    by_name = sorted(items, key=lambda item: item.name.casefold())
    if sort == 'largest':
        return sorted(by_name, key=lambda item: -(item.size or 0))
    if sort == 'oldest':
        return sorted(by_name, key=lambda item: item.date)
    return sorted(by_name, key=lambda item: -item.date)


def find(criteria: FindCriteria, backends: FindBackend | Sequence[FindBackend]) -> FindOutcome:
    """Ask the first backend that answers, then filter, rank and cap locally."""
    if not criteria.narrowed and not criteria.scope:
        raise ValueError('find needs a name, type, date or folder to search.')
    if criteria.sort not in SORTS:
        raise ValueError(f'Unsupported sort: {criteria.sort}. Use one of: {", ".join(SORTS)}.')
    sources = list(backends) if isinstance(backends, (list, tuple)) else [backends]
    for backend in sources:
        try:
            items, complete = run_bounded(lambda backend=backend: backend.find(criteria, FETCH_LIMIT),
                                          SEARCH_TIMEOUT_SEC)
        except (SearchUnavailable, TimeoutError, OSError) as exc:
            debug_log(f'File find backend {backend.name} unavailable ({type(exc).__name__}).', 'tools')
            continue
        unique = {os.path.normcase(item.path): item for item in items if item.path}
        kept = [item for item in unique.values() if not _noisy(item.path, criteria.scope) and matches(item, criteria)]
        ordered = _order(kept, criteria.sort)
        debug_log(f'File find via {backend.name}: {len(items)} raw, {len(kept)} kept, complete={complete}.', 'tools')
        return FindOutcome(items=ordered[:criteria.limit], count=len(kept), complete=complete, backend=backend.name)
    raise SearchUnavailable('File search is not available right now.')


# --- the folder scan -----------------------------------------------------------------------

class ScanBackend:
    """Walk the scope folder directly: always current, bounded by an entry budget and a deadline."""
    name = 'scan'

    def __init__(self, max_entries: int = SCAN_MAX_ENTRIES, deadline_sec: float = SCAN_DEADLINE_SEC):
        self.max_entries, self.deadline_sec = max_entries, deadline_sec

    def find(self, criteria: FindCriteria, limit: int) -> tuple[list[Found], bool]:
        if not criteria.scope or not os.path.isdir(criteria.scope):
            raise SearchUnavailable('The folder to search does not exist.')
        deadline = time.monotonic() + self.deadline_sec
        results: list[Found] = []
        pending, seen = [criteria.scope], 0
        while pending:
            folder = pending.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                seen += 1
                if seen > self.max_entries or time.monotonic() > deadline:
                    return results, False
                try:
                    is_link = entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction())
                    is_folder = entry.is_dir(follow_symlinks=False)
                    if is_folder and not is_link:
                        if entry.name.casefold() in NOISE_COMPONENTS:
                            continue
                        pending.append(entry.path)
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                item = Found(entry.path, 'folder' if is_folder else 'file', stat.st_mtime,
                             getattr(stat, 'st_birthtime', 0.0) or 0.0, None if is_folder else stat.st_size)
                if matches(item, criteria):
                    results.append(item)
        return results, True

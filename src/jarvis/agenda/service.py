"""The agenda across every configured calendar: sources from config, named ranges, merged results.

See ``agenda.spec.md``.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, List, Sequence

from ..debug import debug_log
from . import ics
from .sources import IcsSource, OutlookCalendarSource, SourceResult

RANGES = ("today", "tomorrow", "week")
SOURCE_TYPES = ("ics", "outlook")
_WEEK_DAYS = 7
_lock = threading.Lock()
_ics_sources: dict = {}


@dataclass
class Agenda:
    occurrences: List[ics.Occurrence] = field(default_factory=list)
    statuses: List[SourceResult] = field(default_factory=list)


def cache_dir(cfg: Any) -> Path:
    """Where saved calendar copies live: next to the Jarvis database."""
    db_path = str(getattr(cfg, "db_path", "") or "")
    base = Path(db_path).parent if db_path and db_path != ":memory:" else Path.home() / ".local" / "share" / "jarvis"
    return base / "calendar_cache"


def sources_from_config(cfg: Any) -> list:
    """One source per entry of ``calendar_sources``; ICS addresses are read from the credential store."""
    from .. import credentials
    ttl = float(getattr(cfg, "calendar_cache_minutes", 15) or 0) * 60.0
    folder = cache_dir(cfg)
    built = []
    for entry in getattr(cfg, "calendar_sources", None) or []:
        name, kind = entry.get("name"), entry.get("type")
        if kind == "ics":
            # Kept for the process so the parsed copy is reused instead of read again per request.
            identity = (name, str(folder), ttl)
            with _lock:
                source = _ics_sources.get(identity)
                if source is None:
                    key = credentials.account_key("calendar_url", name)
                    source = IcsSource(name, cache_dir=folder, ttl_sec=ttl,
                                       address=lambda key=key: credentials.get_secret(key))
                    _ics_sources[identity] = source
            built.append(source)
        elif kind == "outlook":
            built.append(OutlookCalendarSource(name))
    return built


def named_range(name: str, now: datetime) -> tuple:
    """``today``, ``tomorrow`` or ``week`` (today and the next six days) as whole local days."""
    today = datetime.combine(now.astimezone().date(), time())
    if name == "tomorrow":
        start, days = today + timedelta(days=1), 1
    elif name == "week":
        start, days = today, _WEEK_DAYS
    else:
        start, days = today, 1
    # Midnights are local wall times, so a daylight-saving day is still one whole day.
    return start.astimezone(), (start + timedelta(days=days)).astimezone()


def read_agenda(sources: Sequence[Any], start: datetime, end: datetime) -> Agenda:
    """Every source read in parallel and merged in time order; a failing source never hides the others."""
    if not sources:
        return Agenda()
    with ThreadPoolExecutor(max_workers=min(4, len(sources)), thread_name_prefix="agenda") as pool:
        results = list(pool.map(lambda source: _safe_read(source, start, end), sources))
    merged = [occ for result in results for occ in result.occurrences]
    merged.sort(key=lambda o: (o.start, o.title))
    debug_log(f"agenda: {len(merged)} events from {sum(r.ok for r in results)}/{len(results)} calendars", "agenda")
    return Agenda(merged, results)


def _safe_read(source: Any, start: datetime, end: datetime) -> SourceResult:
    try:
        return source.read(start, end)
    except Exception as exc:  # noqa: BLE001 - one broken calendar must not break the agenda
        debug_log(f"calendar source failed ({type(exc).__name__})", "agenda")
        return SourceResult(source.name, getattr(source, "kind", "?"), False, error="failed")

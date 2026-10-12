"""Where calendar events come from: a secret ICS address (Google, Outlook.com, any CalDAV server's
export) with a local copy for offline use, and classic Outlook on this PC through COM.

Every source answers ``read(start, end)`` with a ``SourceResult``; failures are results with an
error code, never exceptions. Addresses are secrets: they come from the credential store through the
``address`` callable and are never logged, cached by name or returned. See ``agenda.spec.md``.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from ..debug import debug_log
from ..platform.windows.outlook import OutlookUnavailable
from . import ics

FETCH_TIMEOUT_SEC = 15.0
MAX_DOWNLOAD_BYTES = 8_000_000
OUTLOOK_MAX_ITEMS = 500
_OL_FOLDER_CALENDAR = 9

__all__ = ["IcsSource", "OutlookCalendarSource", "OutlookUnavailable", "SourceResult", "normalise_address"]


@dataclass
class SourceResult:
    name: str
    kind: str
    ok: bool
    occurrences: List[ics.Occurrence] = field(default_factory=list)
    # True when the events come from the last saved copy because a fresh fetch failed.
    offline_copy: bool = False
    fetched_at: Optional[float] = None
    # Empty, or one of: no_address, unreachable, http_error, not_a_calendar, too_large,
    # outlook_not_installed, outlook_new_only, outlook_timeout, outlook_failed.
    error: str = ""


def normalise_address(url: str) -> str:
    """``webcal://`` as ``https://``; anything but http(s) raises ``ValueError``."""
    text = (url or "").strip()
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme in ("webcal", "webcals"):
        parts = parts._replace(scheme="https")
    elif scheme not in ("http", "https"):
        raise ValueError("a calendar address must start with https://, http:// or webcal://")
    if not parts.netloc:
        raise ValueError("a calendar address needs a host")
    return urlunsplit(parts)


class FetchError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fetch_text(url: str, timeout_sec: float = FETCH_TIMEOUT_SEC) -> str:
    """GET ``url`` with a size cap. Raises ``FetchError`` (``unreachable``, ``http_error``, ``too_large``)."""
    import requests
    try:
        with requests.get(normalise_address(url), timeout=timeout_sec, stream=True,
                          headers={"Accept": "text/calendar, */*;q=0.5"}) as response:
            if response.status_code >= 400:
                raise FetchError("http_error")
            chunks = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise FetchError("too_large")
                chunks.append(chunk)
            data = b"".join(chunks)
            return data.decode(response.encoding or "utf-8", errors="replace") if response.encoding \
                else data.decode("utf-8", errors="replace")
    except FetchError:
        raise
    except ValueError as exc:
        raise FetchError("http_error") from exc
    except Exception as exc:  # noqa: BLE001 - every network failure means the same to the user
        debug_log(f"calendar fetch failed ({type(exc).__name__})", "agenda")
        raise FetchError("unreachable") from exc


class IcsSource:
    """One calendar's secret ICS address, with a copy on disk reused for ``ttl_sec`` and offline."""

    kind = "ics"

    def __init__(self, name: str, *, cache_dir: Path, ttl_sec: float, address: Callable[[], str],
                 clock: Callable[[], float] = time.time, fetch: Callable[[str], str] = fetch_text):
        self.name = name
        self.address = address
        self._cache_dir = Path(cache_dir)
        self._ttl = max(0.0, float(ttl_sec))
        self._clock = clock
        self._fetch = fetch
        self._parsed: Optional[Tuple[float, ics.Calendar]] = None

    @property
    def cache_path(self) -> Path:
        digest = hashlib.sha256(self.name.encode("utf-8")).hexdigest()[:16]
        return self._cache_dir / f"calendar-{digest}.ics"

    def _saved(self) -> Optional[Tuple[float, ics.Calendar]]:
        path = self.cache_path
        try:
            fetched_at = path.stat().st_mtime
        except OSError:
            return None
        if self._parsed is not None and self._parsed[0] == fetched_at:
            return self._parsed
        try:
            calendar = ics.parse_calendar(path.read_text(encoding="utf-8"))
        except (OSError, ics.CalendarFormatError):
            return None
        self._parsed = (fetched_at, calendar)
        return self._parsed

    def _save(self, text: str, now: float) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.cache_path)
            os.utime(self.cache_path, (now, now))
        except OSError as exc:
            debug_log(f"calendar copy not saved ({type(exc).__name__})", "agenda")

    def forget_copy(self) -> None:
        try:
            self.cache_path.unlink()
        except OSError:
            pass
        self._parsed = None

    def read(self, start: datetime, end: datetime) -> SourceResult:
        now = self._clock()
        saved = self._saved()
        if saved is not None and now - saved[0] < self._ttl:
            return self._result(saved, start, end, offline=False)
        url = self.address()
        if not url:
            return SourceResult(self.name, self.kind, False, error="no_address")
        try:
            text = self._fetch(url)
            calendar = ics.parse_calendar(text)
        except FetchError as exc:
            if saved is not None:
                debug_log(f"calendar fetch failed ({exc.code}); last copy used", "agenda")
                result = self._result(saved, start, end, offline=True)
                result.error = exc.code
                return result
            return SourceResult(self.name, self.kind, False, error=exc.code)
        except ics.CalendarFormatError:
            debug_log("calendar address did not return a calendar", "agenda")
            return SourceResult(self.name, self.kind, False, error="not_a_calendar")
        self._save(text, now)
        stamp = self.cache_path.stat().st_mtime if self.cache_path.exists() else now
        self._parsed = (stamp, calendar)
        return self._result((now, calendar), start, end, offline=False)

    def _result(self, saved: Tuple[float, ics.Calendar], start: datetime, end: datetime, *,
                offline: bool) -> SourceResult:
        found = ics.occurrences(saved[1], start, end)
        for occ in found:
            occ.calendar = self.name
        return SourceResult(self.name, self.kind, True, found, offline_copy=offline, fetched_at=saved[0])


def _local(moment: Any) -> datetime:
    """An Outlook COM time (a local wall time, which pywin32 may label UTC) as an aware local time."""
    naive = datetime(moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    return naive.astimezone()


def _restrict_time(moment: datetime) -> str:
    """A time in the form Outlook's ``Items.Restrict`` accepts (local wall time)."""
    return moment.astimezone().strftime("%m/%d/%Y %I:%M %p")


class OutlookCalendarSource:
    """The default calendar of classic Outlook on this PC, read through COM."""

    kind = "outlook"

    def __init__(self, name: str, *, runner: Optional[Callable] = None,
                 install_state: Optional[Callable[[], str]] = None):
        from ..platform.windows import outlook
        self.name = name
        self._runner = runner or (lambda fn, timeout: outlook.run_with_namespace(fn, timeout))
        self._install_state = install_state or outlook.install_state

    def read(self, start: datetime, end: datetime) -> SourceResult:
        state = self._install_state()
        if state != "classic":
            code = "outlook_new_only" if state == "new_only" else "outlook_not_installed"
            return SourceResult(self.name, self.kind, False, error=code)

        def collect(namespace) -> List[Dict[str, Any]]:
            items = namespace.GetDefaultFolder(_OL_FOLDER_CALENDAR).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            query = f"[Start] < '{_restrict_time(end)}' AND [End] > '{_restrict_time(start)}'"
            try:
                chosen = items.Restrict(query)
            except Exception:  # noqa: BLE001 - a locale that reads the date differently
                debug_log("outlook restrict failed; scanning sorted items", "agenda")
                chosen = items
            out = []
            for index, item in enumerate(chosen):
                if index >= OUTLOOK_MAX_ITEMS:
                    break
                item_start = _local(item.Start)
                if item_start >= end:
                    break
                out.append({"title": str(item.Subject or ""), "location": str(getattr(item, "Location", "") or ""),
                            "start": item_start, "end": _local(item.End),
                            "all_day": bool(getattr(item, "AllDayEvent", False))})
            return out

        try:
            rows = self._runner(collect, 20.0)
        except OutlookUnavailable as exc:
            return SourceResult(self.name, self.kind, False, error=exc.reason)
        found = []
        for row in rows:
            if row["end"] <= start and row["end"] != row["start"]:
                continue
            found.append(ics.Occurrence(" ".join(row["title"].split())[:300], " ".join(row["location"].split())[:300],
                                        row["start"], row["end"], row["all_day"], calendar=self.name))
        debug_log(f"outlook calendar: {len(found)} events", "agenda")
        return SourceResult(self.name, self.kind, True, found, fetched_at=time.time())

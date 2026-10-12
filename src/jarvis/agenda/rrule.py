"""Bounded RFC 5545 recurrence expansion for calendar events.

Supports FREQ DAILY, WEEKLY, MONTHLY and YEARLY with INTERVAL, COUNT, UNTIL, BYDAY (with ordinals
such as ``2TU`` or ``-1FR``), BYMONTHDAY (negative days count from the month's end), BYMONTH,
BYSETPOS and WKST. That covers what Google Calendar and Outlook write. Instances are naive wall
times in the event's own zone, so a 10:00 meeting stays at 10:00 across a daylight-saving change.

Anything else (HOURLY, BYWEEKNO, BYYEARDAY, ...) raises ``UnsupportedRule``; the caller keeps the
first instance only. Every expansion is bounded by ``MAX_PERIODS``.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date, datetime, timedelta
from typing import Dict, Iterator, List, Optional, Tuple

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_PERIODS = 60_000
_SUPPORTED = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}
_FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")


class UnsupportedRule(ValueError):
    """A recurrence rule this expander does not implement."""


def parse(value: str) -> Dict[str, str]:
    """``FREQ=WEEKLY;BYDAY=MO`` as ``{"FREQ": "WEEKLY", "BYDAY": "MO"}`` (keys upper-cased)."""
    rule: Dict[str, str] = {}
    for part in (value or "").split(";"):
        if "=" in part:
            key, _, val = part.partition("=")
            rule[key.strip().upper()] = val.strip()
    return rule


def _ints(text: Optional[str]) -> List[int]:
    out = []
    for piece in (text or "").split(","):
        piece = piece.strip()
        if piece:
            out.append(int(piece))
    return out


def _bydays(text: Optional[str]) -> List[Tuple[int, int]]:
    """``2TU,-1FR,MO`` as ``[(2, 1), (-1, 4), (0, 0)]``: (ordinal or 0, weekday index)."""
    out = []
    for piece in (text or "").split(","):
        piece = piece.strip().upper()
        if not piece:
            continue
        day = piece[-2:]
        if day not in WEEKDAYS:
            raise UnsupportedRule(f"BYDAY {piece}")
        number = piece[:-2]
        out.append((int(number) if number not in ("", "+") else 0, WEEKDAYS.index(day)))
    return out


def _month_days(year: int, month: int, bymonthday: List[int], byday: List[Tuple[int, int]],
                default_day: int) -> List[int]:
    """Days of one month chosen by BYMONTHDAY and BYDAY (both narrow, as RFC 5545 says)."""
    length = _calendar.monthrange(year, month)[1]
    chosen: Optional[set] = None
    if bymonthday:
        chosen = {d if d > 0 else length + d + 1 for d in bymonthday}
        chosen = {d for d in chosen if 1 <= d <= length}
    if byday:
        matches = set()
        for ordinal, weekday in byday:
            days = [d for d in range(1, length + 1) if date(year, month, d).weekday() == weekday]
            if ordinal == 0:
                matches.update(days)
            elif -len(days) <= ordinal <= len(days) and ordinal != 0:
                matches.add(days[ordinal - 1] if ordinal > 0 else days[ordinal])
        chosen = matches if chosen is None else chosen & matches
    if chosen is None:
        chosen = {default_day} if default_day <= length else set()
    return sorted(chosen)


def _apply_setpos(candidates: List[datetime], setpos: List[int]) -> List[datetime]:
    if not setpos:
        return candidates
    picked = set()
    for pos in setpos:
        if 0 < pos <= len(candidates):
            picked.add(candidates[pos - 1])
        elif 0 < -pos <= len(candidates):
            picked.add(candidates[pos])
    return sorted(picked)


def instances(rule: Dict[str, str], dtstart: datetime, *, until: Optional[datetime] = None,
              window_start: Optional[datetime] = None, stop_after: Optional[datetime] = None
              ) -> Iterator[datetime]:
    """The series' instances as naive wall times, starting with ``dtstart`` itself.

    ``until`` is the rule's UNTIL already converted to the same naive wall time as ``dtstart``.
    ``window_start`` lets a rule without COUNT skip whole periods that end before it (instances before
    it may still be yielded; the caller filters). ``stop_after`` ends the expansion once candidates
    pass it. Raises ``UnsupportedRule`` before yielding anything when the rule is outside what is
    supported.
    """
    freq = rule.get("FREQ", "").upper()
    if freq not in _FREQUENCIES or set(rule) - _SUPPORTED:
        raise UnsupportedRule(freq or "no FREQ")
    try:
        interval = max(1, int(rule.get("INTERVAL") or 1))
        count = int(rule["COUNT"]) if rule.get("COUNT") else None
        bymonth = _ints(rule.get("BYMONTH"))
        bymonthday = _ints(rule.get("BYMONTHDAY"))
        setpos = _ints(rule.get("BYSETPOS"))
        byday = _bydays(rule.get("BYDAY"))
    except ValueError as exc:
        raise UnsupportedRule(str(exc)) from exc
    wkst_name = rule.get("WKST", "MO").upper()
    wkst = WEEKDAYS.index(wkst_name) if wkst_name in WEEKDAYS else 0
    if freq in ("DAILY", "WEEKLY") and any(o for o, _ in byday):
        raise UnsupportedRule("ordinal BYDAY in a daily or weekly rule")
    start_index = 0
    if count is None and window_start is not None and window_start > dtstart and freq in ("DAILY", "WEEKLY"):
        span = interval * (1 if freq == "DAILY" else 7)
        start_index = max(0, (window_start.date() - dtstart.date()).days // span - 1)
    return _generate(freq, interval, count, bymonth, bymonthday, setpos, byday, wkst, dtstart, until,
                     stop_after, start_index)


def _generate(freq, interval, count, bymonth, bymonthday, setpos, byday, wkst, dtstart, until, stop_after,
              start_index) -> Iterator[datetime]:
    clock = dtstart.time()

    def period(index: int) -> List[datetime]:
        if freq == "DAILY":
            day = dtstart.date() + timedelta(days=index * interval)
            if bymonth and day.month not in bymonth:
                return []
            if bymonthday and day.day not in _month_days(day.year, day.month, bymonthday, [], 0):
                return []
            if byday and day.weekday() not in {w for _, w in byday}:
                return []
            return [datetime.combine(day, clock)]
        if freq == "WEEKLY":
            week_start = dtstart.date() - timedelta(days=(dtstart.weekday() - wkst) % 7)
            week_start += timedelta(weeks=index * interval)
            weekdays = sorted({w for _, w in byday}) if byday else [dtstart.weekday()]
            days = [week_start + timedelta(days=(w - wkst) % 7) for w in weekdays]
            return sorted(datetime.combine(d, clock) for d in days if not bymonth or d.month in bymonth)
        if freq == "MONTHLY":
            months = dtstart.year * 12 + dtstart.month - 1 + index * interval
            year, month = divmod(months, 12)
            if year > 9999:
                return []
            month += 1
            if bymonth and month not in bymonth:
                return []
            return [datetime.combine(date(year, month, d), clock)
                    for d in _month_days(year, month, bymonthday, byday, dtstart.day)]
        year = dtstart.year + index * interval
        if year > 9999:
            return []
        out: List[datetime] = []
        if not bymonth and byday and not bymonthday:
            # Weekdays counted through the whole year (``20MO`` is the year's twentieth Monday).
            first = date(year, 1, 1)
            days = [first + timedelta(days=n) for n in range(366 if _calendar.isleap(year) else 365)]
            for ordinal, weekday in byday:
                matching = [d for d in days if d.weekday() == weekday]
                if ordinal == 0:
                    out.extend(datetime.combine(d, clock) for d in matching)
                elif 0 < abs(ordinal) <= len(matching):
                    out.append(datetime.combine(matching[ordinal - 1] if ordinal > 0 else matching[ordinal], clock))
            return sorted(set(out))
        for month in (bymonth or [dtstart.month]):
            out.extend(datetime.combine(date(year, month, d), clock)
                       for d in _month_days(year, month, bymonthday, byday, dtstart.day))
        return sorted(out)

    emitted = 0
    if start_index == 0:
        # DTSTART is always the first instance, as Google, Outlook and RFC 5545 readers treat it.
        yield dtstart
        emitted = 1
        if count is not None and emitted >= count:
            return
    for index in range(start_index, start_index + MAX_PERIODS):
        for candidate in _apply_setpos(period(index), setpos):
            if candidate <= dtstart:
                continue
            if until is not None and candidate > until:
                return
            if stop_after is not None and candidate > stop_after:
                return
            yield candidate
            emitted += 1
            if count is not None and emitted >= count:
                return
        if stop_after is not None and _period_floor(freq, interval, dtstart, index) > stop_after:
            return


def _period_floor(freq: str, interval: int, dtstart: datetime, index: int) -> datetime:
    """A moment no later than any candidate of period ``index``."""
    if freq == "DAILY":
        return datetime.combine(dtstart.date() + timedelta(days=index * interval), datetime.min.time())
    if freq == "WEEKLY":
        return datetime.combine(dtstart.date() + timedelta(weeks=index * interval - 1), datetime.min.time())
    if freq == "MONTHLY":
        months = dtstart.year * 12 + dtstart.month - 1 + index * interval
        year, month = divmod(months, 12)
        return datetime(min(year, 9999), month + 1, 1)
    return datetime(min(dtstart.year + index * interval, 9999), 1, 1)

"""A bounded iCalendar (RFC 5545) reader for the agenda: events, time zones and recurrence.

``parse_calendar`` reads the text a Google Calendar or Outlook.com "secret address" serves;
``occurrences`` lists the events that overlap a window, with recurring series expanded
(``rrule.py``), EXDATE removed and RECURRENCE-ID overrides applied. Times come back as aware
datetimes. A TZID is resolved as an IANA name, then from the calendar's own VTIMEZONE rules, then as
a Windows zone name; anything else is read as local time. Floating times and all-day dates are local.

Only titles, places and times are kept. Descriptions, attendees and attachments are never read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..debug import debug_log
from . import rrule

MAX_TEXT_CHARS = 8_000_000
MAX_EVENTS = 20_000
MAX_INSTANCES = 2_000
_TEXT_CHARS = 300

# Windows zone names Outlook writes in TZID, mapped to IANA names (the common ones; others use the
# calendar's VTIMEZONE).
WINDOWS_ZONES = {
    "UTC": "UTC", "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest", "Turkey Standard Time": "Europe/Istanbul",
    "Russian Standard Time": "Europe/Moscow", "Israel Standard Time": "Asia/Jerusalem",
    "South Africa Standard Time": "Africa/Johannesburg", "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore", "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul", "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland", "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage", "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax", "Newfoundland Standard Time": "America/St_Johns",
    "SA Pacific Standard Time": "America/Bogota", "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Central Standard Time (Mexico)": "America/Mexico_City",
}


class CalendarFormatError(ValueError):
    """The text is not an iCalendar file."""


@dataclass
class Property:
    name: str
    params: Dict[str, str]
    value: str


@dataclass
class Component:
    name: str
    props: List[Property] = field(default_factory=list)
    children: List["Component"] = field(default_factory=list)

    def get(self, name: str) -> Optional[Property]:
        for prop in self.props:
            if prop.name == name:
                return prop
        return None

    def all(self, name: str) -> List[Property]:
        return [prop for prop in self.props if prop.name == name]


@dataclass
class Calendar:
    events: List[Component]
    zones: Dict[str, tzinfo]


@dataclass
class Occurrence:
    title: str
    location: str
    start: datetime
    end: datetime
    all_day: bool
    uid: str = ""
    calendar: str = ""

    @property
    def start_date(self) -> date:
        return self.start.date() if self.all_day else self.start.astimezone().date()


# ── Content lines ──────────────────────────────────────────────────────────

def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_line(line: str) -> Optional[Property]:
    """``NAME;P=V;Q="a:b":value`` as a property; colons and semicolons inside quotes are not separators."""
    in_quotes = False
    head_end = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            head_end = index
            break
    if head_end < 0:
        return None
    head, value = line[:head_end], line[head_end + 1:]
    parts: List[str] = []
    current = ""
    in_quotes = False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
            current += char
        elif char == ";" and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    params = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return Property(parts[0].strip().upper(), params, value)


def _unescape(text: str) -> str:
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append("\n" if nxt in "nN" else nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _clean_text(text: str) -> str:
    """One line of at most ``_TEXT_CHARS`` characters."""
    return " ".join(_unescape(text).split())[:_TEXT_CHARS]


# ── Parsing ────────────────────────────────────────────────────────────────

def parse_calendar(text: str) -> Calendar:
    """Read an iCalendar file. Raises ``CalendarFormatError`` when ``text`` is not one."""
    if not isinstance(text, str) or "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise CalendarFormatError("not an iCalendar file")
    text = text[:MAX_TEXT_CHARS]
    root = Component("ROOT")
    stack = [root]
    for line in _unfold(text):
        prop = _split_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN":
            child = Component(prop.value.strip().upper())
            stack[-1].children.append(child)
            stack.append(child)
        elif prop.name == "END":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].props.append(prop)
    calendars = [c for c in root.children if c.name == "VCALENDAR"]
    if not calendars:
        raise CalendarFormatError("no VCALENDAR")
    events: List[Component] = []
    raw_zones: List[Component] = []
    for cal in calendars:
        for child in cal.children:
            if child.name == "VEVENT" and len(events) < MAX_EVENTS:
                events.append(child)
            elif child.name == "VTIMEZONE":
                raw_zones.append(child)
    zones: Dict[str, tzinfo] = {}
    for zone in raw_zones:
        tzid = zone.get("TZID")
        if tzid is None:
            continue
        try:
            zones[tzid.value.strip()] = VTimezone.from_component(zone)
        except (ValueError, rrule.UnsupportedRule) as exc:
            debug_log(f"calendar time zone rules unreadable ({type(exc).__name__})", "agenda")
    return Calendar(events, zones)


# ── Time zones ─────────────────────────────────────────────────────────────

def _offset(text: str) -> timedelta:
    match = re.fullmatch(r"([+-])(\d\d)(\d\d)(\d\d)?", text.strip())
    if not match:
        raise ValueError("bad UTC offset")
    sign = -1 if match.group(1) == "-" else 1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)),
                            seconds=int(match.group(4) or 0))


class VTimezone(tzinfo):
    """A zone built from a calendar's VTIMEZONE STANDARD and DAYLIGHT rules."""

    def __init__(self, name: str, rules: List[Tuple[datetime, timedelta, timedelta, Optional[dict],
                                                    List[datetime], bool]]):
        self._name = name
        self._rules = rules
        self._cache: Dict[int, List[Tuple[datetime, timedelta, bool]]] = {}
        earliest = min(rules, key=lambda r: r[0])
        self._before = earliest[1]
        standard = [r for r in rules if not r[5]]
        self._standard = (standard or rules)[0][2]

    @classmethod
    def from_component(cls, component: Component) -> "VTimezone":
        rules = []
        for child in component.children:
            if child.name not in ("STANDARD", "DAYLIGHT"):
                continue
            start = child.get("DTSTART")
            frm, to = child.get("TZOFFSETFROM"), child.get("TZOFFSETTO")
            if start is None or frm is None or to is None:
                continue
            onset = datetime.strptime(start.value.strip()[:15], "%Y%m%dT%H%M%S")
            rule = child.get("RRULE")
            rdates = [datetime.strptime(v.strip()[:15], "%Y%m%dT%H%M%S")
                      for prop in child.all("RDATE") for v in prop.value.split(",") if len(v.strip()) >= 15]
            rules.append((onset, _offset(frm.value), _offset(to.value), rrule.parse(rule.value) if rule else None,
                          rdates, child.name == "DAYLIGHT"))
        if not rules:
            raise ValueError("no zone rules")
        tzid = component.get("TZID")
        return cls(tzid.value.strip() if tzid else "zone", rules)

    def _transitions(self, year: int) -> List[Tuple[datetime, timedelta, bool]]:
        """Changes of offset from the year before ``year`` to the year after, as local wall times."""
        if year not in self._cache:
            found = []
            for onset, frm, to, rule, rdates, daylight in self._rules:
                moments = [onset] + rdates
                if rule is not None:
                    rule = dict(rule)
                    until = _parse_until(rule.pop("UNTIL"), None, frm) if rule.get("UNTIL") else None
                    try:
                        moments += list(rrule.instances(rule, onset, until=until,
                                                        stop_after=datetime(min(year + 1, 9998), 12, 31)))
                    except rrule.UnsupportedRule:
                        pass
                found.extend((m, to, daylight) for m in moments if year - 1 <= m.year <= year + 1)
            self._cache[year] = sorted(set(found), key=lambda t: t[0])
        return self._cache[year]

    def _current(self, local: datetime) -> Tuple[timedelta, bool]:
        offset, daylight = self._before, False
        for moment, to, is_daylight in self._transitions(local.year):
            if moment <= local:
                offset, daylight = to, is_daylight
            else:
                break
        return offset, daylight

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return self._standard
        return self._current(dt.replace(tzinfo=None))[0]

    def dst(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return timedelta(0)
        return self.utcoffset(dt) - self._standard

    def tzname(self, dt: Optional[datetime]) -> str:
        return self._name

    def fromutc(self, dt: datetime) -> datetime:
        naive = dt.replace(tzinfo=None)
        guess = naive + self._standard
        local = naive + self.utcoffset(guess)
        return local.replace(tzinfo=self)


def resolve_zone(tzid: Optional[str], zones: Dict[str, tzinfo]) -> Optional[tzinfo]:
    """The zone a TZID names, or None to read the time as local."""
    if not tzid:
        return None
    name = tzid.strip().strip('"')
    candidates = [name]
    if "/" in name:
        pieces = [p for p in name.split("/") if p]
        candidates += ["/".join(pieces[-2:]), "/".join(pieces[-3:])]
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    if name in zones:
        return zones[name]
    if name in WINDOWS_ZONES:
        try:
            return ZoneInfo(WINDOWS_ZONES[name])
        except ZoneInfoNotFoundError:
            pass
    debug_log("calendar time zone not recognised; read as local time", "agenda")
    return None


# ── Values ─────────────────────────────────────────────────────────────────

def _localise(naive: datetime, zone: Optional[tzinfo]) -> datetime:
    """A naive wall time in ``zone`` (None is this PC's local time) as an aware datetime."""
    if zone is None:
        return naive.astimezone()
    return naive.replace(tzinfo=zone)


def _parse_value(prop: Property, zones: Dict[str, tzinfo]) -> Tuple[datetime, bool, Optional[tzinfo], bool]:
    """(aware moment, all-day, the zone wall times repeat in, whether it was UTC)."""
    raw = prop.value.strip()
    if prop.params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", raw):
        day = datetime.strptime(raw[:8], "%Y%m%d")
        return day, True, None, False
    utc = raw.endswith("Z")
    naive = datetime.strptime(raw.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return naive.replace(tzinfo=timezone.utc), False, timezone.utc, True
    zone = resolve_zone(prop.params.get("TZID"), zones)
    return _localise(naive, zone), False, zone, False


def _parse_until(raw: str, zone: Optional[tzinfo], offset: Optional[timedelta] = None) -> datetime:
    """UNTIL as a naive wall time comparable with the series' instances."""
    raw = raw.strip()
    if re.fullmatch(r"\d{8}", raw):
        return datetime.strptime(raw, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
    naive = datetime.strptime(raw.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if raw.endswith("Z"):
        if offset is not None:
            return naive + offset
        moment = naive.replace(tzinfo=timezone.utc)
        return (moment.astimezone(zone) if zone is not None else moment.astimezone()).replace(tzinfo=None)
    return naive


_DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _duration(text: str) -> Optional[timedelta]:
    match = _DURATION.match(text.strip().upper())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups()[1:])
    return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def _key(moment: datetime, all_day: bool) -> str:
    """A comparable identity for an instance: its date for all-day events, its UTC instant otherwise."""
    if all_day:
        return moment.strftime("%Y%m%d")
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


# ── Expansion ──────────────────────────────────────────────────────────────

def _cancelled(event: Component) -> bool:
    status = event.get("STATUS")
    return status is not None and status.value.strip().upper() == "CANCELLED"


def _text(event: Component, name: str) -> str:
    prop = event.get(name)
    return _clean_text(prop.value) if prop is not None else ""


def occurrences(calendar: Calendar, window_start: datetime, window_end: datetime) -> List[Occurrence]:
    """Events overlapping ``[window_start, window_end)``, sorted by start, at most ``MAX_INSTANCES``.

    ``window_start`` and ``window_end`` are aware datetimes."""
    masters: List[Component] = []
    overrides: Dict[str, Dict[str, Component]] = {}
    for event in calendar.events:
        rid = event.get("RECURRENCE-ID")
        uid = event.get("UID")
        if rid is not None and uid is not None:
            try:
                moment, all_day, _zone, _utc = _parse_value(rid, calendar.zones)
            except ValueError:
                continue
            overrides.setdefault(uid.value.strip(), {})[_key(moment, all_day)] = event
        else:
            masters.append(event)

    found: List[Occurrence] = []
    for event in masters:
        try:
            found.extend(_event_occurrences(event, calendar, overrides, window_start, window_end))
        except (ValueError, OverflowError) as exc:
            debug_log(f"calendar event skipped ({type(exc).__name__})", "agenda")
        if len(found) >= MAX_INSTANCES:
            break
    # Overrides whose series is missing are events of their own.
    master_uids = {e.get("UID").value.strip() for e in masters if e.get("UID") is not None}
    for uid, moved in overrides.items():
        if uid not in master_uids:
            for event in moved.values():
                found.extend(_event_occurrences(event, calendar, {}, window_start, window_end, single_only=True))
    found.sort(key=lambda o: (o.start, o.title))
    return found[:MAX_INSTANCES]


def _single(event: Component, calendar: Calendar) -> Optional[Tuple[datetime, datetime, bool, Optional[tzinfo]]]:
    start_prop = event.get("DTSTART")
    if start_prop is None:
        return None
    start, all_day, zone, _utc = _parse_value(start_prop, calendar.zones)
    end_prop = event.get("DTEND")
    duration_prop = event.get("DURATION")
    if end_prop is not None:
        end = _parse_value(end_prop, calendar.zones)[0]
    elif duration_prop is not None and _duration(duration_prop.value) is not None:
        end = start + _duration(duration_prop.value)
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    if all_day:
        end = datetime(end.year, end.month, end.day)
    if end < start:
        end = start
    return start, end, all_day, zone


def _occurrence(event: Component, start: datetime, end: datetime, all_day: bool) -> Occurrence:
    uid = event.get("UID")
    if all_day:
        start = datetime.combine(start.date(), time()).astimezone()
        end = datetime.combine(end.date(), time()).astimezone()
    return Occurrence(_text(event, "SUMMARY"), _text(event, "LOCATION"), start, end, all_day,
                      uid.value.strip() if uid else "")


def _overlaps(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> bool:
    if end == start:
        return window_start <= start < window_end
    return start < window_end and end > window_start


def _event_occurrences(event, calendar, overrides, window_start, window_end, single_only=False
                       ) -> List[Occurrence]:
    if _cancelled(event):
        return []
    single = _single(event, calendar)
    if single is None:
        return []
    start, end, all_day, zone = single
    length = end - start
    uid_prop = event.get("UID")
    uid = uid_prop.value.strip() if uid_prop else ""
    rule_prop = event.get("RRULE")
    out: List[Occurrence] = []

    def emit(source: Component, s: datetime, e: datetime, is_all_day: bool) -> None:
        occ = _occurrence(source, s, e, is_all_day)
        if _overlaps(occ.start, occ.end, window_start, window_end):
            out.append(occ)

    if single_only or (rule_prop is None and not event.all("RDATE")):
        emit(event, start, end, all_day)
        return out

    excluded = set()
    for prop in event.all("EXDATE"):
        for piece in prop.value.split(","):
            if piece.strip():
                moment, ex_all_day, _z, _u = _parse_value(Property("EXDATE", prop.params, piece), calendar.zones)
                excluded.add(_key(moment, ex_all_day or all_day))
    moved = overrides.get(uid, {})
    # A moved instance is listed where it moved to, even when its original time is outside the window.
    for override in moved.values():
        replaced = None if _cancelled(override) else _single(override, calendar)
        if replaced is not None:
            emit(override, replaced[0], replaced[1], replaced[2])

    # Instances are naive wall times in the event's zone (local time when it has none).
    def wall(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment
        return (moment.astimezone(zone) if zone is not None and not all_day else moment.astimezone()
                ).replace(tzinfo=None)

    def aware(naive: datetime) -> datetime:
        return naive if all_day else _localise(naive, zone)

    naive_start = wall(start)
    margin = length + timedelta(days=1)
    search_from = wall(window_start - margin)
    search_to = wall(window_end)
    starts: List[datetime] = []
    if rule_prop is not None:
        rule = rrule.parse(rule_prop.value)
        until = _parse_until(rule.pop("UNTIL"), zone) if rule.get("UNTIL") else None
        try:
            for naive in rrule.instances(rule, naive_start, until=until, window_start=search_from,
                                         stop_after=search_to):
                starts.append(naive)
                if len(starts) > rrule.MAX_PERIODS:
                    break
        except rrule.UnsupportedRule:
            debug_log("calendar recurrence rule not supported; first instance only", "agenda")
            starts = [naive_start]
    else:
        starts = [naive_start]
    for prop in event.all("RDATE"):
        if prop.params.get("VALUE", "").upper() == "PERIOD":
            continue
        for piece in prop.value.split(","):
            if piece.strip():
                moment = _parse_value(Property("RDATE", prop.params, piece), calendar.zones)[0]
                starts.append(wall(moment))

    for naive in sorted(set(starts)):
        if naive < search_from:
            continue
        instance_start = aware(naive)
        key = _key(instance_start, all_day)
        if key in excluded:
            continue
        if key in moved:
            continue
        emit(event, instance_start, instance_start + length, all_day)
    return out

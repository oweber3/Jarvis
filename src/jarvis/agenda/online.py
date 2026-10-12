"""Calendars read through a browser sign-in: Microsoft (Graph ``/me/calendarView``) and Google (Calendar API
``events.list`` over the calendars the user shows in Google Calendar).

Both are read only (the sign-in grants ``Calendars.Read`` or ``calendar.readonly``), answer
``read(start, end)`` with a ``SourceResult`` and never raise. Recurring events arrive already expanded.
Nothing is saved to disk. Paging links are followed only on the API's own host and within fixed bounds.
See ``agenda.spec.md``.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..accounts import http
from ..debug import debug_log
from . import ics
from .sources import SourceResult

MAX_EVENTS = 500
MAX_PAGES = 10
MAX_GOOGLE_CALENDARS = 25


def _line(text: Any) -> str:
    return " ".join(str(text or "").split())[:300]


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time()).astimezone()


def _graph_time(value: Dict[str, Any]) -> datetime:
    """A Graph ``dateTimeTimeZone`` asked for in UTC (seven fractional digits are cut to six)."""
    text = str(value.get("dateTime") or "")
    head, dot, fraction = text.partition(".")
    moment = datetime.fromisoformat(head + (dot + fraction[:6] if dot else ""))
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _failed(name: str, kind: str, code: str) -> SourceResult:
    return SourceResult(name, kind, False, error=code)


class MicrosoftCalendarSource:
    """The default calendar of a Microsoft account (Outlook.com or Microsoft 365)."""

    kind = "microsoft"

    def __init__(self, name: str, *, session: Any, transport: Any = None, base: Optional[str] = None):
        self.name = name
        self._session = session
        self._transport = transport
        self._base = base

    def read(self, start: datetime, end: datetime) -> SourceResult:
        base = self._base or http.GRAPH_BASE
        url = f"{base}/me/calendarView"
        params: Dict[str, Any] = {"startDateTime": _utc_text(start), "endDateTime": _utc_text(end),
                                  "$select": "subject,location,start,end,isAllDay,isCancelled",
                                  "$orderby": "start/dateTime", "$top": "100"}
        found: List[ics.Occurrence] = []
        try:
            for _page in range(MAX_PAGES):
                data = http.bearer_get(self._session, url, params, {"Prefer": 'outlook.timezone="UTC"'},
                                       transport=self._transport)
                for item in data.get("value") or []:
                    occurrence = self._occurrence(item) if isinstance(item, dict) else None
                    if occurrence is not None and occurrence.end > start and occurrence.start < end:
                        found.append(occurrence)
                link = data.get("@odata.nextLink")
                if not link or len(found) >= MAX_EVENTS or not http.same_origin(str(link), base):
                    break
                url, params = str(link), {}
        except http.ApiError as exc:
            return _failed(self.name, self.kind, exc.code)
        found = found[:MAX_EVENTS]
        debug_log(f"microsoft calendar: {len(found)} events", "agenda")
        return SourceResult(self.name, self.kind, True, found)

    def _occurrence(self, item: Dict[str, Any]) -> Optional[ics.Occurrence]:
        if item.get("isCancelled"):
            return None
        try:
            begin, finish = _graph_time(item.get("start") or {}), _graph_time(item.get("end") or {})
        except ValueError:
            return None
        all_day = bool(item.get("isAllDay"))
        if all_day:  # all-day events are whole local days, whatever zone they were written in
            begin, finish = _local_midnight(begin.date()), _local_midnight(finish.date())
        location = (item.get("location") or {}).get("displayName") if isinstance(item.get("location"), dict) else ""
        return ics.Occurrence(_line(item.get("subject")), _line(location), begin, finish, all_day, calendar=self.name)


def google_primary_calendar(session: Any, transport: Any = None, base: Optional[str] = None) -> str:
    """The primary calendar's ID (the account's address), or ``""``. Raises ``http.ApiError``."""
    data = http.bearer_get(session, f"{base or http.GOOGLE_CALENDAR_BASE}/users/me/calendarList",
                           {"minAccessRole": "owner", "maxResults": "250", "fields": "items(id,primary)"},
                           transport=transport)
    return next((str(c.get("id") or "") for c in data.get("items") or []
                 if isinstance(c, dict) and c.get("primary")), "")


def _google_time(value: Dict[str, Any]) -> tuple:
    if value.get("date"):
        return _local_midnight(date.fromisoformat(str(value["date"]))), True
    moment = datetime.fromisoformat(str(value.get("dateTime") or "").replace("Z", "+00:00"))
    return (moment if moment.tzinfo else moment.astimezone()), False


class GoogleCalendarSource:
    """Every calendar a Google account shows in Google Calendar (ticked in its list), read only."""

    kind = "google"

    def __init__(self, name: str, *, session: Any, transport: Any = None, base: Optional[str] = None):
        self.name = name
        self._session = session
        self._transport = transport
        self._base = base

    def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return http.bearer_get(self._session, url, params, transport=self._transport)

    def read(self, start: datetime, end: datetime) -> SourceResult:
        base = self._base or http.GOOGLE_CALENDAR_BASE
        found: List[ics.Occurrence] = []
        try:
            listing = self._get(f"{base}/users/me/calendarList",
                                {"minAccessRole": "reader", "maxResults": "250",
                                 "fields": "items(id,primary,selected,hidden,deleted)"})
            calendars = [str(c["id"]) for c in listing.get("items") or []
                         if isinstance(c, dict) and c.get("id") and (c.get("selected") or c.get("primary"))
                         and not c.get("hidden") and not c.get("deleted")][:MAX_GOOGLE_CALENDARS]
            for calendar_id in calendars:
                if len(found) >= MAX_EVENTS:
                    break
                try:
                    found.extend(self._events(base, calendar_id, start, end, MAX_EVENTS - len(found)))
                except http.ApiError as exc:
                    if exc.code != "server_error":
                        raise
                    debug_log("one google calendar could not be read; others kept", "agenda")
        except http.ApiError as exc:
            return _failed(self.name, self.kind, exc.code)
        found.sort(key=lambda o: (o.start, o.title))
        debug_log(f"google calendar: {len(found)} events from {len(calendars)} calendars", "agenda")
        return SourceResult(self.name, self.kind, True, found)

    def _events(self, base: str, calendar_id: str, start: datetime, end: datetime, room: int) -> List[ics.Occurrence]:
        out: List[ics.Occurrence] = []
        page_token = ""
        for _page in range(MAX_PAGES):
            params = {"singleEvents": "true", "orderBy": "startTime", "timeMin": _utc_text(start),
                      "timeMax": _utc_text(end), "maxResults": "250",
                      "fields": "items(summary,location,start,end,status),nextPageToken"}
            if page_token:
                params["pageToken"] = page_token
            data = self._get(f"{base}/calendars/{quote(calendar_id, safe='')}/events", params)
            for item in data.get("items") or []:
                if not isinstance(item, dict) or item.get("status") == "cancelled":
                    continue
                try:
                    begin, all_day = _google_time(item.get("start") or {})
                    finish, _ = _google_time(item.get("end") or {})
                except ValueError:
                    continue
                out.append(ics.Occurrence(_line(item.get("summary")), _line(item.get("location")), begin, finish,
                                          all_day, calendar=self.name))
            page_token = str(data.get("nextPageToken") or "")
            if not page_token or len(out) >= room:
                break
        return out[:room]

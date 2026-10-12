"""The ICS reader: events, all-day days, time zones, recurrence, exceptions and overrides.

Every calendar here is a literal string, so the tests read exactly what a Google or Outlook.com
"secret address" would serve, without any network.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.agenda import ics

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def _cal(*events: str, extra: str = "") -> str:
    body = "\r\n".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{extra}{body}\r\nEND:VCALENDAR\r\n"


def _event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "\r\n".join(lines) + "\r\nEND:VEVENT"


def _between(text, start, end):
    return ics.occurrences(ics.parse_calendar(text), start, end)


def _utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def test_a_timed_utc_event_inside_the_window_is_returned_with_its_title_and_place():
    text = _cal(_event("UID:1", "SUMMARY:Dentist", "LOCATION:High St\\, 4", "DTSTART:20261012T140000Z",
                       "DTEND:20261012T150000Z"))
    found = _between(text, _utc(2026, 10, 12), _utc(2026, 10, 13))
    assert [(o.title, o.location, o.start, o.end) for o in found] == [
        ("Dentist", "High St, 4", _utc(2026, 10, 12, 14), _utc(2026, 10, 12, 15))]
    assert found[0].all_day is False


def test_events_outside_the_window_are_left_out_and_overlapping_ones_kept():
    text = _cal(
        _event("UID:a", "SUMMARY:Before", "DTSTART:20261011T100000Z", "DTEND:20261011T110000Z"),
        _event("UID:b", "SUMMARY:Overnight", "DTSTART:20261011T230000Z", "DTEND:20261012T010000Z"),
        _event("UID:c", "SUMMARY:After", "DTSTART:20261013T100000Z", "DTEND:20261013T110000Z"))
    assert [o.title for o in _between(text, _utc(2026, 10, 12), _utc(2026, 10, 13))] == ["Overnight"]


def test_folded_lines_and_escaped_text_are_read_back_whole():
    text = _cal(_event("UID:1", "SUMMARY:Quarterly planning with the", "  whole team\\; bring notes",
                       "DTSTART:20261012T090000Z", "DTEND:20261012T100000Z"))
    assert _between(text, _utc(2026, 10, 12), _utc(2026, 10, 13))[0].title == \
        "Quarterly planning with the whole team; bring notes"


def test_an_all_day_event_covers_its_local_days_only():
    text = _cal(_event("UID:1", "SUMMARY:Holiday", "DTSTART;VALUE=DATE:20261012", "DTEND;VALUE=DATE:20261013"))
    day = datetime(2026, 10, 12).astimezone()
    found = _between(text, day, day + timedelta(days=1))
    assert len(found) == 1 and found[0].all_day and found[0].start_date == date(2026, 10, 12)
    assert _between(text, day + timedelta(days=1), day + timedelta(days=2)) == []


def test_a_tzid_start_is_read_in_that_zone():
    text = _cal(_event("UID:1", "SUMMARY:Call", "DTSTART;TZID=America/New_York:20261012T090000",
                       "DTEND;TZID=America/New_York:20261012T093000"))
    found = _between(text, _utc(2026, 10, 12), _utc(2026, 10, 13))
    assert found[0].start == datetime(2026, 10, 12, 9, tzinfo=NY)
    assert found[0].start.astimezone(UTC) == _utc(2026, 10, 12, 13)


OUTLOOK_TZ = (
    "BEGIN:VTIMEZONE\r\nTZID:Eastern Standard Time\r\n"
    "BEGIN:STANDARD\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\n"
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11\r\nEND:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\n"
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n")


@pytest.mark.parametrize("day,utc_hour", [((2026, 7, 1), 13), ((2026, 12, 1), 14)])
def test_a_windows_zone_name_is_resolved_on_both_sides_of_daylight_saving(day, utc_hour):
    stamp = "%04d%02d%02dT090000" % day
    text = _cal(_event("UID:1", "SUMMARY:Standup", f"DTSTART;TZID=Eastern Standard Time:{stamp}",
                       f"DTEND;TZID=Eastern Standard Time:{stamp[:-4]}3000"), extra=OUTLOOK_TZ)
    found = _between(text, _utc(*day), _utc(*day) + timedelta(days=1))
    assert found[0].start.astimezone(UTC) == _utc(*day, utc_hour)


def test_an_unknown_zone_uses_the_calendars_own_vtimezone_rules():
    zone = OUTLOOK_TZ.replace("Eastern Standard Time", "Custom Office Zone")
    text = _cal(_event("UID:1", "SUMMARY:Standup", "DTSTART;TZID=Custom Office Zone:20260701T090000",
                       "DURATION:PT15M"), extra=zone)
    found = _between(text, _utc(2026, 7, 1), _utc(2026, 7, 2))
    assert (found[0].start.astimezone(UTC), found[0].end - found[0].start) == (_utc(2026, 7, 1, 13),
                                                                               timedelta(minutes=15))


def test_a_weekly_rule_repeats_on_its_days_at_the_same_wall_time_across_daylight_saving():
    text = _cal(_event("UID:1", "SUMMARY:Lecture", "DTSTART;TZID=America/New_York:20261019T100000",
                       "DTEND;TZID=America/New_York:20261019T110000", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE"))
    found = _between(text, _utc(2026, 10, 26), _utc(2026, 11, 7))
    assert [o.start for o in found] == [datetime(2026, 10, d, 10, tzinfo=NY) for d in (26, 28)] + \
        [datetime(2026, 11, d, 10, tzinfo=NY) for d in (2, 4)]
    # The clocks went back on 1 November, so the same 10:00 is an hour later in UTC.
    assert found[0].start.astimezone(UTC).hour == 14 and found[2].start.astimezone(UTC).hour == 15


def test_count_and_until_end_a_series():
    counted = _cal(_event("UID:1", "SUMMARY:Course", "DTSTART:20261001T090000Z", "DTEND:20261001T100000Z",
                          "RRULE:FREQ=DAILY;COUNT=3"))
    assert [o.start.day for o in _between(counted, _utc(2026, 9, 1), _utc(2026, 11, 1))] == [1, 2, 3]
    until = _cal(_event("UID:1", "SUMMARY:Course", "DTSTART:20261001T090000Z", "DTEND:20261001T100000Z",
                        "RRULE:FREQ=WEEKLY;INTERVAL=2;UNTIL=20261029T090000Z"))
    assert [o.start.day for o in _between(until, _utc(2026, 9, 1), _utc(2026, 12, 1))] == [1, 15, 29]


def test_monthly_rules_by_ordinal_weekday_and_by_last_working_day():
    second_tuesday = _cal(_event("UID:1", "SUMMARY:Club", "DTSTART:20260113T180000Z", "DURATION:PT1H",
                                 "RRULE:FREQ=MONTHLY;BYDAY=2TU"))
    assert [o.start.date() for o in _between(second_tuesday, _utc(2026, 10, 1), _utc(2026, 12, 1))] == [
        date(2026, 10, 13), date(2026, 11, 10)]
    last_weekday = _cal(_event("UID:2", "SUMMARY:Report", "DTSTART:20260130T160000Z", "DURATION:PT1H",
                               "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"))
    assert [o.start.date() for o in _between(last_weekday, _utc(2026, 10, 1), _utc(2026, 12, 1))] == [
        date(2026, 10, 30), date(2026, 11, 30)]


def test_a_yearly_all_day_birthday_and_a_monthly_day_that_some_months_lack():
    birthday = _cal(_event("UID:1", "SUMMARY:Birthday", "DTSTART;VALUE=DATE:19900315",
                           "RRULE:FREQ=YEARLY"))
    day = datetime(2026, 3, 15).astimezone()
    found = _between(birthday, day, day + timedelta(days=1))
    assert [(o.title, o.start_date) for o in found] == [("Birthday", date(2026, 3, 15))]
    thirty_first = _cal(_event("UID:2", "SUMMARY:Rent", "DTSTART:20260131T090000Z", "DURATION:PT10M",
                               "RRULE:FREQ=MONTHLY"))
    assert [o.start.date() for o in _between(thirty_first, _utc(2026, 1, 1), _utc(2026, 6, 1))] == [
        date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)]


def test_excluded_dates_moved_and_cancelled_instances():
    text = _cal(
        _event("UID:s", "SUMMARY:Sync", "DTSTART:20261012T150000Z", "DTEND:20261012T153000Z",
               "RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20261013T150000Z"),
        _event("UID:s", "RECURRENCE-ID:20261014T150000Z", "SUMMARY:Sync (moved)",
               "DTSTART:20261014T170000Z", "DTEND:20261014T173000Z"),
        _event("UID:s", "RECURRENCE-ID:20261015T150000Z", "STATUS:CANCELLED", "SUMMARY:Sync",
               "DTSTART:20261015T150000Z", "DTEND:20261015T153000Z"))
    found = _between(text, _utc(2026, 10, 12), _utc(2026, 10, 20))
    assert [(o.title, o.start) for o in found] == [
        ("Sync", _utc(2026, 10, 12, 15)), ("Sync (moved)", _utc(2026, 10, 14, 17)), ("Sync", _utc(2026, 10, 16, 15))]


def test_a_cancelled_single_event_is_left_out_and_results_are_sorted():
    text = _cal(
        _event("UID:2", "SUMMARY:Late", "DTSTART:20261012T180000Z", "DTEND:20261012T190000Z"),
        _event("UID:1", "SUMMARY:Early", "DTSTART:20261012T080000Z", "DTEND:20261012T090000Z"),
        _event("UID:3", "SUMMARY:Gone", "STATUS:CANCELLED", "DTSTART:20261012T100000Z", "DTEND:20261012T110000Z"))
    assert [o.title for o in _between(text, _utc(2026, 10, 12), _utc(2026, 10, 13))] == ["Early", "Late"]


def test_a_rule_with_an_unsupported_frequency_keeps_its_first_instance():
    text = _cal(_event("UID:1", "SUMMARY:Ping", "DTSTART:20261012T080000Z", "DURATION:PT5M",
                       "RRULE:FREQ=HOURLY;COUNT=5"))
    assert len(_between(text, _utc(2026, 10, 12), _utc(2026, 10, 13))) == 1


def test_text_that_is_not_a_calendar_is_refused():
    with pytest.raises(ics.CalendarFormatError):
        ics.parse_calendar("<html><body>Sign in</body></html>")


def test_a_long_daily_series_is_bounded_but_still_reaches_the_window():
    text = _cal(_event("UID:1", "SUMMARY:Pills", "DTSTART:20000101T080000Z", "DURATION:PT5M",
                       "RRULE:FREQ=DAILY"))
    found = _between(text, _utc(2026, 10, 12), _utc(2026, 10, 14))
    assert [o.start.day for o in found] == [12, 13]

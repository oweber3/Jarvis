"""Calendar sources: a secret ICS address with a local cache, and classic Outlook through COM.

The ICS address points at a loopback fake server (``fake_ics_server.py``); Outlook is a fake MAPI
namespace handed to the source's runner, so no real calendar or Outlook profile is touched.
"""
from datetime import datetime, timedelta, timezone

import pytest

from fake_ics_server import FakeIcsServer
from jarvis.agenda import service, sources

UTC = timezone.utc
DAY = (datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC))
CALENDAR = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Seminar\r\n"
            "DTSTART:20261012T140000Z\r\nDTEND:20261012T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def server():
    fake = FakeIcsServer(CALENDAR)
    yield fake
    try:
        fake.stop()
    except Exception:
        pass


def _ics(server_or_url, tmp_path, clock, ttl=900):
    url = server_or_url.url if hasattr(server_or_url, "url") else server_or_url
    return sources.IcsSource("Work", cache_dir=tmp_path, ttl_sec=ttl, address=lambda: url, clock=clock)


def test_events_are_read_from_the_secret_address(server, tmp_path):
    result = _ics(server, tmp_path, Clock()).read(*DAY)
    assert result.ok and not result.offline_copy
    assert [(o.title, o.calendar) for o in result.occurrences] == [("Seminar", "Work")]


def test_a_recent_copy_is_reused_without_asking_the_server_again(server, tmp_path):
    clock = Clock()
    source = _ics(server, tmp_path, clock, ttl=900)
    source.read(*DAY)
    clock.now += 600
    assert source.read(*DAY).ok and server.requests == 1
    clock.now += 600
    source.read(*DAY)
    assert server.requests == 2


def test_offline_the_last_copy_answers_and_says_when_it_was_fetched(server, tmp_path):
    clock = Clock()
    _ics(server, tmp_path, clock, ttl=60).read(*DAY)
    fetched = clock.now
    server.stop()
    clock.now += 3600
    # A new source object (as after a restart) still finds the copy on disk.
    result = _ics(server, tmp_path, clock, ttl=60).read(*DAY)
    assert result.ok and result.offline_copy and result.fetched_at == pytest.approx(fetched)
    assert [o.title for o in result.occurrences] == ["Seminar"]
    assert result.error == "unreachable"


def test_offline_without_any_copy_fails_honestly(server, tmp_path):
    server.stop()
    result = _ics(server, tmp_path, Clock()).read(*DAY)
    assert not result.ok and result.error == "unreachable" and result.occurrences == []


def test_a_page_that_is_not_a_calendar_is_reported_and_not_cached(tmp_path):
    fake = FakeIcsServer("<html>Sign in to continue</html>", content_type="text/html")
    try:
        result = _ics(fake, tmp_path, Clock()).read(*DAY)
    finally:
        fake.stop()
    assert not result.ok and result.error == "not_a_calendar"
    assert list(tmp_path.iterdir()) == []


def test_an_http_error_is_reported(tmp_path):
    fake = FakeIcsServer("gone", status=404)
    try:
        result = _ics(fake, tmp_path, Clock()).read(*DAY)
    finally:
        fake.stop()
    assert not result.ok and result.error == "http_error"


def test_a_source_without_a_stored_address_says_so(tmp_path):
    result = sources.IcsSource("Work", cache_dir=tmp_path, ttl_sec=60, address=lambda: "", clock=Clock()).read(*DAY)
    assert not result.ok and result.error == "no_address"


def test_webcal_addresses_are_fetched_over_https():
    assert sources.normalise_address("webcal://calendar.example.test/a.ics") == "https://calendar.example.test/a.ics"
    with pytest.raises(ValueError):
        sources.normalise_address("file:///C:/secret.ics")


# ── Classic Outlook ────────────────────────────────────────────────────────

class FakeAppointment:
    def __init__(self, subject, start, end, all_day=False, location=""):
        self.Subject, self.Start, self.End, self.AllDayEvent, self.Location = subject, start, end, all_day, location


class FakeItems(list):
    IncludeRecurrences = False

    def Sort(self, key, descending=False):  # noqa: N802 - COM API
        self.sort(key=lambda a: a.Start, reverse=descending)

    def Restrict(self, query):  # noqa: N802 - COM API
        self.last_query = query
        assert self.IncludeRecurrences, "recurring meetings must be expanded"
        return list(self)


class FakeNamespace:
    def __init__(self, items):
        self.items = items

    def GetDefaultFolder(self, number):  # noqa: N802 - COM API
        assert number == 9  # olFolderCalendar
        return type("Folder", (), {"Items": self.items})()


def _outlook(items, state="classic"):
    return sources.OutlookCalendarSource("Outlook", runner=lambda fn, timeout: fn(FakeNamespace(items)),
                                         install_state=lambda: state)


def test_classic_outlook_appointments_are_read_as_local_times():
    # COM returns Outlook's local wall times (pywin32 may label them UTC); they are read as local.
    local_start = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    items = FakeItems([FakeAppointment("Tutorial", local_start, local_start + timedelta(hours=1), location="Room 2")])
    window = (datetime(2026, 10, 12).astimezone(), datetime(2026, 10, 13).astimezone())
    result = _outlook(items).read(*window)
    assert result.ok
    occ = result.occurrences[0]
    assert (occ.title, occ.location, occ.calendar) == ("Tutorial", "Room 2", "Outlook")
    assert occ.start == datetime(2026, 10, 12, 9).astimezone()


def test_only_the_new_outlook_is_reported_as_having_no_desktop_access():
    result = _outlook(FakeItems(), state="new_only").read(*DAY)
    assert not result.ok and result.error == "outlook_new_only"
    assert _outlook(FakeItems(), state="none").read(*DAY).error == "outlook_not_installed"


def test_an_outlook_failure_is_reported_not_raised():
    def broken(fn, timeout):
        raise sources.OutlookUnavailable("outlook_failed")
    source = sources.OutlookCalendarSource("Outlook", runner=broken, install_state=lambda: "classic")
    assert source.read(*DAY).error == "outlook_failed"


# ── Several sources together ───────────────────────────────────────────────

def test_the_agenda_merges_sources_in_time_order_and_keeps_going_when_one_fails(server, tmp_path):
    early = datetime(2026, 10, 12, 8, tzinfo=UTC)
    outlook = sources.OutlookCalendarSource(
        "Outlook", runner=lambda fn, timeout: fn(FakeNamespace(FakeItems([FakeAppointment(
            "Breakfast", early.astimezone().replace(tzinfo=UTC), (early + timedelta(minutes=30)).astimezone().replace(tzinfo=UTC))]))),
        install_state=lambda: "classic")
    missing = sources.IcsSource("Old", cache_dir=tmp_path, ttl_sec=60, address=lambda: "", clock=Clock())
    agenda = service.read_agenda([_ics(server, tmp_path, Clock()), outlook, missing], *DAY)
    assert [o.title for o in agenda.occurrences] == ["Breakfast", "Seminar"]
    assert {(s.name, s.ok, s.error) for s in agenda.statuses} == {
        ("Work", True, ""), ("Outlook", True, ""), ("Old", False, "no_address")}


def test_sources_come_from_the_configuration_with_addresses_from_the_credential_store(mock_config, tmp_path,
                                                                                     monkeypatch):
    from jarvis import credentials
    stored = {}
    monkeypatch.setattr(credentials, "get_secret", lambda key: stored.get(key, ""))
    stored["calendar_url:Work"] = "https://example.test/work.ics"
    mock_config.db_path = str(tmp_path / "jarvis.db")
    mock_config.calendar_sources = [{"name": "Work", "type": "ics"}, {"name": "Outlook", "type": "outlook"}]
    mock_config.calendar_cache_minutes = 15
    built = service.sources_from_config(mock_config)
    assert [(type(s).__name__, s.name) for s in built] == [("IcsSource", "Work"),
                                                          ("OutlookCalendarSource", "Outlook")]
    assert built[0].address() == "https://example.test/work.ics"


@pytest.mark.parametrize("name,expected", [
    ("today", (datetime(2026, 10, 12), datetime(2026, 10, 13))),
    ("tomorrow", (datetime(2026, 10, 13), datetime(2026, 10, 14))),
    ("week", (datetime(2026, 10, 12), datetime(2026, 10, 19))),
])
def test_named_ranges_are_whole_local_days(name, expected):
    start, end = service.named_range(name, datetime(2026, 10, 12, 15, 30).astimezone())
    assert (start, end) == tuple(d.astimezone() for d in expected)


def test_a_login_page_instead_of_the_calendar_falls_back_to_the_saved_copy(server, tmp_path):
    clock = Clock()
    _ics(server, tmp_path, clock, ttl=60).read(*DAY)
    server.body, server.content_type = "<html>Wi-Fi login</html>", "text/html"
    clock.now += 3600
    result = _ics(server, tmp_path, clock, ttl=60).read(*DAY)
    assert result.ok and result.offline_copy and result.error == "not_a_calendar"


def test_a_calendar_without_a_charset_is_read_as_utf8(tmp_path):
    fake = FakeIcsServer(CALENDAR.replace("Seminar", "Café Grüße"), content_type="text/calendar")
    try:
        result = _ics(fake, tmp_path, Clock()).read(*DAY)
    finally:
        fake.stop()
    assert result.occurrences[0].title == "Café Grüße"

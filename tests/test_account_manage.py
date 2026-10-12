"""Connecting, testing and removing calendars and mailboxes (``jarvis.accounts.manage``), end to end against
loopback fakes: the identity endpoints, Microsoft Graph and Google Calendar (``fake_accounts_server``), an
ICS server, a fake IMAP server and a fake Outlook namespace. Nothing contacts a real service.
"""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from fake_accounts_server import FakeAccountsServer
from fake_ics_server import FakeIcsServer
from fake_imap import FakeMailbox, make_message
from jarvis import credentials
from jarvis.accounts import http, logins, manage, oauth
from test_account_signin import MemoryStore
from test_agenda_sources import FakeItems
from test_mail_accounts import FakeOutlookItems

CALENDAR = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Seminar\r\n"
            "DTSTART:{start}\r\nDTEND:{end}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def _soon(hours):
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)


def _graph_time(moment):
    return {"dateTime": moment.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    server = FakeAccountsServer()
    memory = MemoryStore()
    monkeypatch.setattr(credentials, "_backend", memory)
    monkeypatch.setattr(oauth, "PROVIDERS", {
        "microsoft": replace(oauth.MICROSOFT, authorize_url=f"{server.base}/common/oauth2/v2.0/authorize",
                             token_url=f"{server.base}/{{tenant}}/oauth2/v2.0/token"),
        "google": replace(oauth.GOOGLE, authorize_url=f"{server.base}/google/auth",
                          token_url=f"{server.base}/google/token"),
    })
    monkeypatch.setattr(http, "GRAPH_BASE", f"{server.base}/graph/v1.0")
    monkeypatch.setattr(http, "GOOGLE_CALENDAR_BASE", f"{server.base}/calendar/v3")
    logins.forget_sessions()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db"),
                                "microsoft_oauth_client_id": "ms-client-id",
                                "google_oauth_client_id": "google-client-id",
                                "google_oauth_client_secret": server.google_secret}), encoding="utf-8")
    box = FakeMailbox(password="abcdabcdabcdabcd")
    box.add(make_message("Sam <sam@example.test>", "Hi", "Hello", datetime(2026, 10, 12)))
    store = manage.AccountStore(path, open_browser=server.browser, imap_connect=box.connect,
                                outlook_state=lambda: "none", sign_in_timeout=10)
    yield store, server, memory, path
    server.stop()
    logins.forget_sessions()


def _config(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_one_microsoft_sign_in_connects_the_calendar_and_the_inbox(env):
    store, server, memory, path = env
    server.ms_events = [{"subject": "Lecture", "start": _graph_time(_soon(2)), "end": _graph_time(_soon(3)),
                         "isAllDay": False, "location": {"displayName": "Room 4"}}]
    outcome = store.connect_oauth("microsoft", calendar=True, mail=True)

    assert outcome.ok, outcome.message
    saved = _config(path)
    assert saved["calendar_enabled"] is True and saved["email_enabled"] is True
    [calendar], [mailbox] = saved["calendar_sources"], saved["email_accounts"]
    assert calendar["type"] == "microsoft" and mailbox["type"] == "graph"
    assert calendar["login"] == mailbox["login"] and calendar["user"] == server.user_mail
    text = path.read_text(encoding="utf-8")
    assert not any(token in text for token in list(server.refresh_tokens) + list(server.access_tokens))
    assert [r.kind for r in store.rows()] == ["calendar", "email"]
    assert all(r.detail == server.user_mail for r in store.rows())


def test_unticking_email_connects_the_calendar_only_and_asks_for_less(env):
    store, server, memory, path = env
    assert store.connect_oauth("microsoft", calendar=True, mail=False).ok
    assert "Mail.Read" not in server.authorize_requests[0]["scope"]
    assert _config(path).get("email_accounts", []) == []
    assert "email_enabled" not in _config(path)


def test_the_microsoft_calendar_is_read_across_pages_with_cancelled_events_left_out(env):
    store, server, memory, path = env
    server.page_size = 2
    today = datetime.now().astimezone().date()
    server.ms_events = [
        {"subject": f"Meeting {n}", "start": _graph_time(_soon(n + 1)), "end": _graph_time(_soon(n + 2)),
         "isAllDay": False} for n in range(4)] + [
        {"subject": "Cancelled", "start": _graph_time(_soon(1)), "end": _graph_time(_soon(2)), "isCancelled": True},
        {"subject": "Holiday", "isAllDay": True,
         "start": {"dateTime": f"{today + timedelta(days=1)}T00:00:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": f"{today + timedelta(days=2)}T00:00:00.0000000", "timeZone": "UTC"}}]
    assert store.connect_oauth("microsoft", calendar=True, mail=False).ok
    from jarvis.agenda import service
    from jarvis.config import load_settings
    source = store._source(_config(path)["calendar_sources"][0])
    now = datetime.now().astimezone()
    result = service.read_agenda([source], now - timedelta(hours=1), now + timedelta(days=3))
    titles = [o.title for o in result.occurrences]
    assert titles.count("Cancelled") == 0 and {"Meeting 0", "Meeting 3", "Holiday"} <= set(titles)
    holiday = next(o for o in result.occurrences if o.title == "Holiday")
    assert holiday.all_day and holiday.start_date == today + timedelta(days=1)


def test_google_sign_in_reads_every_shown_calendar_with_recurring_events_expanded(env):
    store, server, memory, path = env
    server.page_size = 1
    tomorrow = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
    server.google_calendars = [
        {"id": server.user_mail, "primary": True, "selected": True},
        {"id": "team@group.calendar.google.com", "selected": True},
        {"id": "hidden@group.calendar.google.com", "selected": True, "hidden": True},
        {"id": "unticked@group.calendar.google.com", "selected": False},
    ]
    server.google_events = {
        server.user_mail: [{"summary": "Gym", "start": {"dateTime": _soon(2).isoformat()},
                            "end": {"dateTime": _soon(3).isoformat()}},
                           {"summary": "Gym", "start": {"dateTime": _soon(26).isoformat()},
                            "end": {"dateTime": _soon(27).isoformat()}}],
        "team@group.calendar.google.com": [{"summary": "Away day", "start": {"date": tomorrow},
                                            "end": {"date": tomorrow}},
                                           {"summary": "Dropped", "status": "cancelled",
                                            "start": {"dateTime": _soon(4).isoformat()},
                                            "end": {"dateTime": _soon(5).isoformat()}}],
        "hidden@group.calendar.google.com": [{"summary": "Secret", "start": {"dateTime": _soon(2).isoformat()},
                                              "end": {"dateTime": _soon(3).isoformat()}}],
        "unticked@group.calendar.google.com": [{"summary": "Other", "start": {"dateTime": _soon(2).isoformat()},
                                                "end": {"dateTime": _soon(3).isoformat()}}],
    }
    outcome = store.connect_oauth("google", calendar=True, mail=True)
    assert outcome.ok, outcome.message
    assert _config(path)["calendar_sources"][0]["type"] == "google"
    assert _config(path).get("email_accounts", []) == []  # Gmail is never part of the Google sign-in
    now = datetime.now().astimezone()
    result = store._source(_config(path)["calendar_sources"][0]).read(now, now + timedelta(days=3))
    assert sorted(o.title for o in result.occurrences) == ["Away day", "Gym", "Gym"]


def test_signing_in_again_replaces_the_old_sign_in_without_duplicates(env):
    store, server, memory, path = env
    assert store.connect_oauth("microsoft", calendar=True, mail=True).ok
    first_login = _config(path)["calendar_sources"][0]["login"]
    assert store.connect_oauth("microsoft", calendar=True, mail=True).ok
    saved = _config(path)
    assert [e["name"] for e in saved["calendar_sources"]] == ["Microsoft"]
    assert [e["name"] for e in saved["email_accounts"]] == ["Microsoft"]
    assert saved["calendar_sources"][0]["login"] != first_login
    assert logins.load(first_login) is None  # the replaced sign-in is gone from the credential store


def test_a_second_microsoft_account_gets_its_own_name(env):
    store, server, memory, path = env
    assert store.connect_oauth("microsoft", calendar=True, mail=False).ok
    server.user_mail = "work@example.test"
    assert store.connect_oauth("microsoft", calendar=True, mail=False).ok
    assert [e["name"] for e in _config(path)["calendar_sources"]] == ["Microsoft", "Microsoft 2"]


def test_a_failed_read_after_sign_in_keeps_nothing(env):
    store, server, memory, path = env
    server.fail_paths = {"/graph/v1.0/me/mailFolders/inbox/messages"}
    outcome = store.connect_oauth("microsoft", calendar=True, mail=True)
    assert not outcome.ok and "Nothing was saved" in outcome.message
    assert "calendar_sources" not in _config(path) and memory.items == {}


def test_a_revoked_sign_in_reports_sign_in_again_on_test_and_in_the_tools(env, mock_config):
    store, server, memory, path = env
    assert store.connect_oauth("microsoft", calendar=True, mail=True).ok
    server.revoke_all()
    logins.forget_sessions()
    outcome = store.test("calendar", "Microsoft")
    assert not outcome.ok and "sign in again" in outcome.message

    from jarvis.tools.builtin.calendar_tool import CalendarTool
    from jarvis.tools.builtin.email_tool import EmailTool
    from jarvis.tools.base import ToolContext
    saved = _config(path)
    mock_config.calendar_sources, mock_config.email_accounts = saved["calendar_sources"], saved["email_accounts"]
    context = ToolContext(None, mock_config, "", "", "", 0, lambda *a, **k: None)
    for tool in (CalendarTool(), EmailTool()):
        result = tool.run({}, context)
        assert not result.success and "Sign in again in Settings" in result.error_message


def test_a_sign_in_button_without_a_client_id_explains_itself(env):
    store, server, memory, path = env
    data = _config(path)
    data["microsoft_oauth_client_id"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    outcome = store.connect_oauth("microsoft")
    assert not outcome.ok and "not set up in this build" in outcome.message
    assert server.authorize_requests == []


def test_removing_the_calendar_keeps_the_sign_in_while_the_inbox_uses_it(env):
    store, server, memory, path = env
    assert store.connect_oauth("microsoft", calendar=True, mail=True).ok
    login_id = _config(path)["email_accounts"][0]["login"]
    assert store.remove("calendar", "Microsoft").ok
    assert _config(path).get("calendar_enabled", False) is False
    assert logins.load(login_id) is not None
    assert store.remove("email", "Microsoft").ok
    assert logins.load(login_id) is None and memory.items == {}


def test_gmail_with_an_app_password_is_tested_and_stored_hidden(env):
    store, server, memory, path = env
    assert not store.add_imap("me@gmail.test", "wrong password").ok
    assert memory.items == {} and "email_accounts" not in _config(path)
    outcome = store.add_imap("me@gmail.test", "abcd abcd abcd abcd")
    assert outcome.ok, outcome.message
    assert _config(path)["email_accounts"] == [{"name": "Gmail", "type": "imap", "host": "imap.gmail.com",
                                                "port": 993, "username": "me@gmail.test"}]
    assert "abcd" not in path.read_text(encoding="utf-8")
    assert memory.items[("jarvis", "email_password:Gmail")] == "abcdabcdabcdabcd"
    assert store.add_imap("me@gmail.test", "abcdabcdabcdabcd").ok  # adding again updates, never duplicates
    assert len(_config(path)["email_accounts"]) == 1


def test_a_calendar_link_is_tested_before_it_is_kept(env):
    store, server, memory, path = env
    start, end = _soon(2), _soon(3)
    ics = FakeIcsServer(CALENDAR.format(start=start.strftime("%Y%m%dT%H%M%SZ"), end=end.strftime("%Y%m%dT%H%M%SZ")))
    try:
        assert not store.add_ics("Uni", "ftp://example.test/cal.ics").ok
        bad = FakeIcsServer("<html>login</html>", content_type="text/html")
        assert not store.add_ics("Uni", bad.url).ok
        bad.stop()
        assert memory.items == {}
        outcome = store.add_ics("Uni", ics.url)
        assert outcome.ok and "1 event" in outcome.message
        assert _config(path)["calendar_sources"] == [{"name": "Uni", "type": "ics"}]
        assert ics.url not in path.read_text(encoding="utf-8")
        assert not store.add_ics("uni", ics.url).ok  # names are unique
    finally:
        ics.stop()


def test_outlook_is_offered_only_when_classic_outlook_is_installed(env, tmp_path):
    store, server, memory, path = env
    assert not store.add_outlook().ok and store.outlook_state() == "none"

    class Namespace:
        def GetDefaultFolder(self, number):  # noqa: N802 - COM API
            items = FakeItems() if number == 9 else FakeOutlookItems()
            return type("Folder", (), {"Items": items})()

    classic = manage.AccountStore(path, outlook_state=lambda: "classic",
                                  outlook_runner=lambda fn, timeout: fn(Namespace()))
    outcome = classic.add_outlook(calendar=True, mail=True)
    assert outcome.ok, outcome.message
    assert _config(path)["calendar_sources"] == [{"name": "Outlook", "type": "outlook"}]
    assert _config(path)["email_accounts"] == [{"name": "Outlook", "type": "outlook"}]
    assert not classic.add_outlook().ok  # already connected


def test_logs_never_carry_addresses_or_tokens(env, monkeypatch):
    store, server, memory, path = env
    lines = []
    import jarvis.accounts.logins as logins_module
    import jarvis.accounts.manage as manage_module
    import jarvis.accounts.oauth as oauth_module
    for module in (oauth_module, manage_module, logins_module, http):
        monkeypatch.setattr(module, "debug_log", lambda message, *a, **k: lines.append(message))
    assert store.connect_oauth("microsoft", calendar=True, mail=True).ok
    store.test("email", "Microsoft")
    text = "\n".join(lines)
    assert lines and server.user_mail not in text
    assert not any(token in text for token in list(server.refresh_tokens) + list(server.access_tokens))


def test_an_unexpected_failure_after_sign_in_leaves_no_sign_in_behind(env, monkeypatch):
    store, server, memory, path = env

    def broken(*args, **kwargs):
        raise AttributeError("malformed reply")

    monkeypatch.setattr(store, "_check_mail", broken)
    with pytest.raises(AttributeError):
        store.connect_oauth("microsoft", calendar=True, mail=True)
    assert memory.items == {} and "calendar_sources" not in _config(path)

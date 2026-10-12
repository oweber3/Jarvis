"""The guided setup commands: ``python -m jarvis.agenda`` and ``python -m jarvis.mail``.

Answers are scripted, the config file is a temporary one, secrets go to an in-memory credential
store, and every service is a fake (loopback ICS server, fake IMAP, fake Microsoft endpoints).
"""
import json

import pytest

from fake_ics_server import FakeIcsServer
from fake_imap import FakeMailbox, make_message
from jarvis import credentials
from jarvis.agenda import cli as agenda_cli
from jarvis.mail import cli as mail_cli

CALENDAR = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Seminar\r\n"
            "DTSTART:20261012T140000Z\r\nDTEND:20261012T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        del self.items[(service, username)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(credentials, "_backend", store)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db")}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return path, store


def _io(module, answers, secrets=(), **extra):
    answers, secrets, lines = list(answers), list(secrets), []
    io = module.Io(ask=lambda prompt: answers.pop(0), ask_secret=lambda prompt: secrets.pop(0),
                   out=lines.append, **extra)
    return io, lines


def _config(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_adding_a_google_calendar_stores_the_address_secretly_and_turns_the_calendar_on(env):
    path, store = env
    server = FakeIcsServer(CALENDAR)
    try:
        io, lines = _io(agenda_cli, ["1", "Uni"], [server.url])
        assert agenda_cli.main(["setup"], io) == 0
    finally:
        server.stop()
    saved = _config(path)
    assert saved["calendar_enabled"] is True
    assert saved["calendar_sources"] == [{"name": "Uni", "type": "ics"}]
    assert server.url not in path.read_text(encoding="utf-8")
    assert server.url not in "\n".join(lines)
    assert store.items[("jarvis", "calendar_url:Uni")] == server.url
    assert any("✅" in line for line in lines)


def test_a_bad_address_saves_nothing(env):
    path, store = env
    server = FakeIcsServer("<html>nope</html>", content_type="text/html")
    try:
        io, lines = _io(agenda_cli, ["2", "Work"], [server.url])
        assert agenda_cli.main(["setup"], io) == 1
    finally:
        server.stop()
    assert "calendar_sources" not in _config(path) and store.items == {}
    assert any("❌" in line for line in lines)


def test_listing_testing_and_removing_a_calendar(env):
    path, store = env
    server = FakeIcsServer(CALENDAR)
    try:
        agenda_cli.main(["setup"], _io(agenda_cli, ["3", "Club"], [server.url])[0])
        io, lines = _io(agenda_cli, [])
        assert agenda_cli.main(["list"], io) == 0 and any("Club" in line and "stored" in line for line in lines)
        assert agenda_cli.main(["test", "Club"], _io(agenda_cli, [])[0]) == 0
    finally:
        server.stop()
    io, lines = _io(agenda_cli, [])
    assert agenda_cli.main(["remove", "club"], io) == 0
    assert _config(path).get("calendar_sources", []) == [] and store.items == {}
    assert _config(path).get("calendar_enabled", False) is False


def test_adding_gmail_with_an_app_password(env):
    path, store = env
    box = FakeMailbox(password="abcdabcdabcdabcd")
    box.add(make_message("Sam <sam@example.test>", "Hi", "Hello", __import__("datetime").datetime(2026, 10, 12)))
    io, lines = _io(mail_cli, ["1", "", "me@gmail.test"], ["abcd abcd abcd abcd"], connect=box.connect)
    assert mail_cli.main(["setup"], io) == 0
    saved = _config(path)
    assert saved["email_enabled"] is True
    assert saved["email_accounts"] == [{"name": "Gmail", "type": "imap", "host": "imap.gmail.com", "port": 993,
                                        "username": "me@gmail.test"}]
    assert "abcd" not in path.read_text(encoding="utf-8") and "abcd" not in "\n".join(lines)
    assert store.items[("jarvis", "email_password:Gmail")] == "abcdabcdabcdabcd"
    assert any("myaccount.google.com/apppasswords" in line for line in lines)


def test_a_refused_gmail_password_is_not_kept(env):
    path, store = env
    box = FakeMailbox(password="right")
    io, lines = _io(mail_cli, ["1", "", "me@gmail.test"], ["wrong"], connect=box.connect)
    assert mail_cli.main(["setup"], io) == 1
    assert store.items == {} and "email_accounts" not in _config(path)
    assert any("app password" in line for line in lines)


def test_adding_a_microsoft_account_signs_in_with_a_device_code(env):
    from test_mail_accounts import FakeGraph
    path, store = env
    fake = FakeGraph()
    io, lines = _io(mail_cli, ["4", "", "client-abc", "y"], transport=fake, sleep=lambda s: None)
    assert mail_cli.main(["setup"], io) == 0
    assert _config(path)["email_accounts"] == [{"name": "Outlook.com", "type": "graph", "client_id": "client-abc",
                                                "tenant": "consumers"}]
    assert any("ABCD-EFGH" in line for line in lines)
    assert store.items[("jarvis", "email_graph_token:Outlook.com")] == "refresh-2"  # rotated by the test read
    io, lines = _io(mail_cli, [])
    mail_cli.main(["list"], io)
    assert any("signed in" in line for line in lines)
    assert mail_cli.main(["remove", "Outlook.com"], _io(mail_cli, [])[0]) == 0 and store.items == {}


def test_outlook_when_only_the_new_outlook_is_installed_explains_why(env, monkeypatch):
    from jarvis.platform.windows import outlook
    monkeypatch.setattr(outlook, "install_state", lambda: "new_only")
    io, lines = _io(mail_cli, ["3", ""])
    assert mail_cli.main(["setup"], io) == 1
    assert any("new Outlook" in line for line in lines)

"""Read-only email accounts: IMAP (Gmail with an app password), classic Outlook through COM and
Microsoft Graph. Every account here is a fake; nothing contacts a real mail service.
"""
from datetime import datetime, timedelta, timezone

import pytest

from fake_imap import FakeMailbox, make_message
from jarvis.mail import graph, imap, outlook, service

UTC = timezone.utc
T0 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


# ── IMAP ───────────────────────────────────────────────────────────────────

@pytest.fixture
def box():
    mailbox = FakeMailbox()
    mailbox.add(make_message("Sam Lee <sam@example.test>", "Lunch?", "Are you free at noon?", T0), seen=True)
    mailbox.add(make_message("Prof. Ada King <ada@uni.example.test>", "Coursework feedback",
                             "Your essay was strong.\n\nSee the attached notes for details.", T0 + timedelta(hours=1)))
    mailbox.add(make_message("Shop <news@shop.example.test>", "Sale", None,
                             T0 + timedelta(hours=2), html="<p>Big <b>sale</b> today</p>"))
    return mailbox


def _imap(box, password="app-password"):
    return imap.ImapAccount("Gmail", host="imap.gmail.com", port=993, username="me@gmail.test",
                            password=lambda: password, connect=box.connect)


def test_the_newest_messages_come_first_with_sender_subject_time_and_a_snippet(box):
    result = _imap(box).list_messages(limit=2)
    assert result.ok
    assert [(m.sender_name, m.subject, m.unread) for m in result.messages] == [
        ("Shop", "Sale", True), ("Prof. Ada King", "Coursework feedback", True)]
    assert result.messages[1].received == T0 + timedelta(hours=1)
    assert result.messages[1].snippet.startswith("Your essay was strong.")
    assert result.messages[1].sender_address == "ada@uni.example.test"


def test_unread_only_and_sender_filters(box):
    assert [m.subject for m in _imap(box).list_messages(unread_only=True).messages] == ["Sale", "Coursework feedback"]
    assert [m.subject for m in _imap(box).list_messages(sender="sam").messages] == ["Lunch?"]


def test_a_sender_with_non_ascii_letters_is_searched_as_utf8(box):
    box.add(make_message("Zoë Brontë <zoe@example.test>", "Hallo", "Hi", T0 + timedelta(hours=3)))
    assert [m.subject for m in _imap(box).list_messages(sender="Zoë").messages] == ["Hallo"]


def test_reading_a_message_returns_its_text_and_never_marks_it_read(box):
    account = _imap(box)
    listed = account.list_messages(limit=1).messages[0]
    message = account.read_message(listed.native_id)
    assert message.body == "Big sale today"
    assert box.selected_readonly is True
    assert box.writes == []
    assert not box.messages[2]["seen"]


def test_a_wrong_password_and_an_unreachable_server_are_reported(box):
    assert _imap(box, password="wrong").list_messages().error == "auth_failed"
    box.fail_connect = True
    assert _imap(box).list_messages().error == "unreachable"
    assert imap.ImapAccount("Gmail", host="imap.gmail.com", port=993, username="me@gmail.test",
                            password=lambda: "", connect=box.connect).list_messages().error == "no_password"


# ── Classic Outlook ────────────────────────────────────────────────────────

class FakeMail:
    Class = 43  # olMail

    def __init__(self, entry_id, sender, address, subject, body, received, unread):
        self.EntryID, self.SenderName, self.SenderEmailAddress = entry_id, sender, address
        self.Subject, self.Body, self.ReceivedTime, self.UnRead = subject, body, received, unread


class FakeOutlookItems(list):
    def Sort(self, key, descending=False):  # noqa: N802 - COM API
        self.sort(key=lambda m: m.ReceivedTime, reverse=descending)

    def Restrict(self, query):  # noqa: N802 - COM API
        assert query == "[UnRead] = True"
        return FakeOutlookItems(m for m in self if m.UnRead)


class FakeOutlookNamespace:
    def __init__(self, mails):
        self.mails = mails

    def GetDefaultFolder(self, number):  # noqa: N802 - COM API
        assert number == 6  # olFolderInbox
        return type("Folder", (), {"Items": FakeOutlookItems(self.mails)})()

    def GetItemFromID(self, entry_id):  # noqa: N802 - COM API
        return next(m for m in self.mails if m.EntryID == entry_id)


def _outlook(mails, state="classic"):
    return outlook.OutlookMailAccount("Outlook", runner=lambda fn, timeout: fn(FakeOutlookNamespace(mails)),
                                      install_state=lambda: state)


OUTLOOK_MAILS = [
    FakeMail("E1", "Sam Lee", "sam@example.test", "Notes", "Here are the notes.", T0, False),
    FakeMail("E2", "IT Desk", "it@example.test", "Password expiry", "Change it soon.", T0 + timedelta(hours=1), True),
]


def test_classic_outlook_inbox_lists_newest_first_and_reads_by_id():
    account = _outlook(OUTLOOK_MAILS)
    listed = account.list_messages().messages
    assert [(m.sender_name, m.subject, m.unread) for m in listed] == [("IT Desk", "Password expiry", True),
                                                                     ("Sam Lee", "Notes", False)]
    assert [m.subject for m in account.list_messages(unread_only=True).messages] == ["Password expiry"]
    assert [m.subject for m in account.list_messages(sender="sam lee").messages] == ["Notes"]
    assert account.read_message(listed[1].native_id).body == "Here are the notes."


def test_only_the_new_outlook_is_reported():
    assert _outlook([], state="new_only").list_messages().error == "outlook_new_only"


# ── Microsoft Graph ────────────────────────────────────────────────────────

class FakeGraph:
    """Microsoft Graph as a transport: (method, url, params, headers). Accepts the access tokens in ``valid``."""

    def __init__(self):
        self.calls = []
        self.valid = {"access-1"}

    def get(self, url, params, headers):
        self.calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        if headers.get("Authorization", "").removeprefix("Bearer ") not in self.valid:
            return 401, {}
        if url.endswith("/me/mailFolders/inbox/messages"):
            items = [
                {"id": "AAA", "subject": "Ticket update", "receivedDateTime": "2026-10-12T09:00:00Z", "isRead": False,
                 "bodyPreview": "Your ticket was answered", "from": {"emailAddress": {"name": "Help", "address": "help@x.test"}}},
                {"id": "BBB", "subject": "Hello", "receivedDateTime": "2026-10-12T07:00:00Z", "isRead": True,
                 "bodyPreview": "Hi there", "from": {"emailAddress": {"name": "Sam Lee", "address": "sam@x.test"}}},
            ]
            return 200, {"value": items}
        if url.endswith("/me/messages/BBB"):
            return 200, {"id": "BBB", "subject": "Hello", "receivedDateTime": "2026-10-12T07:00:00Z", "isRead": True,
                         "from": {"emailAddress": {"name": "Sam Lee", "address": "sam@x.test"}},
                         "body": {"contentType": "text", "content": "Hi there, long text."}}
        return 404, {}


class FakeSession:
    """A sign-in's session: hands out ``tokens`` in turn, or fails like a revoked sign-in."""

    def __init__(self, *tokens, error=None):
        self.tokens, self.error, self.invalidated = list(tokens), error, 0

    def access_token(self):
        from jarvis.accounts.logins import LoginError
        if self.error or not self.tokens:
            raise LoginError(self.error or "not_signed_in")
        return self.tokens[0] if len(self.tokens) == 1 else self.tokens.pop(0)

    def invalidate(self):
        self.invalidated += 1


def _graph(fake, session):
    return graph.GraphAccount("Outlook.com", session=session, transport=fake, base="https://graph.test/v1.0")


def test_graph_lists_the_inbox_newest_first():
    result = _graph(FakeGraph(), FakeSession("access-1")).list_messages()
    assert [(m.sender_name, m.subject, m.unread) for m in result.messages] == [("Help", "Ticket update", True),
                                                                              ("Sam Lee", "Hello", False)]


def test_graph_unread_and_sender_filters_and_reading_a_message():
    fake = FakeGraph()
    account = _graph(fake, FakeSession("access-1"))
    account.list_messages(unread_only=True)
    params = [c for c in fake.calls if c[0] == "GET"][-1][2]
    assert "isRead eq false" in params["$filter"]
    assert [m.subject for m in account.list_messages(sender="sam").messages] == ["Hello"]
    assert account.read_message("BBB").body == "Hi there, long text."


def test_graph_without_a_sign_in_or_with_a_revoked_one_says_to_sign_in_again():
    fake = FakeGraph()
    assert _graph(fake, FakeSession(error="not_signed_in")).list_messages().error == "not_signed_in"
    session = FakeSession("stale-1", "stale-2")
    assert _graph(fake, session).list_messages().error == "not_signed_in"
    assert session.invalidated == 2  # refused once, refreshed, refused again


def test_a_refused_access_token_is_refreshed_once_before_giving_up():
    session = FakeSession("expired", "access-1")
    assert _graph(FakeGraph(), session).list_messages().ok


def test_graph_unreachable_is_not_a_sign_in_problem():
    class Offline:
        def get(self, url, params, headers):
            return 0, {}
    assert _graph(Offline(), FakeSession("access-1")).list_messages().error == "unreachable"


# ── Several accounts ───────────────────────────────────────────────────────

def test_accounts_merge_newest_first_with_short_ids_that_read_back(box):
    accounts = [_imap(box), _outlook(OUTLOOK_MAILS)]
    listing = service.list_messages(accounts, limit=10)
    received = [m.received for m in listing.messages]
    assert received == sorted(received, reverse=True)
    assert {m.subject for m in listing.messages} == {"Sale", "Coursework feedback", "Lunch?", "Password expiry",
                                                     "Notes"}
    handle = next(m.handle for m in listing.messages if m.subject == "Notes")
    assert len(handle) <= 10
    assert service.read_message(accounts, handle).body == "Here are the notes."


def test_reading_the_latest_from_a_sender_without_an_id(box):
    message = service.read_latest(  # "read me the latest email from my professor" after the model picks "Ada"
        [_imap(box), _outlook(OUTLOOK_MAILS)], sender="Ada")
    assert message.subject == "Coursework feedback" and "essay was strong" in message.body


def test_an_unknown_id_fails_honestly(box):
    assert service.read_message([_imap(box)], "nope1234") is None


def test_accounts_come_from_the_configuration(mock_config, monkeypatch):
    from jarvis import credentials
    monkeypatch.setattr(credentials, "get_secret", lambda key: "pw" if key == "email_password:Gmail" else "")
    mock_config.email_accounts = [
        {"name": "Gmail", "type": "imap", "host": "imap.gmail.com", "port": 993, "username": "me@gmail.test"},
        {"name": "Outlook", "type": "outlook"},
        {"name": "Outlook.com", "type": "graph", "login": "microsoft-1a2b3c4d"},
    ]
    built = service.accounts_from_config(mock_config)
    assert [(type(a).__name__, a.name) for a in built] == [
        ("ImapAccount", "Gmail"), ("OutlookMailAccount", "Outlook"), ("GraphAccount", "Outlook.com")]


def test_the_real_imap_client_reads_without_changing_the_mailbox(box):
    """End to end through ``imaplib`` over a loopback IMAP server speaking the wire protocol."""
    import imaplib
    from fake_imap import ImapWireServer
    box.add(make_message("Zoë Brontë <zoe@example.test>", "Hallo", "Grüße aus Köln", T0 + timedelta(hours=3)))
    server = ImapWireServer(box)
    try:
        account = imap.ImapAccount("Gmail", host="127.0.0.1", port=server.port, username="me@gmail.test",
                                   password=lambda: "app-password",
                                   connect=lambda host, port, timeout: imaplib.IMAP4(host, port, timeout=timeout))
        listed = account.list_messages(limit=2)
        assert [m.subject for m in listed.messages] == ["Hallo", "Sale"]
        assert [m.subject for m in account.list_messages(sender="Zoë").messages] == ["Hallo"]
        assert account.read_message(listed.messages[0].native_id).body == "Grüße aus Köln"
    finally:
        server.close()
    commands = [line.split(" ", 1)[1].split(" ")[0].upper() for line in server.lines]
    assert set(commands) <= {"CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT"}
    assert all("BODY.PEEK" in line for line in server.lines if " FETCH " in line)
    assert box.writes == [] and box.selected_readonly is True


def test_the_default_imap_connection_verifies_the_server_certificate(monkeypatch):
    import ssl
    seen = {}

    class Capture:
        def __init__(self, host, port, **kwargs):
            seen.update(kwargs)
            raise OSError("stop here")

    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", Capture)
    imap.ImapAccount("Gmail", host="imap.gmail.com", port=993, username="u", password=lambda: "pw").list_messages()
    context = seen.get("ssl_context")
    assert context is not None and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_imap_failures_after_sign_in_are_results_not_exceptions(box, monkeypatch):
    import imaplib
    from fake_imap import FakeImapConnection

    def broken_select(self, mailbox="INBOX", readonly=False):
        raise imaplib.IMAP4.abort("connection dropped")

    monkeypatch.setattr(FakeImapConnection, "select", broken_select)
    assert _imap(box).list_messages().error == "server_error"
    assert _imap(box).read_message("101") is None
    assert _imap(box, password="pässword").list_messages().error in ("auth_failed", "server_error")


def test_a_sign_ins_calendar_and_inbox_share_one_session(mock_config):
    from jarvis.accounts import logins
    from jarvis.agenda import service as agenda_service
    logins.forget_sessions()
    mock_config.email_accounts = [{"name": "Outlook.com", "type": "graph", "login": "microsoft-1a2b3c4d"}]
    mock_config.calendar_sources = [{"name": "Outlook.com", "type": "microsoft", "login": "microsoft-1a2b3c4d"}]
    mailbox = service.accounts_from_config(mock_config)[0]
    calendar = agenda_service.sources_from_config(mock_config)[0]
    assert mailbox._session is calendar._session is logins.session_for("microsoft-1a2b3c4d")
    logins.forget_sessions()


def test_a_forbidden_or_rate_limited_read_is_a_server_error_and_keeps_the_sign_in():
    class Forbidden:
        def get(self, url, params, headers):
            return 403, {"error": {"code": "ErrorAccessDenied"}}
    session = FakeSession("access-1")
    assert _graph(Forbidden(), session).list_messages().error == "server_error"
    assert session.invalidated == 0

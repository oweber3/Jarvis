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
    """Microsoft identity platform and Graph endpoints as a transport: (method, url, data/params)."""

    def __init__(self):
        self.calls = []
        self.refresh_token = "refresh-1"
        self.device_polls = 0

    def post(self, url, data):
        self.calls.append(("POST", url, dict(data)))
        if url.endswith("/devicecode"):
            return 200, {"device_code": "dev-123", "user_code": "ABCD-EFGH", "interval": 0, "expires_in": 900,
                         "verification_uri": "https://microsoft.com/devicelogin",
                         "message": "To sign in, open https://microsoft.com/devicelogin and enter ABCD-EFGH."}
        if url.endswith("/token") and data.get("grant_type") == graph.DEVICE_GRANT:
            self.device_polls += 1
            if self.device_polls < 2:
                return 400, {"error": "authorization_pending"}
            return 200, {"access_token": "access-1", "refresh_token": self.refresh_token, "expires_in": 3600}
        if url.endswith("/token") and data.get("grant_type") == "refresh_token":
            if data.get("refresh_token") != self.refresh_token:
                return 400, {"error": "invalid_grant"}
            self.refresh_token = "refresh-2"
            return 200, {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}
        return 404, {}

    def get(self, url, params, headers):
        self.calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        if headers.get("Authorization") not in ("Bearer access-1", "Bearer access-2"):
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


class TokenStore(dict):
    def get_token(self):
        return self.get("token", "")

    def set_token(self, value):
        self["token"] = value
        return True


def _graph(fake, store):
    return graph.GraphAccount("Outlook.com", client_id="client-abc", tenant="consumers", transport=fake,
                              get_token=store.get_token, set_token=store.set_token)


def test_device_code_sign_in_stores_the_refresh_token_only_in_the_store():
    fake, store = FakeGraph(), TokenStore()
    prompts = []
    assert graph.device_sign_in("client-abc", "consumers", set_token=store.set_token, show=prompts.append,
                                transport=fake, sleep=lambda s: None)
    assert store["token"] == "refresh-1"
    assert "ABCD-EFGH" in prompts[0]
    scope = fake.calls[0][2]["scope"]
    assert "Mail.Read" in scope and "offline_access" in scope and "Send" not in scope


def test_graph_lists_the_inbox_and_keeps_the_rotated_refresh_token():
    fake, store = FakeGraph(), TokenStore(token="refresh-1")
    result = _graph(fake, store).list_messages()
    assert [(m.sender_name, m.subject, m.unread) for m in result.messages] == [("Help", "Ticket update", True),
                                                                              ("Sam Lee", "Hello", False)]
    assert store["token"] == "refresh-2"


def test_graph_unread_and_sender_filters_and_reading_a_message():
    fake, store = FakeGraph(), TokenStore(token="refresh-1")
    account = _graph(fake, store)
    account.list_messages(unread_only=True)
    params = [c for c in fake.calls if c[0] == "GET"][-1][2]
    assert "isRead eq false" in params["$filter"]
    assert [m.subject for m in account.list_messages(sender="sam").messages] == ["Hello"]
    assert account.read_message("BBB").body == "Hi there, long text."


def test_graph_without_a_sign_in_or_with_an_expired_one_says_to_sign_in_again():
    fake = FakeGraph()
    assert _graph(fake, TokenStore()).list_messages().error == "not_signed_in"
    assert _graph(fake, TokenStore(token="stale")).list_messages().error == "not_signed_in"


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
        {"name": "Outlook.com", "type": "graph", "client_id": "abc", "tenant": "consumers"},
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

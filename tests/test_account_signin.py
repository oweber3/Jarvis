"""Browser sign-in (OAuth 2.0 authorisation code with PKCE and a loopback redirect) and stored sign-ins.

Every endpoint is the loopback fake in ``fake_accounts_server``; nothing contacts Microsoft or Google.
"""
import base64
import hashlib
import json
import socket
import threading
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from fake_accounts_server import FakeAccountsServer
from jarvis import credentials
from jarvis.accounts import logins, oauth


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
def server():
    fake = FakeAccountsServer()
    yield fake
    fake.stop()


@pytest.fixture
def store(monkeypatch):
    memory = MemoryStore()
    monkeypatch.setattr(credentials, "_backend", memory)
    logins.forget_sessions()
    yield memory
    logins.forget_sessions()


def _providers(server):
    return {
        "microsoft": replace(oauth.MICROSOFT, authorize_url=f"{server.base}/common/oauth2/v2.0/authorize",
                             token_url=f"{server.base}/{{tenant}}/oauth2/v2.0/token"),
        "google": replace(oauth.GOOGLE, authorize_url=f"{server.base}/google/auth",
                          token_url=f"{server.base}/google/token"),
    }


MS_CLIENT = oauth.Client("ms-client-id")


def _google_client(server):
    return oauth.Client("google-client-id", server.google_secret)


def _port_open(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_the_code_challenge_is_the_s256_hash_of_a_fresh_verifier():
    verifier, challenge = oauth.pkce_pair()
    again, _ = oauth.pkce_pair()
    assert 43 <= len(verifier) <= 128 and verifier != again
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_microsoft_sign_in_asks_only_for_read_scopes_and_proves_the_verifier(server):
    provider = _providers(server)["microsoft"]
    scopes = oauth.scopes_for(provider, calendar=True, mail=True)
    tokens = oauth.authorize(provider, MS_CLIENT, scopes, open_browser=server.browser, timeout=10)

    assert tokens.refresh_token in server.refresh_tokens and tokens.access_token in server.access_tokens
    asked = server.authorize_requests[0]
    assert asked["code_challenge_method"] == "S256" and asked["response_type"] == "code"
    assert set(asked["scope"].split()) == {"offline_access", "User.Read", "Calendars.Read", "Mail.Read"}
    assert len(asked["state"]) >= 32
    redirect = urlsplit(asked["redirect_uri"])
    assert redirect.scheme == "http" and redirect.hostname == "localhost" and redirect.port
    exchange = server.token_requests[0]
    assert exchange["grant_type"] == "authorization_code" and exchange["code_verifier"]
    assert "client_secret" not in exchange


def test_unticking_mail_leaves_mail_out_of_the_microsoft_scopes():
    scopes = oauth.scopes_for(oauth.MICROSOFT, calendar=True, mail=False)
    assert "Mail.Read" not in scopes.split() and "Calendars.Read" in scopes.split()
    assert all(not s.endswith(("Write", "Send", "ReadWrite")) for s in oauth.scopes_for(oauth.MICROSOFT, True, True).split())


def test_google_sign_in_asks_for_the_read_only_calendar_only_with_offline_access(server):
    provider = _providers(server)["google"]
    scopes = oauth.scopes_for(provider, calendar=True, mail=True)
    tokens = oauth.authorize(provider, _google_client(server), scopes, open_browser=server.browser, timeout=10)

    assert tokens.refresh_token
    asked = server.authorize_requests[0]
    assert asked["scope"] == "https://www.googleapis.com/auth/calendar.readonly"
    assert asked["access_type"] == "offline"
    assert urlsplit(asked["redirect_uri"]).hostname == "127.0.0.1"
    assert server.token_requests[0]["client_secret"] == server.google_secret


def test_a_forged_redirect_is_refused_and_the_real_one_still_signs_in(server):
    provider = _providers(server)["microsoft"]
    seen = {}

    def browser(url):
        server.browser(url, state="<script>alert(1)</script>", wait=True)
        seen["forged"] = dict(server.redirect_result)
        return server.browser(url)

    tokens = oauth.authorize(provider, MS_CLIENT, "offline_access User.Read", open_browser=browser, timeout=10)
    assert tokens.refresh_token
    assert seen["forged"]["status"] == 400 and "<script>" not in seen["forged"]["body"]
    assert len(server.token_requests) == 1  # only the real code was exchanged


def test_only_forged_redirects_end_as_a_state_mismatch_at_the_time_limit(server):
    provider = _providers(server)["microsoft"]
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(provider, MS_CLIENT, "offline_access User.Read",
                        open_browser=lambda url: server.browser(url, state="forged", wait=True), timeout=1.5)
    assert failure.value.code == "state_mismatch"
    assert server.token_requests == []


def test_a_silent_connection_cannot_hold_up_cancel(server):
    cancel = threading.Event()
    held = []

    def browser(url):
        port = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0]).port
        held.append(socket.create_connection(("127.0.0.1", port)))  # connects and never sends a request
        threading.Timer(0.3, cancel.set).start()
        return True

    started = time.monotonic()
    try:
        with pytest.raises(oauth.OAuthError) as failure:
            oauth.authorize(_providers(server)["microsoft"], MS_CLIENT, "offline_access", open_browser=browser,
                            cancel=cancel, timeout=60)
    finally:
        for connection in held:
            connection.close()
    assert failure.value.code == "cancelled" and time.monotonic() - started < 3


def test_pressing_cancel_on_the_consent_page_is_reported_as_declined(server):
    provider = _providers(server)["google"]
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(provider, _google_client(server), oauth.GOOGLE_CALENDAR_SCOPE,
                        open_browser=lambda url: server.browser(url, deny=True), timeout=10)
    assert failure.value.code == "denied"


def test_a_scope_the_user_unticked_on_the_consent_page_fails_the_sign_in(server):
    server.granted_scope = "offline_access User.Read Mail.Read"
    provider = _providers(server)["microsoft"]
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(provider, MS_CLIENT, oauth.scopes_for(provider, calendar=True, mail=True),
                        open_browser=server.browser, timeout=10)
    assert failure.value.code == "scope_missing"


def test_cancel_stops_waiting_and_closes_the_port(server):
    cancel = threading.Event()
    seen = {}

    def browser(url):
        seen["port"] = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0]).port
        threading.Timer(0.3, cancel.set).start()
        return True

    started = time.monotonic()
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(_providers(server)["microsoft"], MS_CLIENT, "offline_access", open_browser=browser,
                        cancel=cancel, timeout=60)
    assert failure.value.code == "cancelled"
    assert time.monotonic() - started < 5
    assert not _port_open(seen["port"])


def test_nobody_signing_in_times_out(server):
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(_providers(server)["microsoft"], MS_CLIENT, "offline_access",
                        open_browser=lambda url: True, timeout=0.6)
    assert failure.value.code == "timeout"


def test_the_loopback_receiver_listens_on_127_0_0_1_only_and_ignores_other_paths(server):
    receiver = oauth.LoopbackReceiver("127.0.0.1", "the-state")
    try:
        assert receiver.address[0] == "127.0.0.1"
        assert receiver.redirect_uri == f"http://127.0.0.1:{receiver.address[1]}"
        result = {}
        waiter = threading.Thread(target=lambda: result.update(
            code=receiver.wait("the-state", timeout=10, cancel=threading.Event())))
        waiter.start()
        import urllib.error
        import urllib.request
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{receiver.redirect_uri}/favicon.ico", timeout=5)
        assert missing.value.code == 404
        with urllib.request.urlopen(f"{receiver.redirect_uri}/?code=abc&state=the-state", timeout=5) as page:
            assert page.status == 200
            assert page.headers["Cache-Control"] == "no-store"
        waiter.join(5)
        assert result["code"] == "abc"
    finally:
        receiver.close()
    assert not _port_open(receiver.address[1])


def test_a_browser_that_cannot_be_opened_is_reported(server):
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(_providers(server)["microsoft"], MS_CLIENT, "offline_access",
                        open_browser=lambda url: False, timeout=5)
    assert failure.value.code == "browser"


def test_an_unconfigured_client_cannot_start_a_sign_in():
    with pytest.raises(oauth.OAuthError) as failure:
        oauth.authorize(oauth.MICROSOFT, oauth.Client(""), "offline_access", open_browser=lambda url: True)
    assert failure.value.code == "not_configured"


def test_client_ids_come_from_config_over_the_built_in_defaults():
    class Cfg:
        microsoft_oauth_client_id = "from-config"
        google_oauth_client_id = ""
        google_oauth_client_secret = ""
    assert oauth.client_for("microsoft", Cfg()).client_id == "from-config"
    assert not oauth.client_for("google", Cfg()).configured


# ── Stored sign-ins ───────────────────────────────────────────────────────

def _signed_in(server, store, monkeypatch, provider_key="microsoft"):
    providers = _providers(server)
    monkeypatch.setattr(oauth, "PROVIDERS", providers)
    client = MS_CLIENT if provider_key == "microsoft" else _google_client(server)
    scopes = oauth.scopes_for(providers[provider_key], calendar=True, mail=True)
    tokens = oauth.authorize(providers[provider_key], client, scopes, open_browser=server.browser, timeout=10)
    login_id = logins.new_login_id(provider_key)
    assert logins.save(login_id, logins.LoginRecord.from_tokens(provider_key, client, tokens))
    return login_id, tokens


def test_the_refresh_token_is_kept_only_in_the_credential_store(server, store, monkeypatch):
    login_id, tokens = _signed_in(server, store, monkeypatch)
    stored = credentials.get_secret(credentials.account_key("oauth_login", login_id))
    assert tokens.refresh_token in stored
    record = json.loads(stored)
    assert record["provider"] == "microsoft" and record["client_id"] == "ms-client-id"


def test_access_tokens_are_refreshed_reused_and_a_rotated_refresh_token_is_saved(server, store, monkeypatch):
    login_id, tokens = _signed_in(server, store, monkeypatch)
    session = logins.session_for(login_id)
    first = session.access_token()
    assert session.access_token() == first  # reused while valid
    assert logins.load(login_id).refresh_token != tokens.refresh_token  # rotated and saved
    assert logins.session_for(login_id) is session  # calendar and mail share one session


def test_a_revoked_sign_in_says_to_sign_in_again(server, store, monkeypatch):
    login_id, _ = _signed_in(server, store, monkeypatch)
    server.revoke_all()
    with pytest.raises(logins.LoginError) as failure:
        logins.session_for(login_id).access_token()
    assert failure.value.code == "not_signed_in"


def test_an_unreachable_token_endpoint_is_not_mistaken_for_a_revoked_sign_in(server, store, monkeypatch):
    login_id, _ = _signed_in(server, store, monkeypatch)
    server.stop()
    with pytest.raises(logins.LoginError) as failure:
        logins.session_for(login_id).access_token()
    assert failure.value.code == "unreachable"
    assert logins.load(login_id) is not None  # the sign-in is kept for when the network is back


def test_a_missing_sign_in_says_to_sign_in_again(store):
    with pytest.raises(logins.LoginError) as failure:
        logins.session_for("microsoft-00000000").access_token()
    assert failure.value.code == "not_signed_in"


def test_google_refreshes_with_its_client_secret_and_keeps_an_unrotated_token(server, store, monkeypatch):
    server.rotate = False
    login_id, tokens = _signed_in(server, store, monkeypatch, "google")
    assert logins.session_for(login_id).access_token()
    assert server.token_requests[-1]["client_secret"] == server.google_secret
    assert logins.load(login_id).refresh_token == tokens.refresh_token


def test_a_sign_in_removed_during_a_refresh_does_not_come_back(server, store, monkeypatch):
    login_id, _ = _signed_in(server, store, monkeypatch)
    real_refresh = oauth.refresh

    def refresh_while_removed(*args, **kwargs):
        tokens = real_refresh(*args, **kwargs)
        credentials.delete_secret(credentials.account_key("oauth_login", login_id))  # Settings removed it meanwhile
        return tokens

    monkeypatch.setattr(oauth, "refresh", refresh_while_removed)
    assert logins.session_for(login_id).access_token()
    assert logins.load(login_id) is None

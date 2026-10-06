"""The phone access HTTP server, driven over real loopback HTTP with a fake daemon behind it."""
import http.client
import json

import pytest

from fake_remote_backend import FakeBackend
from jarvis.remote.hub import RemoteHub
from jarvis.remote.pairing import DeviceStore
from jarvis.remote.server import MAX_BODY_BYTES, MAX_TEXT_CHARS, RemoteServer, ServerSettings


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path)


def make_server(backend, store, **overrides):
    settings = ServerSettings(
        host="127.0.0.1", port=0, quick_actions=overrides.pop("quick_actions", ["Pause the music"]),
        allow_confirm=overrides.pop("allow_confirm", True), poll_timeout_sec=overrides.pop("poll_timeout_sec", 0.3))
    hub = RemoteHub(backend, sync_interval_sec=0.05)
    server = RemoteServer(hub, store, settings, **overrides)
    server.start()
    return server


@pytest.fixture
def server(backend, store):
    server = make_server(backend, store)
    yield server
    server.stop()


class Client:
    def __init__(self, server, token=None):
        self.port = server.port
        self.token = token

    def request(self, method, path, body=None, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = dict(headers or {})
        if self.token:
            all_headers["Authorization"] = f"Bearer {self.token}"
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=all_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = data
        return response.status, parsed, dict(response.getheaders())


def paired_client(server, store):
    code = store.start_pairing()
    status, body, _ = Client(server).request("POST", "/api/pair", {"code": code, "name": "phone"})
    assert status == 200
    return Client(server, body["token"])


@pytest.mark.unit
class TestStaticApp:
    @pytest.mark.parametrize("path", ["/", "/app.js", "/app.css", "/manifest.webmanifest", "/icon.svg"])
    def test_the_app_is_served_without_a_token(self, server, path):
        status, body, headers = Client(server).request("GET", path)
        assert status == 200 and body

    def test_security_headers_are_set(self, server):
        _, _, headers = Client(server).request("GET", "/")
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_paths_outside_the_app_are_not_served(self, server):
        for path in ["/../config.json", "/%2e%2e/remote.spec.md", "/static/../server.py", "/nope.txt"]:
            status, _, _ = Client(server).request("GET", path)
            assert status == 404, path


@pytest.mark.unit
class TestAuthentication:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/poll"), ("POST", "/api/chat"), ("POST", "/api/stop"), ("POST", "/api/confirm"),
        ("POST", "/api/unpair")])
    def test_the_api_needs_a_token(self, server, method, path):
        status, _, _ = Client(server).request(method, path, {})
        assert status == 401

    def test_a_wrong_token_is_refused(self, server, store):
        paired_client(server, store)
        status, _, _ = Client(server, "made-up").request("GET", "/api/poll")
        assert status == 401

    def test_a_wrong_pairing_code_is_refused(self, server, store):
        code = store.start_pairing()
        wrong = "000000" if code != "000000" else "111111"
        status, _, _ = Client(server).request("POST", "/api/pair", {"code": wrong, "name": "phone"})
        assert status == 403

    def test_unpairing_revokes_the_device(self, server, store):
        client = paired_client(server, store)
        assert client.request("POST", "/api/unpair")[0] == 200
        assert client.request("GET", "/api/poll")[0] == 401
        assert store.devices() == []

    def test_clients_outside_private_networks_are_refused_before_anything_else(self, backend, store):
        server = make_server(backend, store, client_filter=lambda address: False)
        try:
            for method, path in [("GET", "/"), ("POST", "/api/pair")]:
                assert Client(server).request(method, path, {})[0] == 403
        finally:
            server.stop()


@pytest.mark.unit
class TestChat:
    def test_a_request_is_answered_through_the_poll(self, server, store, backend):
        client = paired_client(server, store)
        status, body, _ = client.request("POST", "/api/chat", {"text": "volume up"})
        assert status == 202
        _, snap, headers = client.request("GET", "/api/poll?after=0&rev=-1")
        assert snap["queries"][str(body["query_id"])]["status"] == "done"
        assert [e["text"] for e in snap["entries"]] == ["volume up", "Done."]
        assert headers["Cache-Control"] == "no-store"

    def test_busy_is_a_conflict(self, server, store, backend):
        backend.mode = "busy"
        assert paired_client(server, store).request("POST", "/api/chat", {"text": "hi"})[0] == 409

    def test_an_unready_daemon_is_unavailable(self, server, store, backend):
        backend.mode = "unavailable"
        assert paired_client(server, store).request("POST", "/api/chat", {"text": "hi"})[0] == 503

    @pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {"text": 5}, {}, {"text": "x" * (MAX_TEXT_CHARS + 1)}])
    def test_bad_text_is_refused(self, server, store, backend, body):
        assert paired_client(server, store).request("POST", "/api/chat", body)[0] == 400
        assert backend.submitted == []

    def test_malformed_json_is_refused(self, server, store):
        assert paired_client(server, store).request("POST", "/api/chat", raw=b"{nope")[0] == 400

    def test_an_oversized_body_is_refused(self, server, store):
        client = paired_client(server, store)
        assert client.request("POST", "/api/chat", raw=b"x" * (MAX_BODY_BYTES + 1))[0] == 413

    def test_stop_cancels_the_query(self, server, store, backend):
        backend.mode = "hold"
        client = paired_client(server, store)
        client.request("POST", "/api/chat", {"text": "long"})
        assert client.request("POST", "/api/stop")[0] == 200
        assert backend.cancelled == 1

    def test_the_poll_carries_quick_actions_and_the_confirm_setting(self, server, store):
        _, snap, _ = paired_client(server, store).request("GET", "/api/poll?after=0&rev=-1")
        assert snap["quick_actions"] == ["Pause the music"]
        assert snap["allow_confirm"] is True

    def test_a_poll_with_nothing_new_waits_then_returns(self, server, store):
        client = paired_client(server, store)
        _, first, _ = client.request("GET", "/api/poll?after=0&rev=-1")
        status, again, _ = client.request("GET", f"/api/poll?after=0&rev={first['rev']}")
        assert status == 200 and again["rev"] == first["rev"]

    @pytest.mark.parametrize("query", ["after=x&rev=1", "after=1&rev=y", ""])
    def test_odd_poll_parameters_read_as_from_the_start(self, server, store, query):
        assert paired_client(server, store).request("GET", f"/api/poll?{query}")[0] == 200


@pytest.mark.unit
class TestConfirmation:
    CONFIRMATION = {"id": "abc", "tool": "fileOps", "action": "delete", "target": "notes.txt",
                    "consequence": "The file is removed.", "expires_in": 40}

    def test_the_phone_can_approve(self, server, store, backend):
        backend.confirmation = dict(self.CONFIRMATION)
        client = paired_client(server, store)
        _, snap, _ = client.request("GET", "/api/poll?after=0&rev=-1")
        assert snap["confirmation"]["target"] == "notes.txt"
        assert client.request("POST", "/api/confirm", {"id": "abc", "approve": True})[0] == 200
        assert backend.resolved == [("abc", True)]

    def test_a_stale_answer_is_a_conflict(self, server, store):
        assert paired_client(server, store).request("POST", "/api/confirm", {"id": "gone", "approve": True})[0] == 409

    def test_approve_must_be_a_real_boolean(self, server, store, backend):
        backend.confirmation = dict(self.CONFIRMATION)
        client = paired_client(server, store)
        assert client.request("POST", "/api/confirm", {"id": "abc", "approve": "yes"})[0] == 400
        assert backend.resolved == []

    def test_confirmations_stay_on_the_desktop_when_turned_off(self, backend, store):
        backend.confirmation = dict(self.CONFIRMATION)
        server = make_server(backend, store, allow_confirm=False)
        try:
            client = paired_client(server, store)
            _, snap, _ = client.request("GET", "/api/poll?after=0&rev=-1")
            assert snap["confirmation"] is None
            assert client.request("POST", "/api/confirm", {"id": "abc", "approve": True})[0] == 403
            assert backend.resolved == []
        finally:
            server.stop()

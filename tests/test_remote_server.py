"""The phone access HTTP server over real sockets, HTTP and HTTPS, with a fake daemon behind it.

The chat is the web chat's hub on a real ``ChatStore``; confirmations, statistics and speech are fakes.
See ``remote/remote.spec.md``.
"""
import http.client
import io
import json
import socket
import threading
import ssl
import time
import wave

import pytest

from fake_remote_backend import FakeRemoteBackend
from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ChatStore
from jarvis.remote.pairing import DeviceStore
from jarvis.remote.server import MAX_BODY_BYTES, RemoteServer, ServerSettings
from jarvis.remote.tls import TLSConfig
from jarvis.remote.voice import MAX_VOICE_BYTES
from jarvis.webchat.api import MAX_TEXT_CHARS
from jarvis.webchat.hub import ChatHub
from tls_fixtures import client_context, make_certificate

SECOND = b"\x10\x00" * 16000


@pytest.fixture
def static_dir(tmp_path):
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Jarvis</title><script type=module src='/assets/app.js'>")
    (root / "assets" / "app.js").write_text("console.log('phone')")
    (root / "assets" / "app.css").write_text("body{}")
    (root / "manifest.webmanifest").write_text("{\"name\": \"Jarvis\"}")
    (root / "icon.svg").write_text("<svg/>")
    (root / "icon-192.png").write_bytes(b"\x89PNG")
    (root / "icon-512.png").write_bytes(b"\x89PNG")
    (root / "apple-touch-icon.png").write_bytes(b"\x89PNG")
    (root / "pcm-worklet.js").write_text("registerProcessor('pcm-capture', class {})")
    (tmp_path / "secret.txt").write_text("do not serve")
    return root


@pytest.fixture
def chat_backend():
    return FakeWebChatBackend()


@pytest.fixture
def backend():
    return FakeRemoteBackend()


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path)


@pytest.fixture
def chat_store(tmp_path):
    s = ChatStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


def make_server(chat_backend, chat_store, backend, store, static_dir, **overrides):
    client_filter = overrides.pop("client_filter", None)
    settings = ServerSettings(
        host="127.0.0.1", port=0, quick_actions=overrides.pop("quick_actions", ["Pause the music"]),
        allow_confirm=overrides.pop("allow_confirm", True), poll_timeout_sec=overrides.pop("poll_timeout_sec", 0.3),
        static_dir=static_dir, **overrides)
    hub = ChatHub(chat_backend, chat_store, sync_interval_sec=0.05)
    extra = {"client_filter": client_filter} if client_filter else {}
    server = RemoteServer(hub, store, backend, settings, **extra)
    server.start()
    return server


@pytest.fixture
def server(chat_backend, chat_store, backend, store, static_dir):
    server = make_server(chat_backend, chat_store, backend, store, static_dir)
    yield server
    server.stop()


class Client:
    def __init__(self, server, token=None, context=None):
        self.port = server.port
        self.token = token
        self.context = context

    def _connection(self):
        if self.context is not None:
            return http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10, context=self.context)
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def raw(self, method, path, body=None, headers=None):
        conn = self._connection()
        sent = dict(headers or {})
        if self.token:
            sent["Authorization"] = f"Bearer {self.token}"
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data, dict(response.getheaders())

    def request(self, method, path, body=None, raw=None, headers=None):
        sent = dict(headers or {})
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if payload is not None:
            sent.setdefault("Content-Type", "application/json")
        status, data, response_headers = self.raw(method, path, payload, sent)
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = data
        return status, parsed, response_headers


def pair(server, store, context=None):
    code = store.start_pairing()
    status, body, _ = Client(server, context=context).request("POST", "/api/pair", {"code": code, "name": "phone"})
    assert status == 200
    return Client(server, body["token"], context)


def paired_client(server, store):
    return pair(server, store)


def poll(client, **query):
    params = {"after": 0, "rev": -1, **query}
    return client.request("GET", "/api/poll?" + "&".join(f"{k}={v}" for k, v in params.items()))


@pytest.mark.unit
class TestStaticApp:
    @pytest.mark.parametrize("path", ["/", "/index.html", "/manifest.webmanifest", "/icon.svg", "/icon-192.png",
                                      "/icon-512.png", "/apple-touch-icon.png", "/pcm-worklet.js",
                                      "/assets/app.js", "/assets/app.css"])
    def test_the_app_is_served_without_a_token(self, server, path):
        status, body, _ = Client(server).request("GET", path)
        assert status == 200 and body

    def test_security_headers_are_set(self, server):
        _, _, headers = Client(server).request("GET", "/")
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_only_the_phones_own_page_may_ask_for_the_microphone(self, server):
        for path in ("/", "/api/poll"):
            _, _, headers = Client(server).request("GET", path)
            policy = headers["Permissions-Policy"]
            assert "microphone=(self)" in policy and "camera=()" in policy and "geolocation=()" in policy

    def test_paths_outside_the_app_are_not_served(self, server):
        for path in ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt", "/assets/%2e%2e/index.html",
                     "/static/../server.py", "/nope.txt", "/assets/", "/assets/%5C%5Cevil.example%5Cshare%5Cx.js",
                     "/assets//evil.example/share/x.js", "/assets/C:%5CWindows%5Cwin.ini"]:
            status, _, _ = Client(server).request("GET", path)
            assert status == 404, path


@pytest.mark.unit
class TestAuthentication:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/poll"), ("POST", "/api/chat"), ("POST", "/api/stop"), ("POST", "/api/confirm"),
        ("POST", "/api/unpair"), ("POST", "/api/voice"), ("GET", "/api/audio/" + "0" * 32),
        ("GET", "/api/library"), ("GET", "/api/state"), ("POST", "/api/projects"), ("GET", "/api/models")])
    def test_the_api_needs_a_token(self, server, method, path):
        if path == "/api/voice":
            status, _, _ = Client(server).request(method, path, raw=SECOND, headers={
                "Content-Type": "application/octet-stream"})
        else:
            status, _, _ = Client(server).request(method, path, {} if method == "POST" else None)
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
        assert poll(client)[0] == 401
        assert store.devices() == []

    def test_clients_outside_private_networks_are_refused_before_anything_else(
            self, chat_backend, chat_store, backend, store, static_dir):
        server = make_server(chat_backend, chat_store, backend, store, static_dir, client_filter=lambda a: False)
        try:
            for method, path in [("GET", "/"), ("POST", "/api/pair")]:
                assert Client(server).request(method, path, {})[0] == 403
        finally:
            server.stop()


@pytest.mark.unit
class TestChatThroughTheSharedHub:
    def test_a_request_is_answered_in_the_open_chat(self, server, store, chat_backend):
        client = paired_client(server, store)
        status, body, _ = client.request("POST", "/api/chat", {"text": "volume up"})
        assert status == 202 and body["status"] == "accepted"
        _, snap, headers = poll(client)
        assert [(m["role"], m["text"]) for m in snap["messages"]] == [("user", "volume up"), ("assistant", "Done.")]
        assert headers["Cache-Control"] == "no-store"
        assert chat_backend.origins == ["phone"]

    def test_the_conversation_is_the_web_chats_one(self, server, store, chat_store):
        client = paired_client(server, store)
        client.request("POST", "/api/chat", {"text": "hello"})
        stored = chat_store.messages(chat_store.get_active_chat_id())
        assert [m.content for m in stored] == ["hello", "Done."]
        _, library, _ = client.request("GET", "/api/library")
        assert library["active_chat_id"] == chat_store.get_active_chat_id()

    def test_busy_is_a_conflict(self, server, store, chat_backend):
        chat_backend.mode = "busy"
        assert paired_client(server, store).request("POST", "/api/chat", {"text": "hi"})[0] == 409

    def test_an_unready_daemon_is_unavailable(self, server, store, chat_backend):
        chat_backend.mode = "unavailable"
        assert paired_client(server, store).request("POST", "/api/chat", {"text": "hi"})[0] == 503

    @pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {"text": 5}, {}, {"text": "x" * (MAX_TEXT_CHARS + 1)}])
    def test_bad_text_is_refused(self, server, store, chat_backend, body):
        assert paired_client(server, store).request("POST", "/api/chat", body)[0] == 400
        assert chat_backend.submitted == []

    def test_malformed_json_is_refused(self, server, store):
        assert paired_client(server, store).request("POST", "/api/chat", raw=b"{nope")[0] == 400

    def test_an_oversized_body_is_refused(self, server, store):
        assert paired_client(server, store).request("POST", "/api/chat", raw=b"x" * (MAX_BODY_BYTES + 1))[0] == 413

    def test_stop_cancels_the_query(self, server, store, chat_backend):
        chat_backend.mode = "hold"
        client = paired_client(server, store)
        client.request("POST", "/api/chat", {"text": "long"})
        assert client.request("POST", "/api/stop")[0] == 200
        assert chat_backend.cancelled == 1

    def test_the_poll_carries_the_phone_object(self, server, store):
        _, snap, _ = poll(paired_client(server, store))
        phone = snap["phone"]
        assert phone["quick_actions"] == ["Pause the music"] and phone["allow_confirm"] is True
        assert phone["pc"] == {"cpu": 12.0, "ram": 40.0} and phone["confirmation"] is None
        assert phone["voice"]["audio"] == {}

    def test_a_poll_with_nothing_new_waits_then_returns(self, server, store):
        client = paired_client(server, store)
        _, first, _ = poll(client)
        status, again, _ = poll(client, rev=first["rev"], after=2 ** 40)
        assert status == 200 and again["rev"] == first["rev"]

    @pytest.mark.parametrize("query", ["after=x&rev=1", "after=1&rev=y", ""])
    def test_odd_poll_parameters_read_as_from_the_start(self, server, store, query):
        assert paired_client(server, store).request("GET", f"/api/poll?{query}")[0] == 200

    def test_projects_and_chats_work_from_the_phone(self, server, store):
        client = paired_client(server, store)
        _, project, _ = client.request("POST", "/api/projects", {"name": "Home"})
        status, created, _ = client.request("POST", "/api/chats", {"project_id": project["id"]})
        assert status == 201 and created["chat"]["project_id"] == project["id"]
        assert client.request("PATCH", f"/api/chats/{created['chat']['id']}", {"pinned": True})[0] == 200
        _, library, _ = client.request("GET", "/api/library")
        assert [p["name"] for p in library["projects"]] == ["Home"]

    def test_search_models_and_suggestions_are_served(self, server, store):
        client = paired_client(server, store)
        client.request("POST", "/api/chat", {"text": "remember the milk"})
        _, found, _ = client.request("GET", "/api/search?q=milk")
        assert len(found["results"]) == 1
        assert client.request("GET", "/api/models")[0] == 200
        assert client.request("GET", "/api/suggestions")[0] == 200

    @pytest.mark.parametrize("method,path,body", [
        ("POST", "/api/clear", {"confirm": True}), ("POST", "/api/attachments/screenshot", {}),
        ("GET", "/api/identity?nonce=" + "ab" * 16, None), ("GET", "/api/mic", None),
        ("POST", "/api/mic/start", {}), ("POST", "/api/mic/stop", {"session": "0" * 32})])
    def test_what_acts_on_the_pc_is_not_served_to_a_phone(self, server, store, chat_backend, method, path, body):
        client = paired_client(server, store)
        assert client.request(method, path, body)[0] == 404
        assert chat_backend.mic_calls == [] and chat_backend.switch_calls == []

    def test_clearing_everything_from_a_phone_deletes_nothing(self, server, store, chat_store):
        client = paired_client(server, store)
        client.request("POST", "/api/chat", {"text": "keep me"})
        client.request("POST", "/api/clear", {"confirm": True})
        assert [m.content for m in chat_store.messages(chat_store.get_active_chat_id())] == ["keep me", "Done."]


@pytest.mark.unit
class TestAttachments:
    def test_a_text_file_can_be_attached_from_the_phone(self, server, store):
        client = paired_client(server, store)
        status, body, _ = client.request("POST", "/api/attachments", raw=b"some notes", headers={
            "Content-Type": "application/octet-stream"})
        assert status == 201 and body["kind"] == "text" and "id" in body

    def test_an_upload_must_declare_octet_stream(self, server, store):
        client = paired_client(server, store)
        assert client.request("POST", "/api/attachments", raw=b"some notes")[0] == 415

    def test_the_screenshot_button_has_no_reason_to_show(self, server, store):
        _, support, _ = paired_client(server, store).request("GET", "/api/attachments/support")
        assert "screenshot" in support


@pytest.mark.unit
class TestConfirmation:
    CONFIRMATION = {"id": "abc", "tool": "fileOps", "action": "delete", "target": "notes.txt",
                    "consequence": "The file is removed.", "expires_in": 40}

    def test_the_phone_can_approve(self, server, store, backend):
        backend.confirmation = dict(self.CONFIRMATION)
        client = paired_client(server, store)
        _, snap, _ = poll(client)
        assert snap["phone"]["confirmation"]["target"] == "notes.txt"
        assert client.request("POST", "/api/confirm", {"id": "abc", "approve": True})[0] == 200
        assert backend.resolved == [("abc", True)]

    def test_a_stale_answer_is_a_conflict(self, server, store):
        assert paired_client(server, store).request("POST", "/api/confirm", {"id": "gone", "approve": True})[0] == 409

    def test_approve_must_be_a_real_boolean(self, server, store, backend):
        backend.confirmation = dict(self.CONFIRMATION)
        client = paired_client(server, store)
        assert client.request("POST", "/api/confirm", {"id": "abc", "approve": "yes"})[0] == 400
        assert backend.resolved == []

    def test_confirmations_stay_on_the_desktop_when_turned_off(self, chat_backend, chat_store, backend, store, static_dir):
        backend.confirmation = dict(self.CONFIRMATION)
        server = make_server(chat_backend, chat_store, backend, store, static_dir, allow_confirm=False)
        try:
            client = paired_client(server, store)
            _, snap, _ = poll(client)
            assert snap["phone"]["confirmation"] is None
            assert client.request("POST", "/api/confirm", {"id": "abc", "approve": True})[0] == 403
            assert backend.resolved == []
        finally:
            server.stop()

    def test_a_new_confirmation_wakes_a_waiting_poll(self, chat_backend, chat_store, backend, store, static_dir):
        server = make_server(chat_backend, chat_store, backend, store, static_dir, poll_timeout_sec=10)
        try:
            client = paired_client(server, store)
            _, first, _ = poll(client)
            backend.confirmation = dict(self.CONFIRMATION)
            started = time.monotonic()
            _, snap, _ = poll(client, rev=first["rev"], after=2 ** 40)
            assert time.monotonic() - started < 5
            assert snap["phone"]["confirmation"]["id"] == "abc"
        finally:
            server.stop()


@pytest.mark.unit
class TestOtherWebPagesCannotReachTheServer:
    """Another web page in a browser on the network must not reach phone access: a rebound DNS name arrives with
    a foreign Host, a cross-site request carries a foreign Origin or ``Sec-Fetch-Site``, and a simple form post
    cannot send a JSON body. The phone app itself, opened by address or by the PC's name, is served."""

    @pytest.mark.parametrize("host", ["192.168.1.5:8765", "100.101.102.103:8765", "127.0.0.1:8765",
                                      "localhost:8765", "[fd7a:115c:a1e0::1]:8765"])
    def test_the_app_opened_by_address_is_served(self, server, host):
        assert Client(server).request("GET", "/", headers={"Host": host})[0] == 200

    def test_the_app_opened_by_the_pcs_name_is_served(self, server):
        name = socket.gethostname()
        for host in (f"{name}:8765", f"{name.lower()}.local:8765", f"{name.lower()}.tail1234.ts.net"):
            assert Client(server).request("GET", "/", headers={"Host": host})[0] == 200, host

    @pytest.mark.parametrize("host", ["evil.example:8765", "evil.example", "8.8.8.8:8765", "",
                                      "192.168.1.5.evil.example", "phone.tail1234.ts.net"])
    def test_a_foreign_host_is_refused(self, server, host):
        assert Client(server).request("GET", "/", headers={"Host": host})[0] == 403

    def test_the_certificates_own_name_is_served_even_when_it_is_not_the_host_name(
            self, chat_backend, chat_store, backend, store, static_dir):
        server = make_server(chat_backend, chat_store, backend, store, static_dir, tls_host="renamed.tail1234.ts.net")
        try:
            assert Client(server).request("GET", "/", headers={"Host": "renamed.tail1234.ts.net:8765"})[0] == 200
            assert Client(server).request("GET", "/", headers={"Host": "other.tail1234.ts.net:8765"})[0] == 403
        finally:
            server.stop()

    def test_a_rebound_name_cannot_spend_pairing_attempts(self, server, store):
        code = store.start_pairing()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(6):
            status, _, _ = Client(server).request("POST", "/api/pair", {"code": wrong, "name": "x"},
                                                  headers={"Host": "evil.example:8765"})
            assert status == 403
        assert Client(server).request("POST", "/api/pair", {"code": code, "name": "phone"})[0] == 200

    def test_a_request_from_another_origin_is_refused(self, server, store):
        code = store.start_pairing()
        status, _, _ = Client(server).request("POST", "/api/pair", {"code": code, "name": "x"},
                                              headers={"Origin": "http://evil.example"})
        assert status == 403
        assert store.devices() == []

    def test_a_request_from_the_page_itself_is_served(self, server, store):
        code = store.start_pairing()
        status, _, _ = Client(server).request(
            "POST", "/api/pair", {"code": code, "name": "phone"},
            headers={"Host": "192.168.1.5:8765", "Origin": "http://192.168.1.5:8765", "Sec-Fetch-Site": "same-origin"})
        assert status == 200

    def test_a_cross_site_api_request_is_refused(self, server, store):
        client = paired_client(server, store)
        assert poll(client)[0] == 200
        status, _, _ = client.request("GET", "/api/poll?after=0&rev=-1", headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403

    def test_a_cross_site_audio_upload_is_refused(self, server, store, backend):
        client = paired_client(server, store)
        status, _, _ = client.request("POST", "/api/voice", raw=SECOND, headers={
            "Content-Type": "application/octet-stream", "Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"})
        assert status == 403 and backend.transcribed == []

    def test_a_link_from_another_site_still_opens_the_app(self, server):
        assert Client(server).request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 200

    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None])
    def test_a_body_that_is_not_declared_json_is_refused(self, server, store, content_type):
        code = store.start_pairing()
        headers = {"Content-Type": content_type} if content_type else {"Content-Type": ""}
        raw = json.dumps({"code": code, "name": "x"}).encode()
        assert Client(server).request("POST", "/api/pair", raw=raw, headers=headers)[0] == 415
        assert Client(server).request("POST", "/api/pair", {"code": code, "name": "phone"})[0] == 200

    def test_a_pairing_without_a_body_is_refused_without_spending_an_attempt(self, server, store):
        code = store.start_pairing()
        for _ in range(6):
            assert Client(server).request("POST", "/api/pair")[0] == 415
        assert Client(server).request("POST", "/api/pair", {"code": code, "name": "phone"})[0] == 200

    def test_bodiless_commands_from_the_phone_still_work(self, server, store):
        client = paired_client(server, store)
        assert client.request("POST", "/api/stop")[0] == 200
        assert client.request("POST", "/api/unpair")[0] == 200


@pytest.fixture
def secure_server(chat_backend, chat_store, backend, store, static_dir, tmp_path):
    cert, key = make_certificate(tmp_path)
    server = make_server(chat_backend, chat_store, backend, store, static_dir,
                         tls=TLSConfig(str(cert), str(key)))
    server.context = client_context(cert)
    yield server
    server.stop()


@pytest.mark.unit
class TestHttps:
    def test_the_app_is_served_over_https_on_the_same_port(self, secure_server):
        status, body, _ = Client(secure_server, context=secure_server.context).request("GET", "/")
        assert status == 200 and body

    def test_pairing_and_the_api_work_over_https(self, secure_server, store, chat_backend):
        client = pair(secure_server, store, secure_server.context)
        assert client.request("POST", "/api/chat", {"text": "hello"})[0] == 202
        assert chat_backend.origins == ["phone"]

    def test_every_guard_still_applies_over_https(self, secure_server, store):
        client = pair(secure_server, store, secure_server.context)
        assert Client(secure_server, context=secure_server.context).request("GET", "/api/poll")[0] == 401
        assert client.request("GET", "/", headers={"Host": "evil.example"})[0] == 403
        assert client.request("POST", "/api/chat", {"text": "x"}, headers={"Origin": "https://evil.example"})[0] == 403
        assert client.request("GET", "/api/poll", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403

    def test_plain_http_to_the_https_port_gets_no_page(self, secure_server):
        with pytest.raises((http.client.HTTPException, OSError)):
            conn = http.client.HTTPConnection("127.0.0.1", secure_server.port, timeout=5)
            conn.request("GET", "/")
            response = conn.getresponse()
            assert response.status != 200
            raise OSError("no page served")

    def test_a_client_that_never_finishes_the_handshake_blocks_nobody(self, secure_server):
        stalled = socket.create_connection(("127.0.0.1", secure_server.port), timeout=5)
        try:
            status, _, _ = Client(secure_server, context=secure_server.context).request("GET", "/")
            assert status == 200
        finally:
            stalled.close()

    def test_an_untrusted_client_cannot_complete_a_request(self, secure_server):
        with pytest.raises(ssl.SSLError):
            Client(secure_server, context=ssl.create_default_context()).request("GET", "/")

    def test_plain_http_is_not_available_when_a_certificate_is_set(self, secure_server):
        assert secure_server.secure


@pytest.mark.unit
class TestSpokenRequests:
    def speak(self, client, body=SECOND, **headers):
        sent = {"Content-Type": "application/octet-stream", **headers}
        return client.request("POST", "/api/voice", raw=body, headers=sent)

    def test_plain_http_has_no_voice_and_says_why(self, server, store, backend):
        client = paired_client(server, store)
        status, body, _ = self.speak(client)
        assert (status, body["error"]) == (503, "no_https") and backend.transcribed == []
        assert poll(client)[1]["phone"]["voice"] == {"available": False, "reason": "no_https", "audio": {}}

    def test_over_https_a_spoken_request_gets_a_spoken_reply(self, secure_server, store, chat_backend, backend):
        client = pair(secure_server, store, secure_server.context)
        assert poll(client)[1]["phone"]["voice"]["available"] is True
        status, body, _ = self.speak(client)
        assert status == 202 and body["text"] == "what time is it"
        assert chat_backend.origins == ["phone"] and chat_backend.submitted == ["what time is it"]
        deadline = time.time() + 5
        audio = {}
        while not audio.get(str(body["query_id"]), {}).get("status") == "ready":
            assert time.time() < deadline
            audio = poll(client)[1]["phone"]["voice"]["audio"]
            time.sleep(0.02)
        url = audio[str(body["query_id"])]["url"]
        status, clip, headers = client.raw("GET", url)
        assert status == 200 and headers["Content-Type"] == "audio/wav" and headers["Cache-Control"] == "no-store"
        with wave.open(io.BytesIO(clip)) as wav:
            assert wav.getframerate() == 22050
        assert client.raw("GET", url)[0] == 404

    def test_another_paired_phone_cannot_fetch_the_reply_audio(self, secure_server, store):
        owner = pair(secure_server, store, secure_server.context)
        intruder = pair(secure_server, store, secure_server.context)
        _, body, _ = self.speak(owner)
        deadline = time.time() + 5
        while True:
            audio = poll(owner)[1]["phone"]["voice"]["audio"]
            if audio.get(str(body["query_id"]), {}).get("status") == "ready":
                break
            assert time.time() < deadline
            time.sleep(0.02)
        url = audio[str(body["query_id"])]["url"]
        assert intruder.raw("GET", url)[0] == 404
        assert owner.raw("GET", url)[0] == 200  # the failed attempt did not spend it

    def test_a_typed_request_can_ask_for_a_spoken_reply(self, secure_server, store):
        client = pair(secure_server, store, secure_server.context)
        _, body, _ = client.request("POST", "/api/chat", {"text": "hello", "speak": True})
        deadline = time.time() + 5
        while poll(client)[1]["phone"]["voice"]["audio"].get(str(body["query_id"]), {}).get("status") != "ready":
            assert time.time() < deadline
            time.sleep(0.02)

    def test_a_typed_request_does_not_ask_for_audio_by_default(self, secure_server, store, backend):
        client = pair(secure_server, store, secure_server.context)
        client.request("POST", "/api/chat", {"text": "hello"})
        assert poll(client)[1]["phone"]["voice"]["audio"] == {} and backend.rendered == []

    def test_audio_must_declare_octet_stream(self, secure_server, store):
        client = pair(secure_server, store, secure_server.context)
        assert client.request("POST", "/api/voice", raw=SECOND)[0] == 415
        assert client.request("POST", "/api/voice", {"audio": "x"})[0] == 415

    def test_audio_over_the_cap_is_refused_whole(self, secure_server, store, backend):
        client = pair(secure_server, store, secure_server.context)
        status, _, _ = self.speak(client, b"\x00" * (MAX_VOICE_BYTES + 2))
        assert status == 413 and backend.transcribed == []

    def test_a_busy_pc_refuses_before_transcribing(self, secure_server, store, backend):
        backend.busy = True
        client = pair(secure_server, store, secure_server.context)
        assert self.speak(client)[0] == 409 and backend.transcribed == []

    def test_speech_the_model_cannot_hear_starts_nothing(self, secure_server, store, backend, chat_backend):
        backend.transcript = ""
        client = pair(secure_server, store, secure_server.context)
        status, body, _ = self.speak(client)
        assert (status, body["status"]) == (200, "nothing_heard") and chat_backend.submitted == []

    def test_the_speech_model_not_being_ready_is_said_in_the_poll(self, secure_server, store, backend):
        backend.voice_blocker = "not_ready"
        client = pair(secure_server, store, secure_server.context)
        assert poll(client)[1]["phone"]["voice"] == {"available": False, "reason": "not_ready", "audio": {}}


@pytest.mark.unit
class TestNothingSensitiveIsLogged:
    def test_no_token_transcript_reply_or_device_name_reaches_the_logs(
            self, secure_server, store, backend, chat_backend, monkeypatch):
        from jarvis.remote import phone, server as server_module, voice
        from jarvis.webchat import hub as hub_module
        lines = []
        for module in (server_module, voice, phone, hub_module):
            monkeypatch.setattr(module, "debug_log", lambda message, *a, **k: lines.append(str(message)))
        backend.transcript = "confidential words about the bank"
        chat_backend.reply_text = "A confidential reply."
        code = store.start_pairing()
        _, body, _ = Client(secure_server, context=secure_server.context).request(
            "POST", "/api/pair", {"code": code, "name": "Alex-private-phone"})
        client = Client(secure_server, body["token"], secure_server.context)
        _, sent, _ = TestSpokenRequests().speak(client)
        deadline = time.time() + 5
        while poll(client)[1]["phone"]["voice"]["audio"].get(str(sent["query_id"]), {}).get("status") != "ready":
            assert time.time() < deadline
            time.sleep(0.02)
        url = poll(client)[1]["phone"]["voice"]["audio"][str(sent["query_id"])]["url"]
        client.raw("GET", url)
        joined = "\n".join(lines)
        assert lines
        for secret in ("confidential", "bank", "Alex-private-phone", body["token"], code, url.rsplit("/", 1)[1]):
            assert secret not in joined


@pytest.mark.unit
class TestReviewFindings:
    """Edge cases a fresh-eyes review found: what an unauthenticated client may make the server read, a request
    the phone gave up on, and the content type of every method."""

    def raw_post(self, server, path, length, headers=None, send=0):
        """A POST that declares ``length`` bytes and sends ``send`` of them, returning the status line's code."""
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                f"Content-Type: application/octet-stream\r\nContent-Length: {length}\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()) + "\r\n")
        sock.sendall(head.encode() + b"x" * send)
        sock.settimeout(5)
        try:
            data = sock.recv(200)
        finally:
            sock.close()
        return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else None

    @pytest.mark.parametrize("path,length", [("/api/voice", MAX_VOICE_BYTES), ("/api/attachments", 10 * 1024 * 1024)])
    def test_an_unpaired_client_cannot_make_the_server_read_a_large_body(self, server, path, length):
        started = time.monotonic()
        status = self.raw_post(server, path, length, send=1000)   # the rest is never sent
        assert status == 401
        assert time.monotonic() - started < 3   # answered at once, without waiting for the body

    def test_a_client_outside_private_networks_is_refused_before_a_large_body_is_read(
            self, chat_backend, chat_store, backend, store, static_dir):
        server = make_server(chat_backend, chat_store, backend, store, static_dir, client_filter=lambda a: False)
        try:
            assert self.raw_post(server, "/api/voice", MAX_VOICE_BYTES, send=1000) == 403
        finally:
            server.stop()

    def test_a_paired_phone_may_still_send_a_large_body(self, secure_server, store, backend):
        client = pair(secure_server, store, secure_server.context)
        status, body, _ = client.request("POST", "/api/voice", raw=SECOND, headers={"Content-Type": "application/octet-stream"})
        assert status == 202

    @pytest.mark.parametrize("method,path", [("PATCH", "/api/chats/{chat}"), ("PATCH", "/api/projects/{project}")])
    def test_every_method_with_a_body_must_declare_json(self, server, store, method, path):
        client = paired_client(server, store)
        _, project, _ = client.request("POST", "/api/projects", {"name": "Home"})
        _, library, _ = client.request("GET", "/api/library")
        target = path.format(chat=library["active_chat_id"], project=project["id"])
        assert client.request(method, target, raw=b'{"title": "x", "name": "x"}', headers={"Content-Type": "text/plain"})[0] == 415
        assert client.request(method, target, raw=b'{"title": "x", "name": "x"}', headers={"Content-Type": ""})[0] == 415
        assert client.request(method, target, {"title": "x", "name": "x"})[0] == 200

    def test_a_phone_that_hung_up_while_its_words_were_being_written_out_starts_nothing(
            self, secure_server, store, backend, chat_backend):
        client = pair(secure_server, store, secure_server.context)
        release, started = threading.Event(), threading.Event()
        original = backend.transcribe

        def slow(samples):
            started.set()
            release.wait(5)
            return original(samples)

        backend.transcribe = slow
        raw = socket.create_connection(("127.0.0.1", secure_server.port), timeout=5)
        tls = secure_server.context.wrap_socket(raw, server_hostname="127.0.0.1")
        tls.sendall((f"POST /api/voice HTTP/1.1\r\nHost: 127.0.0.1:{secure_server.port}\r\n"
                     f"Authorization: Bearer {client.token}\r\nContent-Type: application/octet-stream\r\n"
                     f"Content-Length: {len(SECOND)}\r\n\r\n").encode() + SECOND)
        assert started.wait(5)   # the PC is transcribing now
        tls.close()
        time.sleep(0.2)
        release.set()
        time.sleep(0.5)
        assert chat_backend.submitted == []

    def test_stop_ends_a_spoken_request_still_being_written_out(self, secure_server, store, backend, chat_backend):
        client = pair(secure_server, store, secure_server.context)
        release, started = threading.Event(), threading.Event()
        original = backend.transcribe
        backend.transcribe = lambda samples: (started.set(), release.wait(5), original(samples))[2]
        result = {}
        thread = threading.Thread(target=lambda: result.update(
            zip(("status", "body"), client.request("POST", "/api/voice", raw=SECOND,
                                                   headers={"Content-Type": "application/octet-stream"})[:2])))
        thread.start()
        assert started.wait(5)
        assert client.request("POST", "/api/stop")[0] == 200
        release.set()
        thread.join(timeout=5)
        assert result["body"]["status"] == "stopped"
        assert chat_backend.submitted == []


@pytest.mark.unit
class TestOutsideClients:
    def test_an_outside_client_is_answered_before_its_request_is_read(
            self, chat_backend, chat_store, backend, store, static_dir):
        # A client trickling its request line (slowloris over an accidental port forward) must not hold a
        # thread until the 60-second read timeout: it is refused before a byte is parsed.
        server = make_server(chat_backend, chat_store, backend, store, static_dir, client_filter=lambda a: False)
        try:
            started = time.monotonic()
            sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            try:
                sock.sendall(b"GET / HT")   # never finished
                data = sock.recv(200)
            finally:
                sock.close()
            assert data.split(b" ", 2)[1] == b"403"
            assert time.monotonic() - started < 3
        finally:
            server.stop()

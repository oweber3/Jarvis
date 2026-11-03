"""Phone access start-up: nothing when off, one hub shared with the web chat, HTTPS when asked, and a warning
(not a crash) when the port is taken or the certificate is unusable."""
import http.client
import socket
from types import SimpleNamespace

import pytest

from fake_remote_backend import FakeRemoteBackend
from fake_webchat_backend import FakeWebChatBackend
from jarvis.remote import runtime
from jarvis.webchat import runtime as web_runtime
from tls_fixtures import client_context, make_certificate


def settings(tmp_path, **values):
    base = dict(remote_access_enabled=True, remote_access_host="127.0.0.1", remote_access_port=0,
                remote_access_allow_confirm=True, remote_access_quick_actions=["Next song"],
                remote_access_tls_cert="", remote_access_tls_key="", remote_access_tls_host="",
                web_chat_enabled=False, web_chat_port=0, db_path=str(tmp_path / "jarvis.db"))
    base.update(values)
    return SimpleNamespace(**base)


def start(cfg):
    return runtime.start(cfg, backend=FakeRemoteBackend(), chat_backend=FakeWebChatBackend())


@pytest.fixture(autouse=True)
def stopped():
    yield
    runtime.stop()
    web_runtime.stop()


@pytest.mark.unit
class TestRuntime:
    def test_nothing_runs_when_phone_access_is_off(self, tmp_path):
        assert start(settings(tmp_path, remote_access_enabled=False)) is None
        assert runtime.current() is None
        assert not (tmp_path / "jarvis.db").exists()

    def test_the_app_is_served_when_on(self, tmp_path, capsys):
        server = start(settings(tmp_path))
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
        conn.close()
        out = capsys.readouterr().out
        assert "📱" in out and "HTTP" in out and "python -m jarvis.remote cert" in out

    def test_devices_live_next_to_the_database(self, tmp_path):
        server = start(settings(tmp_path))
        server.store.start_pairing()
        assert (tmp_path / "remote_pairing.json").exists()

    def test_a_taken_port_warns_and_leaves_jarvis_running(self, tmp_path, capsys):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            port = blocker.getsockname()[1]
            assert start(settings(tmp_path, remote_access_port=port)) is None
            assert "⚠️" in capsys.readouterr().out
            assert web_runtime._shared is None   # the hub it took was given back
        finally:
            blocker.close()

    def test_stop_closes_the_port(self, tmp_path):
        server = start(settings(tmp_path))
        port = server.port
        runtime.stop()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()


@pytest.mark.unit
class TestHttps:
    def test_a_certificate_makes_the_port_serve_https(self, tmp_path, capsys):
        cert, key = make_certificate(tmp_path)
        server = start(settings(tmp_path, remote_access_tls_cert=str(cert), remote_access_tls_key=str(key),
                                remote_access_tls_host="pc.tail1234.ts.net"))
        conn = http.client.HTTPSConnection("127.0.0.1", server.port, timeout=5, context=client_context(cert))
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
        conn.close()
        out = capsys.readouterr().out
        assert "HTTPS" in out and f"https://pc.tail1234.ts.net:{server.port}/" in out
        assert "http://" not in out and "python -m jarvis.remote cert" not in out

    def test_an_unusable_certificate_stops_the_start_and_never_falls_back_to_http(self, tmp_path, capsys):
        cert, key = make_certificate(tmp_path)
        cert.write_text("not a certificate")
        assert start(settings(tmp_path, remote_access_tls_cert=str(cert), remote_access_tls_key=str(key))) is None
        out = capsys.readouterr().out
        assert "⚠️" in out and "could not be loaded" in out and "python -m jarvis.remote cert" in out
        assert runtime.current() is None and web_runtime._shared is None

    def test_a_missing_key_file_is_named(self, tmp_path, capsys):
        cert, _ = make_certificate(tmp_path)
        assert start(settings(tmp_path, remote_access_tls_cert=str(cert),
                              remote_access_tls_key=str(tmp_path / "gone.key"))) is None
        assert "key file was not found" in capsys.readouterr().out


@pytest.mark.unit
class TestSharedHub:
    def test_with_the_web_chat_off_phone_access_runs_its_own_hub(self, tmp_path):
        server = start(settings(tmp_path))
        assert web_runtime.current() is None
        assert server.hub._thread is not None and server.hub._thread.is_alive()

    def test_with_both_on_they_share_one_hub_and_one_store(self, tmp_path):
        cfg = settings(tmp_path, web_chat_enabled=True)
        web = web_runtime.start(cfg, backend=FakeWebChatBackend())
        phone = start(cfg)
        assert phone.hub is web.hub and phone.hub.store is web.hub.store

    def test_the_web_chats_own_server_never_serves_the_phones_routes(self, tmp_path):
        cfg = settings(tmp_path, web_chat_enabled=True)
        web = web_runtime.start(cfg, backend=FakeWebChatBackend())
        start(cfg)
        for method, path in (("POST", "/api/voice"), ("POST", "/api/confirm"), ("POST", "/api/pair")):
            conn = http.client.HTTPConnection("127.0.0.1", web.port, timeout=5)
            conn.request(method, path, body=b"{}", headers={"Content-Type": "application/json"})
            assert conn.getresponse().status == 404, path
            conn.close()
        assert web._httpd.server_address[0] == "127.0.0.1"

    def test_stopping_phone_access_leaves_the_web_chat_running(self, tmp_path):
        cfg = settings(tmp_path, web_chat_enabled=True)
        web = web_runtime.start(cfg, backend=FakeWebChatBackend())
        start(cfg)
        runtime.stop()
        assert web.hub._thread is not None and web.hub._thread.is_alive()
        conn = http.client.HTTPConnection("127.0.0.1", web.port, timeout=5)
        conn.request("GET", "/api/state")
        assert conn.getresponse().status == 200
        conn.close()

    def test_stopping_the_web_chat_leaves_phone_access_running(self, tmp_path):
        cfg = settings(tmp_path, web_chat_enabled=True)
        web_runtime.start(cfg, backend=FakeWebChatBackend())
        phone = start(cfg)
        web_runtime.stop()
        assert phone.hub._thread is not None and phone.hub._thread.is_alive()
        phone.hub.library()   # the store is still open

    def test_the_store_closes_with_the_last_user(self, tmp_path):
        cfg = settings(tmp_path, web_chat_enabled=True)
        web_runtime.start(cfg, backend=FakeWebChatBackend())
        start(cfg)
        web_runtime.stop()
        assert web_runtime._shared is not None
        runtime.stop()
        assert web_runtime._shared is None

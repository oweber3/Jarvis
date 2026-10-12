"""The web chat window only shows Jarvis's own server, never another program that holds the port.

Each run of the web chat server has a random identity token that only the daemon and the desktop app
know (in process when bundled, over the daemon's stdout in subprocess mode). The window asks the
server to prove it holds the token for a fresh nonce before it loads the page. See
``webchat/webchat.spec.md`` (Desktop window).
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtWidgets import QWidget

from fake_webchat_backend import FakeWebChatBackend
from jarvis import daemon
from jarvis.memory.chat_store import ChatStore
from jarvis.webchat import identity
from jarvis.webchat.hub import ChatHub
from jarvis.webchat.server import ServerSettings, WebChatServer


@pytest.fixture
def jarvis_server(tmp_path):
    store = ChatStore(str(tmp_path / "jarvis.db"))
    server = WebChatServer(ChatHub(FakeWebChatBackend(), store, sync_interval_sec=0.05),
                           ServerSettings(host="127.0.0.1", port=0, poll_timeout_sec=0.3))
    server.start()
    yield server
    server.stop()
    store.close()


class _Foreign(BaseHTTPRequestHandler):
    """Another local program on the port: answers everything, knows nothing of Jarvis's token."""

    body = b"<!doctype html><title>Jarvis Chat</title><p>not Jarvis"
    content_type = "text/html"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class _ForeignJson(_Foreign):
    body = json.dumps({"proof": "0" * 64}).encode()
    content_type = "application/json"


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


@pytest.fixture(params=[_Foreign, _ForeignJson], ids=["html", "json"])
def foreign_port(request):
    httpd = _serve(request.param)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port, path, host=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


# ---------------------------------------------------------------------------
# The server proves who it is
# ---------------------------------------------------------------------------

class TestServerIdentity:
    def test_the_server_proves_it_holds_its_token_for_a_fresh_nonce(self, jarvis_server):
        nonce = identity.new_nonce()
        status, data = _get(jarvis_server.port, f"/api/identity?nonce={nonce}")
        assert status == 200
        assert json.loads(data) == {"proof": identity.proof(jarvis_server.identity_token, nonce)}

    def test_the_token_itself_never_travels(self, jarvis_server):
        _, data = _get(jarvis_server.port, f"/api/identity?nonce={identity.new_nonce()}")
        assert jarvis_server.identity_token not in data.decode()

    def test_each_server_has_its_own_token(self, jarvis_server, tmp_path):
        other = WebChatServer(SimpleNamespace(store=None), ServerSettings(host="127.0.0.1", port=0))
        assert other.identity_token != jarvis_server.identity_token
        assert len(jarvis_server.identity_token) >= 32

    @pytest.mark.parametrize("query", ["", "?nonce=", "?nonce=xyz", "?nonce=" + "a" * 8, "?nonce=" + "a" * 500])
    def test_a_missing_or_odd_nonce_is_refused(self, jarvis_server, query):
        status, _ = _get(jarvis_server.port, f"/api/identity{query}")
        assert status == 400

    def test_the_usual_host_guard_applies(self, jarvis_server):
        status, _ = _get(jarvis_server.port, f"/api/identity?nonce={identity.new_nonce()}", host="evil.example")
        assert status == 403

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows lets a same-user socket take a shared port")
    def test_no_other_program_can_take_the_port_while_jarvis_serves_it(self, jarvis_server):
        thief = socket.socket()
        try:
            thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                thief.bind(("127.0.0.1", jarvis_server.port))
        finally:
            thief.close()


# ---------------------------------------------------------------------------
# The desktop app checks before it shows anything
# ---------------------------------------------------------------------------

class TestVerifier:
    def test_jarvis_with_the_right_token_is_jarvis(self, jarvis_server):
        from desktop_app.web_chat_window import JARVIS, verify_web_chat
        assert verify_web_chat(jarvis_server.port, jarvis_server.identity_token) == JARVIS

    def test_a_server_from_another_run_is_not(self, jarvis_server):
        from desktop_app.web_chat_window import FOREIGN, verify_web_chat
        assert verify_web_chat(jarvis_server.port, identity.new_token()) == FOREIGN

    def test_another_program_on_the_port_is_foreign(self, foreign_port):
        from desktop_app.web_chat_window import FOREIGN, verify_web_chat
        assert verify_web_chat(foreign_port, identity.new_token()) == FOREIGN

    def test_nothing_on_the_port_is_unreachable(self, free_port):
        from desktop_app.web_chat_window import UNREACHABLE, verify_web_chat
        assert verify_web_chat(free_port, identity.new_token()) == UNREACHABLE

    def test_no_proxy_is_ever_used(self, jarvis_server, monkeypatch):
        from desktop_app.web_chat_window import JARVIS, verify_web_chat
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("NO_PROXY", "")
        assert verify_web_chat(jarvis_server.port, jarvis_server.identity_token) == JARVIS


class StubView(QWidget):
    loadFinished = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.loaded = []

    def setUrl(self, url: QUrl) -> None:
        self.loaded.append(url.toString())


def _pump(qapp, until=lambda: False, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        qapp.processEvents()
        time.sleep(0.005)
    return until()


class TestWindowAgainstRealServers:
    def _window(self, port, token):
        from desktop_app.web_chat_window import WebChatWindow
        view = StubView()
        win = WebChatWindow(port, "stopped", view_factory=lambda: view, retry_ms=30, identity=lambda: token)
        win.stub = view
        return win

    def test_jarvis_s_own_server_is_shown(self, qapp, jarvis_server):
        win = self._window(jarvis_server.port, jarvis_server.identity_token)
        try:
            win.set_daemon_status("running")
            assert _pump(qapp, lambda: win.stub.loaded, timeout=5)
            win.stub.loadFinished.emit(True)
            assert win.is_showing_chat()
        finally:
            win.close()

    def test_a_squatter_on_the_port_is_never_shown(self, qapp, foreign_port):
        win = self._window(foreign_port, identity.new_token())
        try:
            win.set_daemon_status("running")
            _pump(qapp, timeout=0.6)
            assert win.stub.loaded == []
            assert not win.is_showing_chat()
            assert "another program" in win.notice_text().lower()
        finally:
            win.close()

    def test_without_a_token_nothing_is_shown(self, qapp, jarvis_server):
        win = self._window(jarvis_server.port, None)
        try:
            win.set_daemon_status("running")
            _pump(qapp, timeout=0.3)
            assert win.stub.loaded == []
            assert not win.is_showing_chat()
        finally:
            win.close()

    def test_the_default_browser_is_only_sent_to_jarvis(self, qapp, foreign_port, jarvis_server):
        from desktop_app.web_chat_window import WebChatWindow
        squatted = WebChatWindow(foreign_port, "running", view_factory=lambda: None,
                                 identity=lambda: identity.new_token())
        ours = WebChatWindow(jarvis_server.port, "running", view_factory=lambda: None,
                             identity=lambda: jarvis_server.identity_token)
        try:
            assert squatted.verified_browser_url() is None
            assert "another program" in squatted.notice_text().lower()
            assert ours.verified_browser_url() == f"http://127.0.0.1:{jarvis_server.port}/"
        finally:
            squatted.close()
            ours.close()


# ---------------------------------------------------------------------------
# How the desktop app learns the token
# ---------------------------------------------------------------------------

class TestDaemonSharesTheToken:
    def test_a_daemon_started_by_the_desktop_app_announces_port_and_token(self, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_STDIN_IPC", "1")
        monkeypatch.delattr(sys, "frozen", raising=False)
        server = SimpleNamespace(port=8766, identity_token="t" * 64)

        daemon.announce_web_chat(server)

        lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(daemon.WEB_CHAT_EVENT_PREFIX)]
        assert [json.loads(l[len(daemon.WEB_CHAT_EVENT_PREFIX):]) for l in lines] == [
            {"type": "ready", "data": {"port": 8766, "token": "t" * 64}}]

    def test_a_console_daemon_prints_no_token(self, monkeypatch, capsys):
        monkeypatch.delenv("JARVIS_STDIN_IPC", raising=False)
        daemon.announce_web_chat(SimpleNamespace(port=8766, identity_token="t" * 64))
        assert "t" * 64 not in capsys.readouterr().out

    def test_no_server_announces_nothing(self, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_STDIN_IPC", "1")
        monkeypatch.delattr(sys, "frozen", raising=False)
        daemon.announce_web_chat(None)
        assert daemon.WEB_CHAT_EVENT_PREFIX not in capsys.readouterr().out


class TestTrayKeepsTheToken:
    def _tray(self, *, bundled=False):
        import desktop_app.app as app_mod
        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.is_bundled = bundled
        tray.chat_window = None
        tray.web_chat_window = None
        tray._chat_submit_fn = None
        tray._confirm_relay = None
        tray._web_chat_token = None
        tray.log_signals = MagicMock()
        return tray

    def _ready_line(self, token, port=8766):
        return daemon.WEB_CHAT_EVENT_PREFIX + json.dumps({"type": "ready", "data": {"port": port, "token": token}})

    def test_the_subprocess_token_is_read_from_daemon_output_and_kept_out_of_the_log(self, qapp):
        tray = self._tray()
        tray.daemon_process = SimpleNamespace(stdout=io.StringIO("hello\n" + self._ready_line("a" * 64) + "\n"))

        tray._read_daemon_logs()

        assert tray._web_chat_identity() == "a" * 64
        logged = "".join(call.args[0] for call in tray.log_signals.new_log.emit.call_args_list)
        assert "hello" in logged and "a" * 64 not in logged

    @pytest.mark.parametrize("line", [
        "__WEB_CHAT__:not json",
        "__WEB_CHAT__:" + json.dumps({"type": "ready", "data": {"port": 8766, "token": 5}}),
        "__WEB_CHAT__:" + json.dumps({"type": "ready", "data": "x"}),
    ])
    def test_a_malformed_announcement_gives_no_token(self, qapp, line):
        tray = self._tray()
        tray.daemon_process = SimpleNamespace(stdout=io.StringIO(line + "\n"))
        tray._read_daemon_logs()
        assert tray._web_chat_identity() is None

    @pytest.mark.parametrize("status", ["starting", "stopping", "stopped", "crashed"])
    def test_the_token_is_forgotten_when_the_daemon_goes_away(self, qapp, status):
        tray = self._tray()
        tray._web_chat_token = "a" * 64
        tray._set_chat_daemon_status(status)
        assert tray._web_chat_identity() is None

    def test_the_bundled_token_comes_from_the_running_server(self, qapp, monkeypatch):
        from jarvis.webchat import runtime
        tray = self._tray(bundled=True)
        monkeypatch.setattr(runtime, "current", lambda: SimpleNamespace(identity_token="b" * 64))
        assert tray._web_chat_identity() == "b" * 64
        monkeypatch.setattr(runtime, "current", lambda: None)
        assert tray._web_chat_identity() is None

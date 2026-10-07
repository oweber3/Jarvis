"""Web chat start-up: nothing when off, loopback only, a warning (not a crash) when the port is taken."""
import http.client
import socket
from types import SimpleNamespace

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.webchat import runtime


def settings(tmp_path, **values):
    base = dict(web_chat_enabled=True, web_chat_port=0, db_path=str(tmp_path / "jarvis.db"))
    base.update(values)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def stopped():
    yield
    runtime.stop()


@pytest.mark.unit
class TestRuntime:
    def test_nothing_runs_when_the_web_chat_is_off(self, tmp_path):
        assert runtime.start(settings(tmp_path, web_chat_enabled=False), backend=FakeWebChatBackend()) is None
        assert runtime.current() is None
        assert not (tmp_path / "jarvis.db").exists()

    def test_the_page_is_served_when_on(self, tmp_path, capsys):
        server = runtime.start(settings(tmp_path), backend=FakeWebChatBackend())
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/api/state")
        response = conn.getresponse()
        assert response.status == 200
        conn.close()
        assert "💬" in capsys.readouterr().out

    def test_it_listens_on_the_loopback_interface_only(self, tmp_path):
        server = runtime.start(settings(tmp_path), backend=FakeWebChatBackend())
        assert server._httpd.server_address[0] == "127.0.0.1"

    def test_chats_live_in_the_jarvis_database_file(self, tmp_path):
        server = runtime.start(settings(tmp_path), backend=FakeWebChatBackend())
        server.hub.new_chat()
        assert (tmp_path / "jarvis.db").exists()

    def test_a_taken_port_warns_and_leaves_jarvis_running(self, tmp_path, capsys):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            port = blocker.getsockname()[1]
            assert runtime.start(settings(tmp_path, web_chat_port=port), backend=FakeWebChatBackend()) is None
            assert "⚠️" in capsys.readouterr().out
            assert runtime.current() is None
        finally:
            blocker.close()

    def test_stop_closes_the_port(self, tmp_path):
        server = runtime.start(settings(tmp_path), backend=FakeWebChatBackend())
        port = server.port
        runtime.stop()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_starting_again_replaces_the_running_server(self, tmp_path):
        first = runtime.start(settings(tmp_path), backend=FakeWebChatBackend())
        first_port = first.port
        second = runtime.start(settings(tmp_path), backend=FakeWebChatBackend())
        assert runtime.current() is second
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", first_port), timeout=1).close()

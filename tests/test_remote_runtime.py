"""Phone access start-up: nothing when off, a warning (not a crash) when the port is taken."""
import http.client
import socket
from types import SimpleNamespace

import pytest

from fake_remote_backend import FakeBackend
from jarvis.remote import runtime


def settings(tmp_path, **values):
    base = dict(remote_access_enabled=True, remote_access_host="127.0.0.1", remote_access_port=0,
                remote_access_allow_confirm=True, remote_access_quick_actions=["Next song"],
                db_path=str(tmp_path / "jarvis.db"))
    base.update(values)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def stopped():
    yield
    runtime.stop()


@pytest.mark.unit
class TestRuntime:
    def test_nothing_runs_when_phone_access_is_off(self, tmp_path):
        assert runtime.start(settings(tmp_path, remote_access_enabled=False), backend=FakeBackend()) is None
        assert runtime.current() is None

    def test_the_app_is_served_when_on(self, tmp_path, capsys):
        server = runtime.start(settings(tmp_path), backend=FakeBackend())
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
        conn.close()
        assert "📱" in capsys.readouterr().out

    def test_devices_live_next_to_the_database(self, tmp_path):
        server = runtime.start(settings(tmp_path), backend=FakeBackend())
        server.store.start_pairing()
        assert (tmp_path / "remote_pairing.json").exists()

    def test_a_taken_port_warns_and_leaves_jarvis_running(self, tmp_path, capsys):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            port = blocker.getsockname()[1]
            assert runtime.start(settings(tmp_path, remote_access_port=port), backend=FakeBackend()) is None
            assert "⚠️" in capsys.readouterr().out
        finally:
            blocker.close()

    def test_stop_closes_the_port(self, tmp_path):
        server = runtime.start(settings(tmp_path), backend=FakeBackend())
        port = server.port
        runtime.stop()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

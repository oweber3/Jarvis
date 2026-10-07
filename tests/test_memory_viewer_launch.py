"""The source-run memory viewer server never stalls on its own output.

The server child writes a start-up banner and a line per request. Its output
goes to a log file in the log directory, so however much it writes it keeps
answering, and a start-up failure still shows what the child printed.
"""
import socket
import sys

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def viewer(qapp, tmp_path, monkeypatch):
    """A memory viewer window with no web engine, logging into ``tmp_path``."""
    import desktop_app.app as app_mod
    import desktop_app.paths as paths_mod

    monkeypatch.setattr(app_mod, "HAS_WEBENGINE", False)
    monkeypatch.setattr(paths_mod, "get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    window = app_mod.MemoryViewerWindow()
    window.MEMORY_VIEWER_PORT = _free_port()
    yield window
    window.stop_server()


@pytest.mark.unit
def test_server_that_writes_a_lot_still_starts(viewer):
    """Output beyond any pipe buffer does not block the server before it listens."""
    port = viewer.MEMORY_VIEWER_PORT
    child = (
        "import socket, sys, time\n"
        "for _ in range(40000):\n"
        "    sys.stdout.write('GET /api/search?q=x HTTP/1.1 200 -\\n')\n"
        "sys.stdout.flush()\n"
        "s = socket.socket()\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen()\n"
        "time.sleep(60)\n"
    )
    viewer._server_command = lambda: [sys.executable, "-c", child]

    assert viewer.start_server() is True


@pytest.mark.unit
def test_start_up_failure_shows_what_the_server_printed(viewer, capsys):
    viewer._server_command = lambda: [
        sys.executable, "-c", "import sys; print('memory viewer exploded'); sys.exit(3)",
    ]

    assert viewer.start_server() is False
    assert "memory viewer exploded" in capsys.readouterr().out


@pytest.mark.unit
def test_server_does_not_log_each_request():
    """Request lines (which carry memory search terms) stay out of the log file."""
    import logging

    pytest.importorskip("flask")
    from desktop_app import memory_viewer

    logger = logging.getLogger("werkzeug")
    before = logger.level
    try:
        memory_viewer.quiet_request_log()
        assert not logger.isEnabledFor(logging.INFO)
    finally:
        logger.setLevel(before)

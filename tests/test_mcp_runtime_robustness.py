"""Persistent MCP runtime against real (fake) stdio servers: timeouts, wedged
sessions, setup failures and shutdown. See ``mcp_runtime.spec.md``.

The servers are small Python child scripts built on the ``mcp`` SDK's
FastMCP; every process a test starts is killed afterwards whatever the
runtime did.
"""

from __future__ import annotations

import concurrent.futures
import sys
import textwrap
import threading
import time

import pytest

psutil = pytest.importorskip("psutil")
pytestmark = pytest.mark.unit


_FAKE_SERVER = textwrap.dedent(
    '''
    import asyncio, os, sys
    from pathlib import Path
    from mcp.server.fastmcp import FastMCP

    work = Path(sys.argv[1])
    (work / f"pid-{os.getpid()}").write_text(str(os.getpid()))
    mcp = FastMCP("fake")

    @mcp.tool()
    async def hang() -> str:
        """Block until the release file appears."""
        (work / "hanging").write_text("x")
        while not (work / "release").exists():
            await asyncio.sleep(0.05)
        return "released"

    @mcp.tool()
    def record(text: str) -> str:
        """Side effect: append text to a log file."""
        with open(work / "actions.log", "a", encoding="utf-8") as fh:
            fh.write(text + "\\n")
        return "recorded " + text

    @mcp.tool()
    def echo(text: str) -> str:
        """Return the text."""
        return text

    mcp.run()
    '''
)

_SILENT_SERVER = textwrap.dedent(
    '''
    import os, sys, time
    from pathlib import Path
    work = Path(sys.argv[1])
    (work / f"pid-{os.getpid()}").write_text(str(os.getpid()))
    # Never answers the MCP initialize handshake (a slow download or a
    # wedged server) but stays alive.
    while True:
        time.sleep(0.2)
    '''
)


def _server_cfg(tmp_path, source, **extra):
    script = tmp_path / "server.py"
    script.write_text(source, encoding="utf-8")
    cfg = {"command": sys.executable, "args": [str(script), str(tmp_path)]}
    cfg.update(extra)
    return cfg


def _spawned_pids(tmp_path):
    return sorted(int(p.read_text()) for p in tmp_path.glob("pid-*"))


def _wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _alive(pid):
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _logged(tmp_path):
    log = tmp_path / "actions.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.fixture
def runtime_cleanup(tmp_path):
    yield
    from jarvis.tools.external.mcp_runtime import shutdown_runtime

    (tmp_path / "release").write_text("x")
    shutdown_runtime()
    for pid in _spawned_pids(tmp_path):
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


def _in_background(fn):
    """Run ``fn`` on a thread; return a future with its result or exception."""
    fut: concurrent.futures.Future = concurrent.futures.Future()

    def runner():
        try:
            fut.set_result(fn())
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)

    threading.Thread(target=runner, daemon=True).start()
    return fut


def test_server_recovers_after_a_timed_out_call(tmp_path, runtime_cleanup):
    """A call past ``timeout_sec`` raises TimeoutError; the stuck session is
    replaced (its subprocess ends) and the next call gets a fresh one."""
    from jarvis.tools.external.mcp_client import MCPClient

    client = MCPClient({"fake": _server_cfg(tmp_path, _FAKE_SERVER, timeout_sec=2)})

    with pytest.raises(concurrent.futures.TimeoutError):
        client.invoke_tool("fake", "hang", {})
    (first_pid,) = _spawned_pids(tmp_path)

    assert client.invoke_tool("fake", "echo", {"text": "still here"})["text"] == "still here"
    assert _wait_for(lambda: not _alive(first_pid), timeout=10), "the stuck server was left running"


def test_call_that_timed_out_while_queued_never_runs(tmp_path, runtime_cleanup):
    """A request that waited behind a slow call and timed out is reported as
    failed, so it must not run when the server becomes free."""
    from jarvis.tools.external.mcp_runtime import get_runtime

    cfg = _server_cfg(tmp_path, _FAKE_SERVER)
    runtime = get_runtime()
    slow = _in_background(lambda: runtime.invoke("fake", cfg, "hang", {}, timeout=30))
    assert _wait_for((tmp_path / "hanging").exists)

    with pytest.raises(concurrent.futures.TimeoutError):
        runtime.invoke("fake", cfg, "record", {"text": "send-the-email"}, timeout=0.5)

    (tmp_path / "release").write_text("x")
    assert slow.result(timeout=10).content[0].text == "released"
    # Anything still queued has been served once this call answers.
    runtime.invoke("fake", cfg, "echo", {"text": "after"}, timeout=10)
    assert _logged(tmp_path) == []


def test_call_queued_behind_a_hung_call_runs_once_on_a_fresh_session(tmp_path, runtime_cleanup):
    """When the call in flight times out, a request still waiting behind it
    (whose caller is still waiting) runs exactly once, on a fresh session,
    and the calls never overlap."""
    from jarvis.tools.external.mcp_runtime import get_runtime

    cfg = _server_cfg(tmp_path, _FAKE_SERVER)
    runtime = get_runtime()
    hung = _in_background(lambda: runtime.invoke("fake", cfg, "hang", {}, timeout=2))
    assert _wait_for((tmp_path / "hanging").exists)
    waiting = _in_background(
        lambda: runtime.invoke("fake", cfg, "record", {"text": "once"}, timeout=20))

    with pytest.raises(concurrent.futures.TimeoutError):
        hung.result(timeout=10)
    assert waiting.result(timeout=20).content[0].text == "recorded once"
    assert _logged(tmp_path) == ["once"]
    assert len(_spawned_pids(tmp_path)) == 2


def test_setup_timeout_does_not_leave_server_processes(tmp_path, monkeypatch, runtime_cleanup):
    """A server that misses the handshake window fails the call and its
    half-started subprocess is stopped, on every attempt."""
    from jarvis.tools.external import mcp_runtime
    from jarvis.tools.external.mcp_client import MCPClient

    monkeypatch.setattr(mcp_runtime, "_SETUP_TIMEOUT_SEC", 1.5)
    client = MCPClient({"silent": _server_cfg(tmp_path, _SILENT_SERVER)})

    for _ in range(2):
        with pytest.raises(concurrent.futures.TimeoutError):
            client.list_tools("silent")

    assert _wait_for(lambda: len(_spawned_pids(tmp_path)) == 2)
    _wait_for(lambda: not any(_alive(p) for p in _spawned_pids(tmp_path)), timeout=10)
    assert [p for p in _spawned_pids(tmp_path) if _alive(p)] == []


def test_shutdown_runtime_stops_server_process(tmp_path, runtime_cleanup):
    """Daemon shutdown asks each worker to exit and its subprocess ends."""
    from jarvis.tools.external.mcp_client import MCPClient
    from jarvis.tools.external.mcp_runtime import shutdown_runtime

    client = MCPClient({"fake": _server_cfg(tmp_path, _FAKE_SERVER)})
    assert client.invoke_tool("fake", "echo", {"text": "hi"})["text"] == "hi"
    pids = _spawned_pids(tmp_path)
    assert pids

    shutdown_runtime()

    _wait_for(lambda: not any(_alive(p) for p in pids), timeout=10)
    assert [p for p in pids if _alive(p)] == []

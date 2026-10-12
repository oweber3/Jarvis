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
    if os.environ.get("JARVIS_TEST_DECOY"):
        # Another program running the very same server command line.
        import time
        (work / "decoy-pid").write_text(str(os.getpid()))
        while True:
            time.sleep(0.2)
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

    @mcp.tool()
    async def slow_effect() -> str:
        """Act later from a thread that keeps this process alive after its
        stdin closes (as a server finishing work in the background would)."""
        import threading, time

        def act():
            while not (work / "go").exists():
                time.sleep(0.05)
            with open(work / "actions.log", "a", encoding="utf-8") as fh:
                fh.write("slow\\n")

        threading.Thread(target=act).start()
        while True:
            await asyncio.sleep(0.05)

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
    (tmp_path / "go").write_text("x")
    shutdown_runtime()
    decoy = tmp_path / "decoy-pid"
    for pid in _spawned_pids(tmp_path) + ([int(decoy.read_text())] if decoy.exists() else []):
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


def _timed_out_server_then_next_call(tmp_path, cfg):
    """A call times out while its server keeps running; return the old
    server's pid, whether it was still alive when the next call answered,
    and that answer."""
    from jarvis.tools.external.mcp_runtime import get_runtime

    runtime = get_runtime()
    with pytest.raises(concurrent.futures.TimeoutError):
        runtime.invoke("fake", cfg, "slow_effect", {}, timeout=1)
    (old_pid,) = _spawned_pids(tmp_path)
    answer = runtime.invoke("fake", cfg, "record", {"text": "fast"}, timeout=30)
    return old_pid, _alive(old_pid), answer


def test_a_timed_out_call_never_acts_alongside_or_after_the_next_call(tmp_path, runtime_cleanup):
    """The replacement session starts only once the stuck one has ended, so
    the timed-out call's side effect can never land after the next call's."""
    cfg = _server_cfg(tmp_path, _FAKE_SERVER)
    old_pid, old_alive, answer = _timed_out_server_then_next_call(tmp_path, cfg)

    assert answer.content[0].text == "recorded fast"
    assert not old_alive, "the old server was still running when the next call ran"
    (tmp_path / "go").write_text("x")  # the old server's background work would act now
    time.sleep(1.0)
    assert _logged(tmp_path) == ["fast"]


def test_a_session_that_will_not_stop_is_killed_before_its_replacement_starts(
    tmp_path, monkeypatch, runtime_cleanup
):
    """If the old session does not end within the stop window, its process
    tree is killed rather than left running beside the replacement."""
    import mcp.client.stdio as stdio

    from jarvis.tools.external import mcp_runtime

    monkeypatch.setattr(stdio, "PROCESS_TERMINATION_TIMEOUT", 60.0)
    monkeypatch.setattr(mcp_runtime, "_STOP_WAIT_SEC", 1.0)
    cfg = _server_cfg(tmp_path, _FAKE_SERVER)
    started = time.monotonic()
    old_pid, old_alive, answer = _timed_out_server_then_next_call(tmp_path, cfg)

    assert answer.content[0].text == "recorded fast"
    assert not old_alive and time.monotonic() - started < 30


def _start_decoy(tmp_path, cfg):
    """Start the server's exact command line as a process outside this test's process tree (its launcher
    exits, so it is nobody's descendant here), as another Jarvis running the same server would be."""
    import os
    import subprocess

    launcher = ("import subprocess, sys; subprocess.Popen(sys.argv[1:], close_fds=True, "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
                "creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0), start_new_session=True)")
    subprocess.run([sys.executable, "-c", launcher, cfg["command"], *cfg["args"]], check=True,
                   env={**os.environ, "JARVIS_TEST_DECOY": "1"}, timeout=30)
    assert _wait_for((tmp_path / "decoy-pid").exists)
    decoy = int((tmp_path / "decoy-pid").read_text())
    ours = {proc.pid for proc in psutil.Process().children(recursive=True)}
    assert decoy not in ours and _alive(decoy)
    return decoy


def test_the_stop_kill_reaches_only_our_own_server_never_another_programs(
    tmp_path, monkeypatch, runtime_cleanup
):
    """Another Jarvis may run the very same server command line; only our own stuck server is killed."""
    import mcp.client.stdio as stdio

    from jarvis.tools.external import mcp_runtime

    monkeypatch.setattr(stdio, "PROCESS_TERMINATION_TIMEOUT", 60.0)
    monkeypatch.setattr(mcp_runtime, "_STOP_WAIT_SEC", 1.0)
    cfg = _server_cfg(tmp_path, _FAKE_SERVER)
    decoy = _start_decoy(tmp_path, cfg)

    old_pid, old_alive, answer = _timed_out_server_then_next_call(tmp_path, cfg)

    assert answer.content[0].text == "recorded fast" and not old_alive
    assert _alive(decoy), "a process outside Jarvis's own tree was killed"
    # Even named directly, a process that is not our descendant is never killed.
    mcp_runtime._kill_process_trees([(decoy, psutil.Process(decoy).create_time())])
    time.sleep(0.5)
    assert _alive(decoy)


def test_a_recorded_process_id_reused_by_another_process_is_never_killed():
    """The kill checks the recorded start time, so a PID reused since the session started is left alone."""
    import subprocess

    from jarvis.tools.external import mcp_runtime

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = psutil.Process(child.pid).create_time()
        mcp_runtime._kill_process_trees([(child.pid, created + 5)])
        time.sleep(0.5)
        assert child.poll() is None
        mcp_runtime._kill_process_trees([(child.pid, created)])
        assert child.wait(timeout=10) is not None
    finally:
        child.kill()


def test_a_caller_cancelling_during_the_drain_never_stops_it(monkeypatch, runtime_cleanup):
    """The worker resolves queued requests when its session ends; a caller
    giving up at that same moment must not stop the others being resolved."""
    import asyncio

    from jarvis.tools.external import mcp_runtime

    in_flight = threading.Event()

    class Conn:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            in_flight.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("jarvis.tools.external.mcp_client._resolve_command", lambda c: c)
    monkeypatch.setattr("jarvis.tools.external.mcp_client.stdio_client", lambda params, **kw: Conn())
    monkeypatch.setattr("jarvis.tools.external.mcp_client.ClientSession", Session)

    class CancelledMeanwhile(concurrent.futures.Future):
        """Its caller cancels it just after the worker saw it was not done."""

        def set_exception(self, exception):
            self.cancel()
            return super().set_exception(exception)

    runtime = mcp_runtime.get_runtime()
    worker = runtime._get_worker("blocking", {"command": "fake"})
    _in_background(lambda: worker.invoke("block", {}, 30))
    assert in_flight.wait(5)
    racing, waiting = CancelledMeanwhile(), concurrent.futures.Future()
    for fut in (racing, waiting):
        worker._loop.call_soon_threadsafe(worker._queue.put_nowait, ("call", ("x", {}), fut))

    worker.shutdown()

    assert isinstance(waiting.exception(timeout=5), mcp_runtime._WorkerDeadError)


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

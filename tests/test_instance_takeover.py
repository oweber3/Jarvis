"""Close Existing & Start New" stops the old instance and everything it started.

Every process here is a fake: the tests never terminate a real process.
"""
import psutil
import pytest


class FakeProcess:
    def __init__(self, pid, name, children=(), stubborn=False):
        self.pid = pid
        self._name = name
        self._children = list(children)
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def name(self):
        return self._name

    def children(self, recursive=False):
        if not recursive:
            return list(self._children)
        found = []
        for child in self._children:
            found.append(child)
            found.extend(child.children(recursive=True))
        return found

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    @property
    def stopped(self):
        return self.killed or (self.terminated and not self.stubborn)

    def wait(self, timeout=None):
        if not self.stopped:
            raise psutil.TimeoutExpired(timeout, self.pid)
        return 0


@pytest.fixture
def processes(monkeypatch, tmp_path):
    """A fake process table and a log directory in ``tmp_path``."""
    import desktop_app.app as app_mod
    import desktop_app.paths as paths_mod

    table = {}

    def lookup(pid):
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        return table[pid]

    monkeypatch.setattr(app_mod.psutil, "Process", lookup)
    monkeypatch.setattr(paths_mod, "get_log_dir", lambda: tmp_path)
    return table


def _old_instance(table, *, stubborn_viewer=False):
    runner = FakeProcess(30, "llama-server.exe")
    owned_ollama = FakeProcess(20, "ollama.exe", children=[runner])
    viewer = FakeProcess(10, "python.exe", stubborn=stubborn_viewer)
    jarvis = FakeProcess(1, "Jarvis.exe", children=[viewer, owned_ollama])
    users_ollama = FakeProcess(99, "ollama.exe")
    for proc in (jarvis, viewer, owned_ollama, runner, users_ollama):
        table[proc.pid] = proc
    return jarvis, viewer, owned_ollama, runner, users_ollama


@pytest.mark.unit
def test_closing_the_old_instance_stops_what_it_started(processes):
    from desktop_app.app import kill_existing_instance

    jarvis, viewer, owned_ollama, runner, users_ollama = _old_instance(processes)

    assert kill_existing_instance(jarvis.pid) is True
    assert jarvis.stopped
    assert viewer.stopped, "the memory viewer child must not keep holding its port"
    assert owned_ollama.stopped and runner.stopped, "an Ollama the old instance launched goes with it"
    assert not users_ollama.terminated and not users_ollama.killed, "an Ollama the user started is left alone"


@pytest.mark.unit
def test_apps_jarvis_opened_for_the_user_keep_running(processes):
    """Word, a browser or a game Jarvis launched are its children too, and may hold unsaved work."""
    from desktop_app.app import kill_existing_instance

    word = FakeProcess(40, "WINWORD.EXE")
    game = FakeProcess(41, "game.exe")
    mcp_server = FakeProcess(42, "node.exe")
    daemon = FakeProcess(12, "python.exe", children=[word, mcp_server])
    jarvis = FakeProcess(1, "Jarvis.exe", children=[daemon, game])
    for proc in (jarvis, daemon, word, game, mcp_server):
        processes[proc.pid] = proc

    assert kill_existing_instance(jarvis.pid) is True
    assert daemon.stopped and mcp_server.stopped
    assert not word.terminated and not word.killed
    assert not game.terminated and not game.killed


@pytest.mark.unit
def test_a_child_that_cannot_be_stopped_does_not_abort_the_takeover(processes, monkeypatch):
    from desktop_app.app import get_crash_paths, kill_existing_instance

    _, crash_marker, _ = get_crash_paths()
    crash_marker.touch()
    jarvis, viewer, owned_ollama, runner, _ = _old_instance(processes)

    def denied():
        raise psutil.AccessDenied(owned_ollama.pid)

    monkeypatch.setattr(owned_ollama, "terminate", denied)
    monkeypatch.setattr(owned_ollama, "kill", denied)

    assert kill_existing_instance(jarvis.pid) is True
    assert jarvis.stopped and viewer.stopped and runner.stopped
    assert not crash_marker.exists()


@pytest.mark.unit
def test_a_child_that_ignores_terminate_is_killed(processes):
    from desktop_app.app import kill_existing_instance

    jarvis, viewer, *_ = _old_instance(processes, stubborn_viewer=True)

    assert kill_existing_instance(jarvis.pid) is True
    assert viewer.killed


@pytest.mark.unit
def test_the_new_instance_never_stops_itself(processes):
    """Even when the new instance was started from inside the old one."""
    import os

    from desktop_app.app import kill_existing_instance

    jarvis, *_ = _old_instance(processes)
    me = FakeProcess(os.getpid(), "python.exe")
    jarvis._children.append(me)

    assert kill_existing_instance(jarvis.pid) is True
    assert not me.terminated and not me.killed


@pytest.mark.unit
def test_a_deliberate_close_is_not_reported_as_a_crash(processes):
    from desktop_app.app import check_previous_crash, get_crash_paths, kill_existing_instance

    crash_log, crash_marker, _ = get_crash_paths()
    crash_marker.touch()
    crash_log.write_text("Traceback: the old session was still running", encoding="utf-8")
    jarvis, *_ = _old_instance(processes)

    assert kill_existing_instance(jarvis.pid) is True
    assert check_previous_crash() is None


@pytest.mark.unit
def test_a_process_that_is_not_jarvis_is_left_alone(processes):
    from desktop_app.app import get_crash_paths, kill_existing_instance

    _, crash_marker, _ = get_crash_paths()
    crash_marker.touch()
    child = FakeProcess(11, "python.exe")
    other = FakeProcess(5, "notepad.exe", children=[child])
    processes.update({5: other, 11: child})

    assert kill_existing_instance(5) is False
    assert not other.terminated and not child.terminated
    assert crash_marker.exists()

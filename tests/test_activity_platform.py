"""Windows side of the activity log: event pacing, idle time, foreground snapshots and real window events.

The real-event tests use a test-owned window far off-screen, created without activation (the UI
Automation fixture app). It is never activated, never given input and never covers anything. Its
title is changed from this process, and the watcher is told to treat it as the foreground window, so
the hook plumbing is exercised without changing what the user is looking at.
"""
import contextlib
import queue
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from jarvis.memory.activity_log import ActivityStore
from jarvis.platform.windows import activity

win32_only = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows hooks")
_APP = Path(__file__).parent / "fixtures" / "uia_fixture_app.py"


@pytest.mark.unit
class TestEventGate:
    def gate(self):
        return activity.EventGate(tick_sec=5.0, title_throttle_sec=2.0)

    def test_a_foreground_change_fires_at_once(self):
        g = self.gate()
        assert g.due(0.0) == "tick"  # nothing seen yet: the first call is the initial observation
        g.note("foreground")
        assert g.due(0.1) == "foreground"
        assert g.due(0.2) is None

    def test_title_changes_inside_the_throttle_window_coalesce_into_one_event(self):
        g = self.gate()
        g.due(0.0)
        g.note("title")
        assert g.due(0.5) is None
        g.note("title")
        g.note("title")
        assert g.due(1.9) is None
        assert g.due(2.1) == "title"
        assert g.due(2.2) is None

    def test_a_foreground_change_wins_over_a_pending_title(self):
        g = self.gate()
        g.due(0.0)
        g.note("title")
        g.note("foreground")
        assert g.due(0.1) == "foreground"
        assert g.due(0.2) is None

    def test_silence_ticks_so_idle_and_missed_events_are_caught(self):
        g = self.gate()
        g.due(0.0)
        assert g.due(4.9) is None
        assert g.due(5.0) == "tick"
        assert g.due(9.0) is None
        assert g.due(10.0) == "tick"

    def test_the_wait_shrinks_when_a_title_is_pending_and_never_hits_zero(self):
        g = self.gate()
        g.due(0.0)
        assert g.wait_sec(1.0) == pytest.approx(4.0)
        g.note("title")
        assert g.wait_sec(1.0) == pytest.approx(1.0)
        assert g.wait_sec(5.0) >= 0.05


@pytest.mark.unit
@win32_only
class TestReaders:
    def test_idle_seconds_is_a_small_non_negative_number(self):
        idle = activity.idle_seconds()
        assert isinstance(idle, float) and 0.0 <= idle < 10 * 365 * 86400

    def test_no_window_gives_no_observation(self):
        assert activity.foreground_snapshot(0) is None


@contextlib.contextmanager
def offscreen_window(tmp_path):
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32")
    user.FindWindowW.restype = wintypes.HWND
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]

    title = "Jarvis activity fixture " + uuid.uuid4().hex
    log_path = tmp_path / "events.log"
    log_path.write_text("", encoding="utf-8")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
    process = subprocess.Popen([sys.executable, str(_APP), title, str(log_path)], startupinfo=startup,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    hwnd = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not hwnd:
            if "ready" in log_path.read_text(encoding="utf-8"):
                hwnd = user.FindWindowW("JarvisUiaFixture", title)
            time.sleep(0.05)
        assert hwnd, "The fixture window did not appear"
        yield int(hwnd), lambda text: user.SetWindowTextW(hwnd, text)
        assert user.GetForegroundWindow() != hwnd, "The fixture window was activated"
    finally:
        if hwnd and process.poll() is None:
            user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


@pytest.mark.integration
@win32_only
class TestRealWindowEvents:
    def test_snapshot_reads_the_title_and_process_of_a_window(self, tmp_path):
        with offscreen_window(tmp_path) as (hwnd, set_title):
            set_title("Quarterly plan - draft")
            snap = wait_for(lambda: (s := activity.foreground_snapshot(hwnd)) and
                            s.title == "Quarterly plan - draft" and s)
            assert snap, "title was not readable"
            assert snap.process.lower().startswith("python")
            assert snap.app

    def test_title_changes_of_the_foreground_window_reach_the_callback(self, tmp_path):
        with offscreen_window(tmp_path) as (hwnd, set_title):
            events = queue.Queue()
            watcher = activity.ForegroundWatcher(events.put, foreground_provider=lambda: hwnd,
                                                 tick_sec=60.0, title_throttle_sec=0.2)
            assert watcher.start()
            try:
                assert wait_for(lambda: not events.empty()), "the initial observation never arrived"
                while not events.empty():
                    events.get_nowait()
                set_title("Draft one")
                kinds = []

                def seen():
                    while not events.empty():
                        kinds.append(events.get_nowait())
                    return "title" in kinds

                assert wait_for(seen), f"no title event arrived: {kinds}"
            finally:
                watcher.stop()

    def test_events_for_a_window_that_is_not_in_the_foreground_are_ignored(self, tmp_path):
        with offscreen_window(tmp_path) as (hwnd, set_title):
            events = queue.Queue()
            watcher = activity.ForegroundWatcher(events.put, foreground_provider=lambda: 0,
                                                 tick_sec=60.0, title_throttle_sec=0.1)
            assert watcher.start()
            try:
                assert wait_for(lambda: not events.empty()), "the initial observation never arrived"
                while not events.empty():
                    events.get_nowait()
                set_title("Should not be noticed")
                time.sleep(1.0)
                # A real foreground change by whoever is using the PC may report "foreground"; the
                # off-screen window's title change must never be reported as a title event.
                kinds = []
                while not events.empty():
                    kinds.append(events.get_nowait())
                assert "title" not in kinds, kinds
            finally:
                watcher.stop()

    def test_the_watcher_stops_promptly_and_can_be_stopped_twice(self):
        watcher = activity.ForegroundWatcher(lambda kind: None, tick_sec=60.0)
        assert watcher.start()
        started = time.monotonic()
        watcher.stop()
        watcher.stop()
        assert time.monotonic() - started < 3.0

    def test_the_whole_pipeline_records_title_changes_as_sessions(self, tmp_path):
        from jarvis.memory import activity_runtime
        from types import SimpleNamespace
        with offscreen_window(tmp_path) as (hwnd, set_title):
            cfg = SimpleNamespace(
                activity_log_enabled=True, activity_log_paused=False, activity_log_retention_days=30,
                activity_log_idle_after_sec=300.0, activity_log_excluded_processes=[],
                activity_log_private_title_markers=[], db_path=str(tmp_path / "jarvis.db"))
            service = activity_runtime.start(
                cfg,
                foreground=lambda: activity.foreground_snapshot(hwnd),
                idle=lambda: 0.0,
                watcher_factory=lambda on_event: activity.ForegroundWatcher(
                    on_event, foreground_provider=lambda: hwnd, tick_sec=0.3, title_throttle_sec=0.2))
            try:
                assert service is not None
                set_title("Budget review")
                time.sleep(2.5)
                set_title("Holiday photos")
                time.sleep(2.5)
            finally:
                activity_runtime.stop()

            store = ActivityStore(cfg.db_path)
            titles = [s.title for s in store.sessions(0, time.time() + 60)]
            store.close()
            assert "Budget review" in titles and "Holiday photos" in titles

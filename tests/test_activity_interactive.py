"""Needs a person at the keyboard: real foreground changes. Excluded from the default run.

    PYTHONPATH=src .mamba_env/python.exe -m pytest tests/test_activity_interactive.py -m interactive -s

The test reads the real foreground window (process and application name only; it prints no title) and
asks you to switch to a different window, which a hook cannot be told to imitate.
"""
import queue
import sys
import time

import pytest

pytestmark = [pytest.mark.interactive, pytest.mark.skipif(sys.platform != "win32", reason="Native Windows hooks")]


def test_the_real_foreground_window_is_readable():
    from jarvis.platform.windows import activity

    snap = activity.foreground_snapshot()
    assert snap is not None and snap.process and snap.app


def test_switching_windows_is_reported_as_a_foreground_event():
    from jarvis.platform.windows import activity

    events = queue.Queue()
    watcher = activity.ForegroundWatcher(events.put, tick_sec=60.0)
    assert watcher.start()
    try:
        events.get(timeout=5)  # the initial observation
        print("\nSwitch to a different window now (you have 20 seconds)...", flush=True)
        deadline = time.monotonic() + 20
        kinds = []
        while time.monotonic() < deadline and "foreground" not in kinds:
            try:
                kinds.append(events.get(timeout=1))
            except queue.Empty:
                pass
        assert "foreground" in kinds, f"no foreground event arrived: {kinds}"
    finally:
        watcher.stop()

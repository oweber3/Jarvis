"""The window the user is looking at, read from the real desktop (interactive: it moves focus).

Every window here is created by the test, in a new child process or in the test process itself, and
closed by the test; no other window is touched. The test process stands in for Jarvis, so its own
window must never be reported. Run manually on a Windows desktop:

    .mamba_env/python.exe -m pytest -m interactive tests/system_controls/test_foreground_real.py
"""
import os
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = [pytest.mark.interactive,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop only")]

_CHILD = """
import sys, tkinter
root = tkinter.Tk()
root.title(sys.argv[1])
root.geometry('360x200+200+200')
root.after(30000, root.destroy)
root.mainloop()
"""


def _window_of(pid, timeout=10.0):
    from jarvis.platform.windows.windows_mgmt import list_windows
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [w for w in list_windows() if w.pid == pid]
        if found:
            return found[0]
        time.sleep(0.1)
    raise AssertionError("the test window did not appear")


@pytest.fixture
def child_window():
    """A window in a brand-new process (not Jarvis's), closed afterwards."""
    title = f"jarvis-foreground-test-{uuid.uuid4().hex[:8]}"
    process = subprocess.Popen([sys.executable, "-c", _CHILD, title])
    try:
        window = _window_of(process.pid)
        assert window.title == title  # it is ours, not a window that happened to be there
        yield window
    finally:
        process.terminate()
        process.wait(10)


@pytest.fixture
def own_window():
    """A window of this process, standing in for Jarvis's chat window."""
    import tkinter
    root = tkinter.Tk()
    root.title(f"jarvis-own-test-{uuid.uuid4().hex[:8]}")
    root.geometry("360x200+260+260")
    root.update()
    try:
        yield root, _window_of(os.getpid())
    finally:
        root.destroy()


def test_the_window_in_front_is_reported_without_its_title(child_window):
    from jarvis.platform.windows import ui_automation, windows_mgmt
    assert windows_mgmt._focus_window(child_window.hwnd), "Windows refused to focus the test window"
    target = ui_automation.foreground_target()
    assert target["hwnd"] == child_window.hwnd
    assert target["process"].lower().startswith("python")
    assert target["monitor"] and target["state"] == "normal"
    assert "title" not in target and child_window.title not in repr(target)


def test_jarvis_in_front_means_the_window_behind_it(child_window, own_window):
    from jarvis.platform.windows import ui_automation, windows_mgmt
    root, mine = own_window
    assert windows_mgmt._focus_window(child_window.hwnd)
    assert windows_mgmt._focus_window(mine.hwnd), "Windows refused to focus the stand-in Jarvis window"
    root.update()
    target = ui_automation.foreground_target()
    assert target is not None and target["hwnd"] != mine.hwnd
    assert target["hwnd"] == child_window.hwnd  # the window the user was in, just behind Jarvis's

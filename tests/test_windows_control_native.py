"""Windows integration checks using a disposable application window."""
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

_HELPER = '''import sys
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QTimer
app = QApplication([])
window = QWidget()
window.setWindowTitle(sys.argv[1])
window.resize(320, 200)
window.show()
QTimer.singleShot(30000, app.quit)
app.exec()
'''


@contextlib.contextmanager
def disposable_window(tmp_path):
    """Yield the handle of a throwaway Qt window; close it gracefully afterwards."""
    from jarvis.platform.windows.windows_mgmt import control_window, list_windows
    title = 'Jarvis control test ' + uuid.uuid4().hex
    helper = tmp_path / 'window.py'
    helper.write_text(_HELPER)
    env = dict(os.environ)
    env.pop('QT_QPA_PLATFORM', None)
    process = subprocess.Popen([sys.executable, str(helper), title], env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    hwnd = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            matches = [window for window in list_windows() if window.title == title]
            if matches:
                hwnd = matches[0].hwnd
                break
            time.sleep(.1)
        assert hwnd is not None, 'Disposable test window did not appear'
        yield hwnd, process
    finally:
        if hwnd and process.poll() is None:
            control_window('close', str(hwnd))
            process.wait(timeout=10)


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows APIs')
@pytest.mark.integration
def test_native_window_states_and_graceful_close(tmp_path):
    from jarvis.platform.windows.windows_mgmt import control_window, _user32
    with disposable_window(tmp_path) as (hwnd, process):
        user = _user32()
        control_window('minimise', str(hwnd))
        assert user.IsIconic(hwnd)
        control_window('focus', str(hwnd))
        assert user.GetForegroundWindow() == hwnd
        assert not user.IsIconic(hwnd)
        control_window('maximise', str(hwnd))
        assert user.IsZoomed(hwnd)
        control_window('restore', str(hwnd))
        assert not user.IsIconic(hwnd) and not user.IsZoomed(hwnd)
        control_window('close', str(hwnd))
        assert process.wait(timeout=10) == 0


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows APIs')
@pytest.mark.integration
def test_native_monitors_are_discovered_with_work_areas():
    from jarvis.platform.windows.displays import list_monitors
    monitors = list_monitors()
    assert monitors, 'No displays were enumerated'
    assert sum(monitor.primary for monitor in monitors) == 1
    assert len({monitor.device for monitor in monitors}) == len(monitors)
    for monitor in monitors:
        left, top, right, bottom = monitor.bounds
        work = monitor.work_area
        assert right > left and bottom > top
        assert left <= work[0] < work[2] <= right and top <= work[1] < work[3] <= bottom


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows APIs')
@pytest.mark.integration
def test_native_window_is_placed_on_every_monitor_and_zone(tmp_path):
    """Place a real window on each live monitor; measure where Windows put it."""
    from jarvis.platform.windows import displays, windows_mgmt as wm
    monitors = displays.list_monitors()
    if len(monitors) < 2:
        pytest.skip('Only one display is connected: multi-monitor placement was not exercised')
    tolerance = wm.PLACEMENT_TOLERANCE_PX
    with disposable_window(tmp_path) as (hwnd, _process):
        for monitor in monitors:
            for zone in ([0, 0, 0.5, 1], [0.5, 0.25, 0.5, 0.5]):
                expected = displays.zone_rectangle(monitor, zone)
                result = wm.place_window(hwnd, monitor, expected)
                with displays.per_monitor_dpi():
                    measured = wm._visible_rect(hwnd)
                    device = displays.monitor_device_for_window(hwnd)
                assert device == monitor.device
                assert all(abs(a - b) <= tolerance for a, b in zip(measured, expected)), (measured, expected)
                assert result['rectangle'] == list(measured)
            moved = wm.place_window(hwnd, monitor)
            work = monitor.work_area
            assert work[0] - tolerance <= moved['rectangle'][0] and moved['rectangle'][2] <= work[2] + tolerance
            wm.place_window(hwnd, monitor, state='maximise')
            assert wm._user32().IsZoomed(hwnd)
            with displays.per_monitor_dpi():
                assert displays.monitor_device_for_window(hwnd) == monitor.device
                assert all(abs(a - b) <= tolerance for a, b in zip(wm._visible_rect(hwnd), monitor.work_area))
            wm.control_window('restore', str(hwnd))


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows APIs')
@pytest.mark.integration
def test_native_placement_leaves_the_threads_dpi_context_unchanged():
    import ctypes
    from jarvis.platform.windows import displays, windows_mgmt as wm
    user = ctypes.WinDLL('user32')
    user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    before = user.GetThreadDpiAwarenessContext()
    displays.list_monitors()
    with pytest.raises(OSError):
        wm.place_window(0, displays.list_monitors()[0])
    assert user.AreDpiAwarenessContextsEqual(before, user.GetThreadDpiAwarenessContext())


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows APIs')
@pytest.mark.parametrize('name', ['Desktop', 'Documents', 'Downloads'])
def test_native_known_folders_exist(name):
    from jarvis.platform.windows.files import known_folder
    assert Path(known_folder(name)).is_dir()

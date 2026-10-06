"""The window the user is looking at: never Jarvis's own, never the desktop or taskbar, never a title.

The OS reads are replaced by a small fake desktop; ``tests/system_controls/test_foreground_real.py``
checks the same against real windows (interactive)."""
import pytest

from jarvis.platform.windows import ui_automation as uia


class FakeDesktop:
    """Windows in z-order (top first): hwnd -> (pid, class name, process, exe description)."""

    def __init__(self):
        self.windows = {
            10: (900, "Chrome_WidgetWin_1", "chrome", "Google Chrome"),
            20: (901, "OpusApp", "WINWORD", "Microsoft Word"),
            30: (1, "Qt6QWindowIcon", "python", "Python"),  # Jarvis's chat window
            40: (902, "Progman", "explorer", "Windows Explorer"),  # the desktop
            50: (902, "Shell_TrayWnd", "explorer", "Windows Explorer"),  # the taskbar
        }
        self.z_order = [10, 20]
        self.foreground = 10
        self.minimised = set()

    def install(self, monkeypatch):
        monkeypatch.setattr(uia, "_foreground_hwnd", lambda: self.foreground)
        monkeypatch.setattr(uia, "_own_pids", lambda: {1})
        monkeypatch.setattr(uia, "_window_pid_of", lambda hwnd: self.windows[hwnd][0])
        monkeypatch.setattr(uia, "_window_class", lambda hwnd: self.windows[hwnd][1])
        monkeypatch.setattr(uia, "_z_order", lambda: list(self.z_order))
        monkeypatch.setattr(uia, "_window_minimised", lambda hwnd: hwnd in self.minimised)
        monkeypatch.setattr(uia, "_window_facts", lambda hwnd: {
            "process": self.windows[hwnd][2], "application": self.windows[hwnd][3],
            "monitor": r"\\.\DISPLAY1", "state": "normal"})
        return self


@pytest.fixture
def desk(monkeypatch):
    return FakeDesktop().install(monkeypatch)


@pytest.mark.unit
class TestForegroundTarget:
    def test_the_foreground_application_window(self, desk):
        target = uia.foreground_target()
        assert target == {"hwnd": 10, "process": "chrome", "application": "Google Chrome",
                          "monitor": r"\\.\DISPLAY1", "state": "normal"}

    def test_never_a_title(self, desk):
        assert "title" not in uia.foreground_target()

    def test_jarvis_in_front_means_the_window_behind_it(self, desk):
        desk.foreground, desk.z_order = 30, [30, 20, 10]
        assert uia.foreground_target()["hwnd"] == 20

    @pytest.mark.parametrize("shell", [40, 50])
    def test_the_desktop_or_taskbar_in_front_means_the_window_behind_it(self, desk, shell):
        desk.foreground, desk.z_order = shell, [shell, 30, 10]
        assert uia.foreground_target()["hwnd"] == 10

    def test_no_application_window_means_no_target(self, desk):
        desk.foreground, desk.z_order = 30, [30, 40, 50]
        assert uia.foreground_target() is None

    def test_a_minimised_window_behind_jarvis_is_not_what_the_user_is_looking_at(self, desk):
        desk.foreground, desk.z_order, desk.minimised = 30, [30, 10, 20], {10}
        assert uia.foreground_target()["hwnd"] == 20

    def test_no_foreground_at_all_falls_back_to_the_z_order(self, desk):
        desk.foreground = 0
        assert uia.foreground_target()["hwnd"] == 10

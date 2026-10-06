"""Windows helpers of the wake screen effect: never-activate windows and the full-screen check."""

import sys

import pytest

from desktop_app import win32_overlay

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows API")


class TestFailSafe:
    def test_other_platforms_report_no_fullscreen_app_and_change_nothing(self, monkeypatch):
        monkeypatch.setattr(win32_overlay.sys, "platform", "linux")
        assert win32_overlay.fullscreen_monitor_rect() is None
        assert win32_overlay.make_never_activate(1234) is False

    @windows_only
    def test_fullscreen_check_answers_with_a_monitor_rect_or_nothing(self):
        rect = win32_overlay.fullscreen_monitor_rect()
        assert rect is None or (len(rect) == 4 and rect[0] < rect[2] and rect[1] < rect[3])


@windows_only
class TestNeverActivate:
    @pytest.fixture
    def hwnd(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        handle = user32.CreateWindowExW(0, "STATIC", "jarvis-test", 0, 0, 0, 10, 10, None, None, None, None)
        assert handle
        yield handle
        user32.DestroyWindow(handle)

    def test_window_is_marked_never_activate(self, hwnd):
        import ctypes
        from ctypes import wintypes

        assert win32_overlay.make_never_activate(hwnd) is True
        user32 = ctypes.WinDLL("user32")
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        assert user32.GetWindowLongPtrW(hwnd, -20) & 0x08000000

    def test_marking_twice_is_harmless(self, hwnd):
        assert win32_overlay.make_never_activate(hwnd) is True
        assert win32_overlay.make_never_activate(hwnd) is True

    def test_an_invalid_window_is_reported_not_raised(self):
        assert win32_overlay.make_never_activate(0) is False

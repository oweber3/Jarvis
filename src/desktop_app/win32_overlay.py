"""
Windows only helpers for the wake screen effect (``wake_overlay``).

- ``fullscreen_monitor_rect``: the monitor a full-screen foreground app covers,
  so games and full-screen video are left alone.
- ``make_never_activate``: marks an overlay window so Windows never activates it.

Every call fails safe: on any other platform, or if a Windows API call fails,
it reports no full-screen app and leaves the window as Qt made it.
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple

from jarvis.debug import debug_log

# The desktop and taskbar can be the foreground window and cover a monitor.
_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
_MONITOR_DEFAULTTONULL = 0
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000


def make_never_activate(hwnd: int) -> bool:
    """Add ``WS_EX_NOACTIVATE`` to window ``hwnd``; returns whether the style is now set."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        # A private handle, so these prototypes never leak into other users of ``windll.user32``.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        style = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        if not style & _WS_EX_NOACTIVATE:
            user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE)
        return bool(user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE) & _WS_EX_NOACTIVATE)
    except Exception as exc:
        debug_log(f"could not mark overlay window as never-activate: {exc}", "desktop")
        return False


def fullscreen_monitor_rect() -> Optional[Tuple[int, int, int, int]]:
    """Native ``(left, top, right, bottom)`` of the monitor a full-screen foreground app covers, or ``None``.

    A maximised window is not full screen: it leaves the taskbar visible.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # A private handle, so these prototypes never leak into other users of ``windll.user32``.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if name.value in _SHELL_CLASSES or user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        monitor = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONULL)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        m = info.rcMonitor
        if rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom:
            return (m.left, m.top, m.right, m.bottom)
        return None
    except Exception as exc:
        debug_log(f"full-screen check failed: {exc}", "desktop")
        return None

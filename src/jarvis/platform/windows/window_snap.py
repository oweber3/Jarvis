"""The Windows output gesture snapping drives: the pointer as an aim, and grabbing, moving and snapping windows.

Used only by gesture snapping (``gestures/gestures.spec.md``), which relays the user's own hand movements;
nothing here is a tool action and nothing here clicks. Coordinates are physical pixels in the virtual desktop.
Moves are posted to windows without waiting (``SWP_ASYNCWINDOWPOS``), so a hung application never holds up
tracking; the final snap runs on a worker thread through the shared, verified ``place_window``.
"""
from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ...debug import debug_log

Rectangle = Tuple[int, int, int, int]

_GA_ROOT = 2
_HWND_TOP = 0
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x4, 0x10, 0x4000
# The desktop and the taskbars: grabbing one would move the shell itself.
_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
# Where a restored window's top edge sits above the pointer when a maximised window is grabbed.
_TITLE_GRIP_PX = 16


def grabbable_class(name: str) -> bool:
    """Whether a top-level window of this class may be grabbed (never the desktop or a taskbar)."""
    return name not in _SHELL_CLASSES


def choose_zone(x: int, y: int, zones: Sequence[Rectangle]) -> Optional[Rectangle]:
    """The zone containing the point, else the nearest one (the point is in the spacing between zones)."""
    best: Optional[Tuple[float, Rectangle]] = None
    for zone in zones:
        left, top, right, bottom = zone
        dx = max(left - x, 0, x - (right - 1))
        dy = max(top - y, 0, y - (bottom - 1))
        gap = dx * dx + dy * dy
        if gap == 0:
            return tuple(zone)
        if best is None or gap < best[0]:
            best = (gap, tuple(zone))
    return best[1] if best else None


@dataclass
class Grabbed:
    """A grabbed window: its handle, its outer frame now (``rect``) and how to put it back."""

    hwnd: int
    rect: Rectangle
    original: Rectangle
    was_maximised: bool


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _user32():
    from ctypes import wintypes
    dll = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "WindowFromPoint": ([_Point], wintypes.HWND),
        "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.UINT], wintypes.BOOL),
        "GetCursorPos": ([ctypes.POINTER(_Point)], wintypes.BOOL),
        "SetCursorPos": ([ctypes.c_int, ctypes.c_int], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


class WindowsSnapOutput:
    """The real desktop: monitors, the pointer and top-level windows."""

    def desktop(self) -> List[Rectangle]:
        from .displays import list_monitors
        return [monitor.bounds for monitor in list_monitors()]

    def position(self) -> Tuple[int, int]:
        from .displays import per_monitor_dpi
        point = _Point()
        with per_monitor_dpi():
            if not _user32().GetCursorPos(ctypes.byref(point)):
                raise OSError("The pointer position could not be read.")
        return int(point.x), int(point.y)

    def move_to(self, x: int, y: int) -> None:
        from .displays import per_monitor_dpi
        with per_monitor_dpi():
            if not _user32().SetCursorPos(int(x), int(y)):
                raise OSError("Windows did not move the pointer.")

    def grab(self, x: int, y: int) -> Optional[Grabbed]:
        """The top-level window under the point, brought to the front without the keyboard focus. A maximised
        window is restored first and placed so the point keeps its share of the window's width."""
        from . import windows_mgmt
        from .displays import per_monitor_dpi
        user = _user32()
        with per_monitor_dpi():
            child = user.WindowFromPoint(_Point(int(x), int(y)))
            hwnd = user.GetAncestor(child, _GA_ROOT) if child else None
            if not hwnd:
                return None
            name = ctypes.create_unicode_buffer(64)
            user.GetClassNameW(hwnd, name, len(name))
            if not grabbable_class(name.value):
                return None
            hwnd = int(hwnd)
            original = windows_mgmt._outer_rect(hwnd)
            was_maximised = windows_mgmt._window_state(hwnd) == "maximised"
            rect = original
            if was_maximised:
                share = (x - original[0]) / max(original[2] - original[0], 1)
                windows_mgmt._show_window(hwnd, "restore")
                left, top, right, bottom = windows_mgmt._outer_rect(hwnd)
                width, height = right - left, bottom - top
                new_left, new_top = int(x - share * width), int(y - _TITLE_GRIP_PX)
                windows_mgmt._move_outer(hwnd, (new_left, new_top, new_left + width, new_top + height))
                rect = (new_left, new_top, new_left + width, new_top + height)
            user.SetWindowPos(hwnd, _HWND_TOP, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        return Grabbed(hwnd, rect, original, was_maximised)

    def move_window(self, window: Grabbed, left: int, top: int) -> None:
        from .displays import per_monitor_dpi
        flags = _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS
        with per_monitor_dpi():
            if not _user32().SetWindowPos(window.hwnd, None, int(left), int(top), 0, 0, flags):
                raise OSError("Windows did not move the window.")
        width, height = window.rect[2] - window.rect[0], window.rect[3] - window.rect[1]
        window.rect = (int(left), int(top), int(left) + width, int(top) + height)

    def drop(self, window: Grabbed, x: int, y: int) -> None:
        """Snap the window into the FancyZones zone at the point, on a worker thread."""
        threading.Thread(target=_snap, args=(window.hwnd, int(x), int(y)), name="gesture-snap", daemon=True).start()

    def put_back(self, window: Grabbed) -> None:
        from . import windows_mgmt
        from .displays import per_monitor_dpi
        with per_monitor_dpi():
            windows_mgmt._move_outer(window.hwnd, window.original)
            if window.was_maximised:
                windows_mgmt._show_window(window.hwnd, "maximise")


def _snap(hwnd: int, x: int, y: int) -> None:
    from . import displays, windows_mgmt
    try:
        monitors = displays.list_monitors()
        monitor = next((m for m in monitors if m.bounds[0] <= x < m.bounds[2] and m.bounds[1] <= y < m.bounds[3]),
                       None)
        zones = displays.fancyzone_sets(monitors).get(monitor.device) if monitor else None
        zone = choose_zone(x, y, zones.rectangles) if zones else None
        if zone is None:
            debug_log("gesture snapping: no zone at the drop, window left where it is", "gestures")
            return
        windows_mgmt.place_window(hwnd, monitor, zone)
        debug_log("gesture snapping: window snapped into a zone", "gestures")
    except Exception as exc:  # noqa: BLE001 - a failed snap leaves the window where it was dropped
        debug_log(f"gesture snapping: snap failed ({type(exc).__name__})", "gestures")
        try:
            print("🖐️ Gesture snapping could not snap the window into the zone", flush=True)
        except Exception:  # noqa: BLE001
            pass

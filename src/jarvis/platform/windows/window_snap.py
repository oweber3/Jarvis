"""The Windows output gesture snapping drives: the pointer as an aim, and grabbing, moving and snapping windows.

Used only by gesture snapping (``gestures/gestures.spec.md``), which relays the user's own hand movements;
nothing here is a tool action and nothing here clicks or sends keys (it scrolls the wheel only). Coordinates are physical pixels in the virtual desktop.
Grabbing and moving post to windows without waiting (``ShowWindowAsync``, ``SWP_ASYNCWINDOWPOS``), so a hung
application never holds up tracking; the final snap and putting a window back run on worker threads, the snap
through the shared, verified ``place_window``.
"""
from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from ...debug import debug_log

Rectangle = Tuple[int, int, int, int]

_GA_ROOT = 2
_HWND_TOP = 0
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x4, 0x10, 0x4000
# The desktop and the taskbars: grabbing one would move the shell itself.
_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
# Where a restored window's top edge sits above the pointer when a maximised window is grabbed.
_TITLE_GRIP_PX = 16
_SW_RESTORE = 9
_INPUT_MOUSE, _MOUSEEVENTF_WHEEL = 0, 0x0800


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


def restored_rect(x: int, y: int, maximised_outer: Rectangle, normal_width: int, normal_height: int) -> Rectangle:
    """Where a maximised window grabbed at the point goes once restored to its normal size: the point keeps its
    share of the window's width and sits ``_TITLE_GRIP_PX`` below the top edge."""
    share = (x - maximised_outer[0]) / max(maximised_outer[2] - maximised_outer[0], 1)
    left, top = int(round(x - share * normal_width)), int(y - _TITLE_GRIP_PX)
    return left, top, left + int(normal_width), top + int(normal_height)


@dataclass
class Grabbed:
    """A grabbed window: its handle, its outer frame now (``rect``, which moves are offset from) and its
    ``WINDOWPLACEMENT`` when grabbed (``placement``), which putting it back restores."""

    hwnd: int
    rect: Rectangle
    placement: Any = None


class _InFlight:
    """The windows a worker is still snapping or putting back; they cannot be grabbed until it finishes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hwnds: set = set()

    def add(self, hwnd: int) -> None:
        with self._lock:
            self._hwnds.add(hwnd)

    def discard(self, hwnd: int) -> None:
        with self._lock:
            self._hwnds.discard(hwnd)

    def __contains__(self, hwnd: object) -> bool:
        with self._lock:
            return hwnd in self._hwnds


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long),
                ("bottom", ctypes.c_long)]


class _WindowPlacement(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                ("ptMinPosition", _Point), ("ptMaxPosition", _Point), ("rcNormalPosition", _Rect)]


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
        "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
        "GetWindowPlacement": ([wintypes.HWND, ctypes.POINTER(_WindowPlacement)], wintypes.BOOL),
        "SetWindowPlacement": ([wintypes.HWND, ctypes.POINTER(_WindowPlacement)], wintypes.BOOL),
        "ShowWindowAsync": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


def wheel_input(delta: int):
    """One vertical mouse-wheel ``INPUT`` at the pointer (positive scrolls up, 120 is one notch), no buttons."""
    from .input_control import _Input, _InputUnion, _MouseInput
    wheel = _MouseInput(0, 0, int(delta) & 0xFFFFFFFF, _MOUSEEVENTF_WHEEL, 0, 0)
    return _Input(_INPUT_MOUSE, _InputUnion(mi=wheel))


def _send_wheel(delta: int) -> None:
    from .input_control import _Input
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int]
    user.SendInput.restype = ctypes.c_uint
    item = wheel_input(delta)
    if user.SendInput(1, ctypes.byref(item), ctypes.sizeof(item)) != 1:
        raise OSError("Windows blocked the scroll (a higher-privilege window may be under the pointer).")


class WindowsSnapOutput:
    """The real desktop: monitors, the pointer and top-level windows."""

    def __init__(self, wheel_sender: Optional[Callable[[int], None]] = None, *, use_fancyzones: bool = True,
                 languages: Sequence[str] = ("en",)) -> None:
        self._wheel_sender = wheel_sender or _send_wheel
        self._use_fancyzones = bool(use_fancyzones)
        self._languages = tuple(languages)
        self._in_flight = _InFlight()

    def scroll(self, delta: int) -> None:
        """One vertical wheel event at the pointer, so it reaches the window under it; zero does nothing."""
        if int(delta):
            self._wheel_sender(int(delta))

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
        """The top-level window under the point, brought to the front without the keyboard focus, or None. A
        maximised window is restored and placed so the point keeps its share of the width. Nothing here waits for
        the application, and a window a worker is still snapping or putting back is not grabbed."""
        from .displays import per_monitor_dpi
        with per_monitor_dpi():
            hwnd = _window_at(int(x), int(y))
            if hwnd is None:
                return None
            if hwnd in self._in_flight:
                debug_log("gesture snapping: window still settling, not grabbed", "gestures")
                return None
            return _take(hwnd, int(x), int(y))

    def move_window(self, window: Grabbed, left: int, top: int) -> None:
        from .displays import per_monitor_dpi
        flags = _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS
        with per_monitor_dpi():
            if not _user32().SetWindowPos(window.hwnd, None, int(left), int(top), 0, 0, flags):
                raise OSError("Windows did not move the window.")
        width, height = window.rect[2] - window.rect[0], window.rect[3] - window.rect[1]
        window.rect = (int(left), int(top), int(left) + width, int(top) + height)

    def drop(self, window: Grabbed, x: int, y: int) -> None:
        """Snap the window into the FancyZones zone at the point (when zones are on), on a worker thread."""
        hwnd, px, py = window.hwnd, int(x), int(y)
        use_fancyzones, languages = self._use_fancyzones, self._languages
        self._on_worker(hwnd, lambda: _snap(hwnd, px, py, use_fancyzones=use_fancyzones, languages=languages),
                        "gesture-snap")

    def put_back(self, window: Grabbed) -> None:
        """Restore the window's placement from when it was grabbed, on a worker thread."""
        hwnd, placement = window.hwnd, window.placement
        self._on_worker(hwnd, lambda: _restore_placement(hwnd, placement), "gesture-put-back")

    def _on_worker(self, hwnd: int, work: Callable[[], None], name: str) -> None:
        """Run ``work`` on a daemon thread with the window marked in flight until it finishes, however it ends."""
        self._in_flight.add(hwnd)

        def run() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - the window stays where it is
                debug_log(f"gesture snapping: {name} failed ({type(exc).__name__})", "gestures")
                try:
                    print("🖐️ Gesture snapping could not finish moving the window", flush=True)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._in_flight.discard(hwnd)

        try:
            threading.Thread(target=run, name=name, daemon=True).start()
        except Exception:
            self._in_flight.discard(hwnd)
            raise


def _window_at(x: int, y: int) -> Optional[int]:
    """The grabbable top-level window under the point (call inside ``per_monitor_dpi``), or None."""
    user = _user32()
    child = user.WindowFromPoint(_Point(x, y))
    hwnd = user.GetAncestor(child, _GA_ROOT) if child else None
    if not hwnd:
        return None
    name = ctypes.create_unicode_buffer(64)
    user.GetClassNameW(hwnd, name, len(name))
    return int(hwnd) if grabbable_class(name.value) else None


def _take(hwnd: int, x: int, y: int) -> Grabbed:
    """Record the window's placement, restore it under the point if maximised and raise it, all posted without
    waiting for the application (call inside ``per_monitor_dpi``)."""
    from . import windows_mgmt
    user = _user32()
    placement = _WindowPlacement()
    placement.length = ctypes.sizeof(_WindowPlacement)
    if not user.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        raise OSError("The window's placement could not be read.")
    rect = windows_mgmt._outer_rect(hwnd)
    if user.IsZoomed(hwnd):
        normal = placement.rcNormalPosition
        rect = restored_rect(x, y, rect, normal.right - normal.left, normal.bottom - normal.top)
        user.ShowWindowAsync(hwnd, _SW_RESTORE)
        left, top, right, bottom = rect
        user.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                          _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS)
    user.SetWindowPos(hwnd, _HWND_TOP, 0, 0, 0, 0,
                      _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS)
    return Grabbed(hwnd, rect, placement)


def _restore_placement(hwnd: int, placement: Any) -> None:
    """Put the window back as ``GetWindowPlacement`` found it: maximised windows are maximised again with their
    restored size intact, others return to their frame."""
    from .displays import per_monitor_dpi
    if placement is None:
        return
    with per_monitor_dpi():
        if not _user32().SetWindowPlacement(hwnd, ctypes.byref(placement)):
            raise OSError("Windows did not put the window back.")
    debug_log("gesture snapping: window put back", "gestures")


def _snap(hwnd: int, x: int, y: int, *, use_fancyzones: bool, languages: Sequence[str]) -> None:
    from . import displays, windows_mgmt
    if not use_fancyzones:
        debug_log("gesture snapping: FancyZones off, window left where it was dropped", "gestures")
        return
    try:
        monitors = displays.list_monitors()
        monitor = next((m for m in monitors if m.bounds[0] <= x < m.bounds[2] and m.bounds[1] <= y < m.bounds[3]),
                       None)
        zones = displays.fancyzone_sets(monitors, tuple(languages)).get(monitor.device) if monitor else None
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

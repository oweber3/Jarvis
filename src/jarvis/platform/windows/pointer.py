"""The pointer output the air mouse drives: monitor rectangles, cursor position and the primary button.

Used only by the air mouse (``gestures/gestures.spec.md``), which relays the user's own hand movements;
nothing here is a tool action. Coordinates are physical pixels in the virtual desktop. The calls return at
once and run on the calling thread, so a press is never separated from its release by an abandoned worker.
"""
from __future__ import annotations

import ctypes
from typing import Callable, List, Optional, Tuple

from .input_control import _Input, _InputUnion, _MouseInput

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
_INPUT_MOUSE = 0
_SM_SWAPBUTTON = 23


def input_record_size() -> int:
    """The size of one ``INPUT`` record as ``SendInput`` is told it."""
    return ctypes.sizeof(_Input)


def _send_mouse(flags: int) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int]
    user.SendInput.restype = ctypes.c_uint
    item = _Input(_INPUT_MOUSE, _InputUnion(mi=_MouseInput(0, 0, 0, flags, 0, 0)))
    if user.SendInput(1, ctypes.byref(item), ctypes.sizeof(item)) != 1:
        raise OSError("Windows blocked the mouse button (a higher-privilege window may be under the pointer).")


def _buttons_swapped() -> bool:
    try:
        return bool(ctypes.windll.user32.GetSystemMetrics(_SM_SWAPBUTTON))
    except (AttributeError, OSError):
        return False


class _CursorPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WindowsPointer:
    """The real pointer. ``sender`` (takes ``MOUSEEVENTF`` flags) and ``swapped`` replace Windows in tests."""

    def __init__(self, sender: Optional[Callable[[int], None]] = None, swapped: Optional[bool] = None):
        self._send = sender or _send_mouse
        swapped = _buttons_swapped() if swapped is None else swapped
        # With swapped buttons the physical right button is the primary one.
        self._down, self._up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if swapped
                                else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))

    def desktop(self) -> List[Tuple[int, int, int, int]]:
        from .displays import list_monitors
        return [monitor.bounds for monitor in list_monitors()]

    def position(self) -> Tuple[int, int]:
        from .displays import per_monitor_dpi
        point = _CursorPoint()
        with per_monitor_dpi():
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                raise OSError("The pointer position could not be read.")
        return int(point.x), int(point.y)

    def move_to(self, x: int, y: int) -> None:
        from .displays import per_monitor_dpi
        with per_monitor_dpi():
            if not ctypes.windll.user32.SetCursorPos(int(x), int(y)):
                raise OSError("Windows did not move the pointer.")

    def press(self) -> None:
        self._send(self._down)

    def release(self) -> None:
        self._send(self._up)

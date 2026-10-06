"""Foreground window events, the foreground window's title and process, and idle time.

OS functions only: they know nothing of the activity store or the reply engine. A
``ForegroundWatcher`` runs one dedicated thread with its own message loop, installs
``SetWinEventHook`` hooks for foreground changes and window title changes, and calls back with a
paced event kind. Nothing here sends input, changes focus or touches a window. See
``memory/activity_log.spec.md``.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, NamedTuple, Optional

from ...debug import debug_log

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_NAMECHANGE = 0x800C
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
_MIN_WAIT_SEC = 0.05
_STOP_JOIN_SEC = 3.0

@lru_cache(maxsize=1)
def _winevent_proc():
    """The WINEVENTPROC callback type. Built on first use: ``WINFUNCTYPE`` exists only on Windows."""
    return ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                              wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


class Foreground(NamedTuple):
    """The foreground window: executable stem, application display name and raw window title."""
    process: str
    app: str
    title: str


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _user32():
    dll = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "SetWinEventHook": ([wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, _winevent_proc(), wintypes.DWORD,
                             wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "UnhookWinEvent": ([wintypes.HANDLE], wintypes.BOOL),
        "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                          wintypes.UINT], wintypes.BOOL),
        "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
        "MsgWaitForMultipleObjects": ([wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                       wintypes.DWORD], wintypes.DWORD),
        "PostThreadMessageW": ([wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "GetLastInputInfo": ([ctypes.POINTER(_LASTINPUTINFO)], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


def _kernel32():
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GetTickCount.argtypes, dll.GetTickCount.restype = [], wintypes.DWORD
    dll.GetCurrentThreadId.argtypes, dll.GetCurrentThreadId.restype = [], wintypes.DWORD
    return dll


# ── Readers ────────────────────────────────────────────────────────────────

def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input in this session."""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32().GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_kernel32().GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


_app_names: Dict[str, str] = {}


def _file_description(path: str) -> str:
    """The executable's own product description (for example "Google Chrome"), or empty."""
    try:
        import win32api
        for language, codepage in win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation"):
            description = win32api.GetFileVersionInfo(
                path, f"\\StringFileInfo\\{language:04x}{codepage:04x}\\FileDescription")
            if description and description.strip():
                return description.strip()
    except Exception:
        pass
    return ""


def app_name(exe: str, stem: str) -> str:
    """The application's product name from its executable (for example "Google Chrome"), else ``stem``."""
    if not exe:
        return stem
    if exe not in _app_names:
        _app_names[exe] = _file_description(exe) or stem
    return _app_names[exe]


def foreground_snapshot(hwnd: Optional[int] = None) -> Optional[Foreground]:
    """The title, process and application name of ``hwnd`` (default: the foreground window)."""
    import psutil
    user = _user32()
    handle = user.GetForegroundWindow() if hwnd is None else hwnd
    if not handle:
        return None
    length = user.GetWindowTextLengthW(handle)
    title = ""
    if length:
        buffer = ctypes.create_unicode_buffer(length + 1)
        user.GetWindowTextW(handle, buffer, len(buffer))
        title = buffer.value
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if not pid.value:
        return None
    try:
        process = psutil.Process(pid.value)
        stem = Path(process.name()).stem
        try:
            exe = process.exe()
        except (psutil.Error, OSError):
            exe = ""
    except (psutil.Error, OSError):
        return None
    return Foreground(stem, app_name(exe, stem), title)


# ── Event pacing ───────────────────────────────────────────────────────────

class EventGate:
    """Decides when the watcher thread reports an event.

    A foreground change is reported at once. Title changes are rate limited: any number inside the
    window become one report when it ends. Silence produces a periodic tick, which carries idle
    detection and catches anything an event missed. Pure logic; the caller owns the clock.
    """

    def __init__(self, tick_sec: float, title_throttle_sec: float) -> None:
        self._tick = float(tick_sec)
        self._throttle = float(title_throttle_sec)
        self._last: Optional[float] = None
        self._foreground = False
        self._title = False

    def note(self, kind: str) -> None:
        if kind == "foreground":
            self._foreground = True
        elif kind == "title":
            self._title = True

    def due(self, now: float) -> Optional[str]:
        if self._last is None:
            kind = "tick"
        elif self._foreground:
            kind = "foreground"
        elif self._title and now - self._last >= self._throttle:
            kind = "title"
        elif now - self._last >= self._tick:
            kind = "tick"
        else:
            return None
        self._last = now
        self._foreground = self._title = False
        return kind

    def wait_sec(self, now: float) -> float:
        if self._last is None or self._foreground:
            return _MIN_WAIT_SEC
        remaining = self._tick - (now - self._last)
        if self._title:
            remaining = min(remaining, self._throttle - (now - self._last))
        return max(_MIN_WAIT_SEC, remaining)


# ── Watcher ────────────────────────────────────────────────────────────────

def _foreground_hwnd() -> int:
    return int(_user32().GetForegroundWindow() or 0)


class ForegroundWatcher:
    """One thread, one message loop, two WinEvent hooks. ``on_event(kind)`` runs on that thread.

    ``kind`` is ``foreground``, ``title`` (the foreground window's title changed) or ``tick``.
    ``foreground_provider`` says which window counts as the foreground one for title events; tests
    give it a window that is never really activated.
    """

    def __init__(self, on_event: Callable[[str], None], *, foreground_provider: Callable[[], int] = _foreground_hwnd,
                 tick_sec: float = 5.0, title_throttle_sec: float = 2.0) -> None:
        self._on_event = on_event
        self._foreground = foreground_provider
        self._gate = EventGate(tick_sec, title_throttle_sec)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self.hooks_installed = False

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="activity-watcher", daemon=True)
        self._thread.start()
        ready.wait(_STOP_JOIN_SEC)
        return self._thread.is_alive()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        if self._thread_id:
            _user32().PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(_STOP_JOIN_SEC)

    # The WinEvent callback runs inside this thread's own message dispatch, so it shares the gate
    # with the loop without locking.
    def _on_win_event(self, _hook, event, hwnd, id_object, _id_child, _thread, _time) -> None:
        try:
            if event == EVENT_SYSTEM_FOREGROUND:
                self._gate.note("foreground")
            elif event == EVENT_OBJECT_NAMECHANGE and id_object == OBJID_WINDOW and hwnd \
                    and int(hwnd) == self._foreground():
                self._gate.note("title")
        except Exception as exc:
            debug_log(f"activity hook callback failed: {type(exc).__name__}", "activity")

    def _run(self, ready: threading.Event) -> None:
        user = _user32()
        message = wintypes.MSG()
        user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # creates this thread's message queue
        self._thread_id = int(_kernel32().GetCurrentThreadId())
        callback = _winevent_proc()(self._on_win_event)  # must outlive the hooks
        hooks = []
        for event in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_NAMECHANGE):
            handle = user.SetWinEventHook(event, event, None, callback, 0, 0, WINEVENT_OUTOFCONTEXT)
            if handle:
                hooks.append(handle)
        self.hooks_installed = len(hooks) == 2
        if not self.hooks_installed:
            debug_log(f"activity hooks installed: {len(hooks)} of 2; falling back to periodic checks", "activity")
        ready.set()
        try:
            while not self._stop.is_set():
                user.MsgWaitForMultipleObjects(0, None, False,
                                               int(self._gate.wait_sec(time.monotonic()) * 1000), QS_ALLINPUT)
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    if message.message == WM_QUIT:
                        self._stop.set()
                        break
                    user.TranslateMessage(ctypes.byref(message))
                    user.DispatchMessageW(ctypes.byref(message))
                kind = None if self._stop.is_set() else self._gate.due(time.monotonic())
                if kind:
                    try:
                        self._on_event(kind)
                    except Exception as exc:
                        debug_log(f"activity event handler failed: {type(exc).__name__}", "activity")
        finally:
            for handle in hooks:
                user.UnhookWinEvent(handle)
            self._thread_id = 0

"""A simulated Windows desktop for follow-up evals and integration tests.

Only the OS layer is replaced: the application catalogue, ``os.startfile``, browser launches,
window enumeration, the window the user is looking at (``foreground``, which a launch or focus
moves), show state, focus, close, placement and display enumeration. The real ``appControl`` and
``windowControl`` tools, the fast-command path, central safety and tool validation all run
unchanged, so an eval observes what a real request would do to the desktop without touching it.

Displays: ``\\\\.\\DISPLAY1`` (primary, alias ``main``) and ``\\\\.\\DISPLAY2`` (aliases ``2``,
``second``, ``side``) with ``left`` and ``right`` zones on the second display.
"""
from __future__ import annotations

import itertools
import os
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Dict, List, Optional
from unittest.mock import patch

PRIMARY = r"\\.\DISPLAY1"
SECOND = r"\\.\DISPLAY2"

MONITOR_ALIASES = {"main": PRIMARY, "2": SECOND, "second": SECOND, "side": SECOND}
WINDOW_ZONES = {SECOND: {"left": [0, 0, 0.5, 1], "right": [0.5, 0, 0.5, 1]}}

# (catalogue name, launch target, executable): executable stems become window process names.
CATALOGUE = (
    ("Word", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Word.lnk",
     r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
    ("Notepad", r"C:\Windows\System32\notepad.exe", r"C:\Windows\System32\notepad.exe"),
    ("Apple Music", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Apple Music.lnk",
     r"C:\Program Files\WindowsApps\AppleInc.AppleMusicWin\AppleMusic.exe"),
    ("Google Chrome", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Google Chrome.lnk",
     r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
)
_TITLES = {"WINWORD": "Document1 - Word", "notepad": "Untitled - Notepad", "AppleMusic": "Apple Music",
           "chrome": "New Tab - Google Chrome", "explorer": "Downloads - File Explorer",
           "Code": "jarvis - Visual Studio Code"}
# Product names the foreground read reports for each executable.
_APPLICATIONS = {"WINWORD": "Microsoft Word", "notepad": "Notepad", "AppleMusic": "Apple Music",
                 "chrome": "Google Chrome", "explorer": "Windows Explorer", "Code": "Visual Studio Code"}
# A real desktop is never empty: with a single window, "it" could be guessed from a listing alone.
BACKGROUND = ("explorer", "chrome", "Code")


@dataclass
class SimWindow:
    hwnd: int
    title: str
    process: str
    pid: int
    monitor: str = PRIMARY
    state: str = "normal"  # normal | maximised | minimised
    zone_rect: Optional[tuple] = None
    open: bool = True


class SimulatedDesktop:
    """Context manager that patches the OS layer. ``windows`` is the live desktop state."""

    def __init__(self, cfg=None, background=BACKGROUND):
        self.cfg = cfg
        self.windows: List[SimWindow] = []
        self.launches: List[str] = []
        self.browser_launches: List[List[str]] = []
        self.foreground: Optional[int] = None
        # When set, placing a window fails with this message (the application refuses to move).
        self.fail_placement: Optional[str] = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._stack: Optional[ExitStack] = None
        for process in background:
            self.spawn(process)

    # -- configuration -----------------------------------------------------------------

    @staticmethod
    def overrides() -> Dict[str, object]:
        """Settings for the simulated displays, for ``dataclasses.replace`` on frozen settings."""
        return {"windows_tools_enabled": True, "windows_app_aliases": {},
                "windows_monitor_aliases": dict(MONITOR_ALIASES),
                "windows_window_zones": {device: dict(zones) for device, zones in WINDOW_ZONES.items()}}

    @classmethod
    def configure(cls, cfg) -> None:
        """Give a mutable test ``cfg`` the simulated displays' aliases and zones."""
        for key, value in cls.overrides().items():
            setattr(cfg, key, value)

    # -- state helpers -----------------------------------------------------------------

    def open_windows(self, process: Optional[str] = None) -> List[SimWindow]:
        with self._lock:
            return [w for w in self.windows if w.open and
                    (process is None or w.process.casefold() == process.casefold())]

    def only(self, process: str) -> SimWindow:
        found = self.open_windows(process)
        assert len(found) == 1, f"expected one open {process} window, found {len(found)}"
        return found[0]

    def spawn(self, process: str, monitor: str = PRIMARY) -> SimWindow:
        """Add a window that already exists before the scenario starts."""
        n = next(self._ids)
        window = SimWindow(hwnd=0x20000 + n * 0x10A, title=_TITLES.get(process, process), process=process,
                           pid=4000 + n, monitor=monitor)
        with self._lock:
            self.windows.append(window)
        return window

    def _by_hwnd(self, hwnd: int) -> SimWindow:
        for window in self.windows:
            if window.hwnd == int(hwnd) and window.open:
                return window
        raise OSError("The selected window is no longer open.")

    # -- OS fakes ----------------------------------------------------------------------

    def _startfile(self, target, *args, **kwargs):
        from jarvis.platform.windows.apps import Application
        from pathlib import PureWindowsPath

        app = next((Application(*entry) for entry in CATALOGUE if entry[1] == target), None)
        if app is None:
            raise OSError(f"Simulated desktop cannot launch {target}")
        self.launches.append(app.name)
        window = self.spawn(PureWindowsPath(app.executable).stem)
        if window is not None:  # a test may hold the window back, as a slow application does
            self.foreground = window.hwnd

    def _spawn_browser(self, command):
        """A browser launched with ``--new-window``: a new chrome window on the primary display."""
        self.browser_launches.append(list(command))
        window = self.spawn("chrome")
        if window is not None:
            self.foreground = window.hwnd

    @staticmethod
    def _find_browser(name=None):
        from jarvis.platform.windows.workspaces import Browser
        return Browser("chrome", "chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe")

    def workspace_definitions(self, directory) -> Dict[str, dict]:
        """A synthetic ``windows_workspaces`` setting: two placeholder documents and a site on the
        second display's left and right zones. Nothing here is a real path or URL."""
        from pathlib import Path
        documents = []
        for name in ("first.pdf", "second.pdf"):
            path = Path(directory) / name
            path.write_bytes(b"%PDF-1.4")
            documents.append(str(path))
        return {"design": {"aliases": ["design project"], "items": [
            {"kind": "browser_window", "label": "textbooks", "urls": documents, "monitor": "second", "zone": "left"},
            {"kind": "browser_window", "label": "chat", "urls": ["https://chat.example.test"],
             "monitor": "second", "zone": "right"}]}}

    def _list_windows(self):
        from jarvis.platform.windows.windows_mgmt import Window
        return [Window(w.hwnd, w.title, w.process, w.pid) for w in self.open_windows()]

    def _show_window(self, hwnd, action):
        window = self._by_hwnd(hwnd)
        window.state = {"minimise": "minimised", "maximise": "maximised", "restore": "normal"}[action]
        if action != "minimise":
            window.zone_rect = None if action == "maximise" else window.zone_rect

    def _focus_window(self, hwnd):
        window = self._by_hwnd(hwnd)
        if window.state == "minimised":
            window.state = "normal"
        self.foreground = window.hwnd
        return True

    def _close_window(self, hwnd):
        self._by_hwnd(hwnd).open = False

    def _foreground_target(self):
        """What ``ui_automation.foreground_target`` reports: never a title."""
        window = next((w for w in self.open_windows() if w.hwnd == self.foreground), None)
        if window is None:
            return None
        return {"hwnd": window.hwnd, "process": window.process,
                "application": _APPLICATIONS.get(window.process, window.process),
                "monitor": window.monitor, "state": window.state}

    def _place_window(self, hwnd, monitor, rectangle=None, state="restore", *, deadline=None):
        window = self._by_hwnd(hwnd)
        if self.fail_placement:
            raise OSError(self.fail_placement)
        window.monitor = monitor.device
        window.state = "maximised" if state == "maximise" else "normal"
        window.zone_rect = tuple(rectangle) if rectangle else None
        landed = list(rectangle) if rectangle else list(monitor.work_area)
        return {"hwnd": window.hwnd, "monitor": monitor.device, "rectangle": landed, "state": state}

    def _list_monitors(self):
        from jarvis.platform.windows.displays import Monitor
        return [Monitor(PRIMARY, (0, 0, 2560, 1440), (0, 0, 2560, 1392), True),
                Monitor(SECOND, (2560, 0, 4480, 1080), (2560, 0, 4480, 1040), False)]

    def _monitor_for_window(self, hwnd):
        return self._by_hwnd(hwnd).monitor

    # -- context manager ---------------------------------------------------------------

    def __enter__(self) -> "SimulatedDesktop":
        from jarvis.platform.windows import apps, displays, ui_automation, windows_mgmt, workspaces
        from jarvis.platform.windows.apps import Application

        catalogue = [Application(*entry) for entry in CATALOGUE]
        index = type("SimIndex", (), {"applications": lambda _self: list(catalogue),
                                      "snapshot": lambda _self: tuple(catalogue),
                                      "start": lambda _self: None})()
        if self.cfg is not None:
            self.configure(self.cfg)
        stack = ExitStack()
        stack.enter_context(patch.object(apps, "APP_INDEX", index))
        stack.enter_context(patch.object(os, "startfile", self._startfile, create=True))
        stack.enter_context(patch.object(apps, "_STABLE_SEC", 0.05))
        stack.enter_context(patch.object(apps, "_POLL_SEC", 0.02))
        stack.enter_context(patch.object(workspaces, "_STABLE_SEC", 0.05))
        stack.enter_context(patch.object(workspaces, "_POLL_SEC", 0.02))
        stack.enter_context(patch.object(workspaces, "_spawn", self._spawn_browser))
        stack.enter_context(patch.object(workspaces, "find_browser", self._find_browser))
        stack.enter_context(patch.object(windows_mgmt, "list_windows", self._list_windows))
        stack.enter_context(patch.object(windows_mgmt, "_show_window", self._show_window))
        stack.enter_context(patch.object(windows_mgmt, "_focus_window", self._focus_window))
        stack.enter_context(patch.object(windows_mgmt, "_close_window", self._close_window))
        stack.enter_context(patch.object(ui_automation, "foreground_target", self._foreground_target,
                                             create=True))  # absent from builds without it
        stack.enter_context(patch.object(windows_mgmt, "place_window", self._place_window))
        stack.enter_context(patch.object(displays, "list_monitors", self._list_monitors))
        stack.enter_context(patch.object(displays, "monitor_device_for_window", self._monitor_for_window))
        self._stack = stack
        return self

    def __exit__(self, *exc) -> bool:
        if self._stack is not None:
            self._stack.close()
        return False

    def snapshot(self) -> List[Dict[str, object]]:
        """Open windows as plain data, for reports."""
        return [{"hwnd": w.hwnd, "process": w.process, "monitor": w.monitor, "state": w.state,
                 "zone_rect": list(w.zone_rect) if w.zone_rect else None} for w in self.open_windows()]

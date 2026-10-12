"""The air mouse as a mode that can be turned on and off: one hand tracker, one ``AirMouse`` and a watchdog.

The camera runs only while the mode is on. ``stop`` always lets go of the button before it releases the
camera, and runs at process exit too. See ``gestures.spec.md``, Air mouse.
"""
from __future__ import annotations

import atexit
import threading
from typing import Any, Callable, Optional

from ..debug import debug_log
from .air_mouse import WATCHDOG_SEC, AirMouse, AirMouseSettings
from .hand_tracking import HandTracker

GESTURE_GUIDE = ("Point with your index finger to move the pointer, pinch your thumb and index finger to click, "
                 "hold the pinch to drag, and open your hand to pause.")


class AirMouseUnavailable(Exception):
    """The air mouse could not start; the message says why in plain words."""


class AirMouseService:
    """Starts and stops the air mouse. ``output_factory`` makes the pointer output; ``tracker_factory`` takes
    the camera index and makes the hand tracker."""

    def __init__(self, cfg: Any, output_factory: Callable[[], Any],
                 tracker_factory: Callable[[int], Any] = HandTracker):
        self._cfg = cfg
        self._output_factory = output_factory
        self._tracker_factory = tracker_factory
        self._lock = threading.Lock()
        self._tracker: Any = None
        self._mouse: Optional[AirMouse] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._stop_watchdog: Optional[threading.Event] = None
        self._watchdog: Optional[threading.Thread] = None
        self._exit_hook = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._mouse is not None

    @property
    def state(self) -> str:
        """The air mouse's state for display, or ``off``."""
        with self._lock:
            mouse = self._mouse
        return mouse.state if mouse is not None else "off"

    def start(self) -> str:
        """``"started"`` or ``"already"``; raises ``AirMouseUnavailable`` with nothing left running."""
        with self._lock:
            if self._mouse is not None:
                return "already"
            try:
                mouse = AirMouse(self._output_factory(), AirMouseSettings.from_config(self._cfg))
            except Exception as exc:  # noqa: BLE001 - no pointer means no air mouse
                debug_log(f"air mouse not started: pointer output unavailable ({type(exc).__name__})", "gestures")
                raise AirMouseUnavailable("the displays and pointer could not be read") from exc
            camera = int(getattr(self._cfg, "air_mouse_camera", 0) or 0)
            tracker = unsubscribe = None
            try:
                tracker = self._tracker_factory(camera)
                unsubscribe = tracker.subscribe(mouse.on_frame)
                started = bool(tracker.start())
            except Exception as exc:  # noqa: BLE001 - treated like a tracker that could not start
                debug_log(f"air mouse: hand tracker failed to start ({type(exc).__name__})", "gestures")
                started = False
            if not started:
                if unsubscribe is not None:
                    unsubscribe()
                mouse.close()
                if tracker is not None:
                    try:
                        tracker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                debug_log("air mouse not started: hand tracking unavailable", "gestures")
                raise AirMouseUnavailable("hand tracking is not available (MediaPipe or its hand model could not "
                                          "be loaded)")
            stop_watchdog = threading.Event()
            watchdog = threading.Thread(target=self._watch, args=(mouse, stop_watchdog), name="air-mouse-watchdog",
                                        daemon=True)
            self._tracker, self._mouse, self._unsubscribe = tracker, mouse, unsubscribe
            self._stop_watchdog, self._watchdog = stop_watchdog, watchdog
            if not self._exit_hook:
                atexit.register(self.stop)
                self._exit_hook = True
            watchdog.start()
        debug_log(f"air mouse started on camera {camera}", "gestures")
        print("🖐️ Air mouse on\n"
              "   👉 Point with your index finger to move the pointer\n"
              "   🤏 Pinch to click, hold the pinch to drag\n"
              "   ✋ Open your hand to pause\n"
              "   🗣️ Say \"stop gesture control\" to turn it off", flush=True)
        return "started"

    def stop(self) -> bool:
        """Let go of the button, then release the camera. Returns whether the air mouse was on."""
        with self._lock:
            tracker, mouse, unsubscribe = self._tracker, self._mouse, self._unsubscribe
            stop_watchdog, watchdog = self._stop_watchdog, self._watchdog
            self._tracker = self._mouse = self._unsubscribe = None
            self._stop_watchdog = self._watchdog = None
        if mouse is None:
            return False
        if unsubscribe is not None:
            unsubscribe()
        mouse.close()
        if stop_watchdog is not None:
            stop_watchdog.set()
        try:
            tracker.stop()
        except Exception as exc:  # noqa: BLE001 - the button is already released
            debug_log(f"air mouse: hand tracker did not stop cleanly ({type(exc).__name__})", "gestures")
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=1.0)
        debug_log("air mouse stopped", "gestures")
        print("🖐️ Air mouse off", flush=True)
        return True

    @staticmethod
    def _watch(mouse: AirMouse, stop: threading.Event) -> None:
        while not stop.wait(WATCHDOG_SEC):
            try:
                mouse.tick()
            except Exception as exc:  # noqa: BLE001 - the watchdog must keep running
                debug_log(f"air mouse watchdog error ({type(exc).__name__})", "gestures")


_service: Optional[AirMouseService] = None


def set_service(service: Optional[AirMouseService]) -> None:
    """Install the daemon's air mouse service, stopping any previous one."""
    global _service
    previous, _service = _service, service
    if previous is not None and previous is not service:
        previous.stop()


def get_service() -> Optional[AirMouseService]:
    return _service


def stop_service() -> None:
    """Stop the air mouse if it is on (daemon shutdown)."""
    if _service is not None:
        _service.stop()

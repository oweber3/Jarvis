"""Gesture snapping as a mode that can be turned on and off: one hand tracker, one ``WindowSnap`` and a watchdog.

The camera runs only while the mode is on. ``stop`` lets go of a held window before it releases the camera, and
runs at process exit too. See ``gestures.spec.md``, Gesture snapping.
"""
from __future__ import annotations

import atexit
import threading
from typing import Any, Callable, Optional

from ..debug import debug_log
from .hand_tracking import HandTracker
from .window_snap import WATCHDOG_SEC, WindowSnap, WindowSnapSettings

GESTURE_GUIDE = ("Point with your index finger to aim, pinch to grab the window under the pointer, move your hand "
                 "and let go to snap it into a zone; open your hand to pause or to cancel a move.")


class WindowSnapUnavailable(Exception):
    """Gesture snapping could not start; the message says why in plain words."""


class WindowSnapService:
    """Starts and stops gesture snapping. ``output_factory`` makes the desktop output; ``tracker_factory`` takes
    the camera index and makes the hand tracker."""

    def __init__(self, cfg: Any, output_factory: Callable[[], Any],
                 tracker_factory: Callable[[int], Any] = HandTracker):
        self._cfg = cfg
        self._output_factory = output_factory
        self._tracker_factory = tracker_factory
        self._lock = threading.Lock()
        self._tracker: Any = None
        self._snap: Optional[WindowSnap] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._stop_watchdog: Optional[threading.Event] = None
        self._watchdog: Optional[threading.Thread] = None
        self._exit_hook = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._snap is not None

    @property
    def state(self) -> str:
        """The gesture snapping state for display, or ``off``."""
        with self._lock:
            snap = self._snap
        return snap.state if snap is not None else "off"

    def start(self) -> str:
        """``"started"`` or ``"already"``; raises ``WindowSnapUnavailable`` with nothing left running."""
        with self._lock:
            if self._snap is not None:
                return "already"
            try:
                snap = WindowSnap(self._output_factory(), WindowSnapSettings.from_config(self._cfg))
            except Exception as exc:  # noqa: BLE001 - no desktop output means no gesture snapping
                debug_log(f"gesture snapping not started: desktop output unavailable ({type(exc).__name__})",
                          "gestures")
                raise WindowSnapUnavailable("the displays could not be read") from exc
            camera = int(getattr(self._cfg, "gesture_snap_camera", 0) or 0)
            tracker = unsubscribe = None
            try:
                tracker = self._tracker_factory(camera)
                unsubscribe = tracker.subscribe(snap.on_frame)
                started = bool(tracker.start())
            except Exception as exc:  # noqa: BLE001 - treated like a tracker that could not start
                debug_log(f"gesture snapping: hand tracker failed to start ({type(exc).__name__})", "gestures")
                started = False
            if not started:
                if unsubscribe is not None:
                    unsubscribe()
                snap.close()
                if tracker is not None:
                    try:
                        tracker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                debug_log("gesture snapping not started: hand tracking unavailable", "gestures")
                raise WindowSnapUnavailable("hand tracking is not available (MediaPipe or its hand model could not "
                                            "be loaded)")
            stop_watchdog = threading.Event()
            watchdog = threading.Thread(target=self._watch, args=(snap, stop_watchdog),
                                        name="gesture-snap-watchdog", daemon=True)
            self._tracker, self._snap, self._unsubscribe = tracker, snap, unsubscribe
            self._stop_watchdog, self._watchdog = stop_watchdog, watchdog
            if not self._exit_hook:
                atexit.register(self.stop)
                self._exit_hook = True
            watchdog.start()
        debug_log(f"gesture snapping started on camera {camera}", "gestures")
        _say("🖐️ Gesture snapping on\n"
             "   👉 Point with your index finger to aim the pointer\n"
             "   🤏 Pinch to grab the window under it, let go to snap it into a zone\n"
             "   ✋ Open your hand to pause, or to cancel a move\n"
             "   🗣️ Say \"stop gesture control\" to turn it off")
        return "started"

    def stop(self) -> bool:
        """Let go of a held window, then release the camera. Returns whether gesture snapping was on."""
        with self._lock:
            tracker, snap, unsubscribe = self._tracker, self._snap, self._unsubscribe
            stop_watchdog, watchdog = self._stop_watchdog, self._watchdog
            self._tracker = self._snap = self._unsubscribe = None
            self._stop_watchdog = self._watchdog = None
        if snap is None:
            return False
        if unsubscribe is not None:
            unsubscribe()
        snap.close()
        if stop_watchdog is not None:
            stop_watchdog.set()
        try:
            tracker.stop()
        except Exception as exc:  # noqa: BLE001 - the window is already let go
            debug_log(f"gesture snapping: hand tracker did not stop cleanly ({type(exc).__name__})", "gestures")
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=1.0)
        debug_log("gesture snapping stopped", "gestures")
        _say("🖐️ Gesture snapping off")
        return True

    @staticmethod
    def _watch(snap: WindowSnap, stop: threading.Event) -> None:
        while not stop.wait(WATCHDOG_SEC):
            try:
                snap.tick()
            except Exception as exc:  # noqa: BLE001 - the watchdog must keep running
                debug_log(f"gesture snapping watchdog error ({type(exc).__name__})", "gestures")


def _say(text: str) -> None:
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001 - a console that cannot print never stops starting or stopping
        pass


_service: Optional[WindowSnapService] = None


def set_service(service: Optional[WindowSnapService]) -> None:
    """Install the daemon's gesture snapping service, stopping any previous one."""
    global _service
    previous, _service = _service, service
    if previous is not None and previous is not service:
        previous.stop()


def get_service() -> Optional[WindowSnapService]:
    return _service


def stop_service() -> None:
    """Stop gesture snapping if it is on (daemon shutdown)."""
    if _service is not None:
        _service.stop()

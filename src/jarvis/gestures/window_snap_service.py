"""Gesture snapping as a mode that can be turned on and off: one hand tracker, one ``WindowSnap`` and a watchdog.

The camera runs only while the mode is on. ``stop`` lets go of a held window before it releases the camera, and
runs at process exit too. A raised hand calls the talk handler the daemon sets, crossed wrists stop the mode, and
``click`` clicks at the pointer for a spoken click. See ``gestures.spec.md``, Gesture snapping.
"""
from __future__ import annotations

import atexit
import threading
from typing import Any, Callable, Optional

from ..debug import debug_log
from . import snap_state
from .hand_tracking import IDLE_FPS, HandTracker
from .window_snap import SWITCH_OFF, TALK, WATCHDOG_SEC, WindowSnap, WindowSnapSettings

GESTURE_GUIDE = ("Raise one finger to aim, make a fist to grab the window under the pointer and move it, "
                 "then raise a finger or open your hand to snap it into a zone; raise two fingers and hold your hand "
                 "above or below where you started to scroll; open your hand flat to pause, and hold it up to talk "
                 "to me. Say click, double click or right click to click at the pointer. Pull two open hands apart "
                 "or together to zoom, and cross your wrists to turn gesture control off.")

_talk_handler: Optional[Callable[[], None]] = None


def set_talk_handler(handler: Optional[Callable[[], None]]) -> None:
    """What a raised hand calls to start listening (the daemon's ``listen_now``), or None."""
    global _talk_handler
    _talk_handler = handler


class WindowSnapUnavailable(Exception):
    """Gesture snapping could not start; the message says why in plain words."""


def snap_tracker(cfg: Any, camera: int, **kwargs: Any) -> HandTracker:
    """The hand tracker gesture snapping runs on: one hand unless a named side needs both in view, and the idle
    rate while no hand is in view. ``kwargs`` (test doubles) go to ``HandTracker``."""
    settings = WindowSnapSettings.from_config(cfg)
    one = settings.hand == "any" and not settings.two_hands
    return HandTracker(camera, max_hands=1 if one else 2, idle_fps=IDLE_FPS, **kwargs)


class WindowSnapService:
    """Starts and stops gesture snapping. ``output_factory`` makes the desktop output; ``tracker_factory`` takes
    the camera index and makes the hand tracker (``snap_tracker`` by default)."""

    def __init__(self, cfg: Any, output_factory: Callable[[], Any],
                 tracker_factory: Optional[Callable[[int], Any]] = None):
        self._cfg = cfg
        self._output_factory = output_factory
        self._tracker_factory = tracker_factory or (lambda camera: snap_tracker(cfg, camera))
        self._lock = threading.Lock()
        self._tracker: Any = None
        self._snap: Optional[WindowSnap] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._stop_watchdog: Optional[threading.Event] = None
        self._watchdog: Optional[threading.Thread] = None
        self._exit_hook = False
        self._no_talk_said = False

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
                snap = WindowSnap(self._output_factory(), WindowSnapSettings.from_config(self._cfg),
                                  on_event=self._on_event)
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
        snap_state.publish(True)
        debug_log(f"gesture snapping started on camera {camera}", "gestures")
        _say("🖐️ Gesture snapping on\n"
             "   ☝️ Raise one finger to aim the pointer\n"
             "   ✊ Make a fist to grab the window under it, raise a finger or open your hand to snap it into a zone\n"
             "   ✌️ Raise two fingers, then hold your hand higher or lower to scroll (further is faster)\n"
             "   ✋ Open your hand flat to pause, hold it up for a second to talk to Jarvis\n"
             "   🖱️ Say \"click\", \"double click\" or \"right click\" to click at the pointer\n"
             "   👐 Pull two open hands apart or together to zoom\n"
             "   🙅 Cross your wrists, or say \"stop gesture control\", to turn it off")
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
        snap_state.publish(False)  # after the watchdog, so its heartbeat cannot turn the flag back on
        debug_log("gesture snapping stopped", "gestures")
        _say("🖐️ Gesture snapping off")
        return True

    def click(self, kind: str) -> str:
        """Click at the pointer for a spoken click: ``"clicked"``, ``"busy"`` (holding, scrolling or zooming) or
        ``"off"``. Raises ``OSError`` when Windows refuses it."""
        with self._lock:
            snap = self._snap
        if snap is None:
            return "off"
        return snap.click(kind)

    def _on_event(self, event: str) -> None:
        """From the detection thread: hand the work to a short-lived thread so tracking never waits, and so stopping
        never runs on the thread it stops."""
        if event == TALK:
            work = self._talk
        elif event == SWITCH_OFF:
            work = self.stop
        else:
            return
        threading.Thread(target=work, name=f"gesture-snap-{event.replace(' ', '-')}", daemon=True).start()

    def _talk(self) -> None:
        handler = _talk_handler
        if handler is None:
            if not self._no_talk_said:
                self._no_talk_said = True
                _say("🖐️ Listening by raising a hand is not available here")
            return
        try:
            handler()
        except Exception as exc:  # noqa: BLE001 - a failed wake never stops gesture snapping
            debug_log(f"gesture snapping: raised hand could not start listening ({type(exc).__name__})", "gestures")

    @staticmethod
    def _watch(snap: WindowSnap, stop: threading.Event) -> None:
        while not stop.wait(WATCHDOG_SEC):
            snap_state.publish(True)  # the heartbeat the desktop app's pointer ring reads
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
    """Stop gesture snapping if it is on and forget the service (daemon shutdown), so a daemon started again in
    the same process builds a new one from its own settings."""
    global _service
    service, _service = _service, None
    if service is not None:
        service.stop()
        debug_log("gesture snapping service released", "gestures")

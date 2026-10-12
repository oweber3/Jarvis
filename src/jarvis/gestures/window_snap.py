"""Gesture snapping: the index fingertip aims the pointer, a pinch grabs the window under it, the window follows
the hand and letting go snaps it into a zone.

Pure gesture logic on ``HandFrame``s. The desktop is an output object (``desktop``, ``position``, ``move_to``,
``grab``, ``move_window``, ``drop``, ``put_back``) supplied by the caller, so this module knows nothing of
Windows or FancyZones. It never clicks. It is the only code that moves the pointer or windows this way, and
only from the user's own hand movements. See ``gestures.spec.md``, Gesture snapping.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from ..debug import debug_log
from .hand_tracking import Hand, HandFrame, Landmark

Rectangle = Tuple[int, int, int, int]

HAND_LOST_SEC = 0.25
WATCHDOG_SEC = 0.05
EXTENDED_FACTOR = 1.2
_DERIVATIVE_CUTOFF_HZ = 1.0
_FINGERS = ("INDEX", "MIDDLE", "RING", "PINKY")

_IDLE, _AIMING, _CLOSING, _HOLDING, _HOLDING_NOTHING = "idle", "aiming", "closing", "holding", "holding nothing"
_STATE_NAMES = {_IDLE: "paused", _AIMING: "aiming", _CLOSING: "pinch closing", _HOLDING: "holding",
                _HOLDING_NOTHING: "holding nothing"}


# --------------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowSnapSettings:
    """Thresholds and tuning. Times are in seconds; ``min_cutoff`` and ``beta`` tune the One Euro filter."""

    hand: str = "any"
    comfort_box: float = 0.5
    pinch_start: float = 0.25
    pinch_release: float = 0.35
    debounce: float = 0.05
    min_cutoff: float = 1.0
    beta: float = 0.005

    @classmethod
    def from_config(cls, cfg: Any) -> "WindowSnapSettings":
        default = cls()

        def number(key: str, fallback: float) -> float:
            value = getattr(cfg, key, fallback)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback

        hand = getattr(cfg, "gesture_snap_hand", default.hand)
        start = number("gesture_snap_pinch_start", default.pinch_start)
        release = number("gesture_snap_pinch_release", default.pinch_release)
        if release <= start:
            release = start + 0.1
        return cls(hand=hand if hand in ("any", "left", "right") else default.hand,
                   comfort_box=number("gesture_snap_comfort_box", default.comfort_box),
                   pinch_start=start, pinch_release=release,
                   debounce=number("gesture_snap_debounce_ms", default.debounce * 1000) / 1000,
                   min_cutoff=number("gesture_snap_smoothing", default.min_cutoff),
                   beta=number("gesture_snap_speed_response", default.beta))


# --------------------------------------------------------------------------------------------------
# Hand measures and geometry
# --------------------------------------------------------------------------------------------------

def _distance(a, b) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def pinch_ratio(hand: Hand, width: int, height: int) -> float:
    """Thumb tip to index tip, as a fraction of the hand size (wrist to middle finger base), both measured
    in the camera image in pixels. The tracker's depth estimate is not used: with the hand side-on to the
    camera it keeps touching fingertips apart."""
    def pixels(a, b) -> float:
        return math.hypot((a.x - b.x) * width, (a.y - b.y) * height)

    size = pixels(hand.point(Landmark.WRIST), hand.point(Landmark.MIDDLE_MCP))
    if size <= 1e-6:
        return math.inf
    return pixels(hand.point(Landmark.THUMB_TIP), hand.point(Landmark.INDEX_TIP)) / size


def is_open_palm(hand: Hand) -> bool:
    """All four fingers extended: each tip farther from the wrist than its middle joint, by a margin."""
    wrist = hand.world_point(Landmark.WRIST)
    for name in _FINGERS:
        tip = _distance(wrist, hand.world_point(getattr(Landmark, f"{name}_TIP")))
        pip = _distance(wrist, hand.world_point(getattr(Landmark, f"{name}_PIP")))
        if tip <= pip * EXTENDED_FACTOR:
            return False
    return True


def clamp_to_desktop(x: float, y: float, monitors: Sequence[Rectangle]) -> Tuple[float, float]:
    """The point itself when it is on a monitor, otherwise the nearest point of the nearest monitor.

    Rectangles have exclusive right and bottom edges, so the last pixel is ``right - 1``."""
    best: Optional[Tuple[float, float, float]] = None
    for left, top, right, bottom in monitors:
        cx = min(max(x, left), right - 1)
        cy = min(max(y, top), bottom - 1)
        gap = (cx - x) ** 2 + (cy - y) ** 2
        if gap == 0:
            return cx, cy
        if best is None or gap < best[0]:
            best = (gap, cx, cy)
    assert best is not None
    return best[1], best[2]


class OneEuroFilter:
    """Casiez, Roussel and Vogel's One Euro filter for one value: steady when slow, quick when fast."""

    def __init__(self, min_cutoff: float, beta: float, derivative_cutoff: float = _DERIVATIVE_CUTOFF_HZ):
        self._min_cutoff = min_cutoff
        self._beta = beta
        self._derivative_cutoff = derivative_cutoff
        self.reset()

    def reset(self) -> None:
        self._t: Optional[float] = None
        self._x = 0.0
        self._dx = 0.0

    @staticmethod
    def _alpha(dt: float, cutoff: float) -> float:
        return 1.0 / (1.0 + 1.0 / (2 * math.pi * cutoff * dt))

    def __call__(self, value: float, t: float) -> float:
        if self._t is None:
            self._t, self._x, self._dx = t, value, 0.0
            return value
        dt = t - self._t
        if dt <= 0:
            return self._x
        self._t = t
        a_d = self._alpha(dt, self._derivative_cutoff)
        self._dx = a_d * (value - self._x) / dt + (1 - a_d) * self._dx
        a = self._alpha(dt, self._min_cutoff + self._beta * abs(self._dx))
        self._x = a * value + (1 - a) * self._x
        return self._x




# --------------------------------------------------------------------------------------------------
# The gesture model
# --------------------------------------------------------------------------------------------------

class WindowSnap:
    """Turns ``HandFrame``s into pointer moves, window grabs, window moves and snaps on ``output``.

    ``on_frame`` runs on the detection thread, ``tick`` on a watchdog thread and ``close`` on whichever
    thread stops the mode; one lock serialises them."""

    def __init__(self, output: Any, settings: WindowSnapSettings, clock: Callable[[], float] = time.perf_counter):
        self._out = output
        self._s = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._monitors: List[Rectangle] = [tuple(int(v) for v in m) for m in output.desktop()]  # type: ignore
        if not self._monitors:
            raise ValueError("no monitors")
        self._desktop_width = max(m[2] for m in self._monitors) - min(m[0] for m in self._monitors)
        self._fx = OneEuroFilter(settings.min_cutoff, settings.beta)
        self._fy = OneEuroFilter(settings.min_cutoff, settings.beta)
        self._state = _IDLE
        self._side: Optional[str] = None
        self._tip: Optional[Tuple[float, float]] = None
        self._last_hand: Optional[float] = None
        self._ref: Tuple[float, float] = (0.0, 0.0)
        self._cursor: Tuple[float, float] = (0.0, 0.0)
        self._shown: Optional[Tuple[int, int]] = None
        self._anchor: Tuple[float, float] = (0.0, 0.0)
        self._below_since: Optional[float] = None
        self._above_since: Optional[float] = None
        self._window: Any = None
        self._offset: Tuple[float, float] = (0.0, 0.0)
        self._placed: Optional[Tuple[int, int]] = None
        self._closed = False
        self._failing: set = set()

    @property
    def state(self) -> str:
        """What the hand is doing, for display: paused, aiming, pinch closing, holding or holding nothing."""
        return _STATE_NAMES[self._state]

    # -- entry points ----------------------------------------------------------------------------

    def on_frame(self, frame: HandFrame) -> None:
        with self._lock:
            if self._closed:
                return
            arrived = self._clock()
            chosen = self._choose(frame.hands)
            if chosen is None:
                self._check_lost(arrived)
                return
            self._last_hand = arrived
            tip = chosen.point(Landmark.INDEX_TIP)
            gain = self._desktop_width / (self._s.comfort_box * max(frame.width, 1))
            position = (self._fx(tip.x * frame.width * gain, frame.time),
                        self._fy(tip.y * frame.height * gain, frame.time))
            self._step(position, pinch_ratio(chosen, frame.width, frame.height), is_open_palm(chosen), frame.time)

    def tick(self) -> None:
        """Watchdog: let go once the hand has been missing too long, even when no frames arrive."""
        with self._lock:
            if not self._closed:
                self._check_lost(self._clock())

    def close(self) -> None:
        """Stop for good, leaving a held window where it is."""
        with self._lock:
            if self._window is not None:
                debug_log("gesture snapping: window left where it is (stopped)", "gestures")
            self._closed = True
            self._window = None
            self._state = _IDLE

    # -- hands -----------------------------------------------------------------------------------

    def _choose(self, hands: Sequence[Hand]) -> Optional[Hand]:
        """The driving hand. Once a hand drives, only it is followed (the hand nearest its last fingertip
        position, so two hands with the same label never swap); another hand can take over only from idle."""
        wanted = self._s.hand
        candidates = [h for h in hands if wanted == "any" or h.side == wanted]
        if self._side is not None and self._tip is not None:
            candidates = [h for h in candidates if h.side == self._side]
            if not candidates:
                return None
            last_x, last_y = self._tip
            chosen = min(candidates, key=lambda h: (h.point(Landmark.INDEX_TIP).x - last_x) ** 2
                         + (h.point(Landmark.INDEX_TIP).y - last_y) ** 2)
        elif candidates:
            chosen = max(candidates, key=lambda h: h.score)
        else:
            return None
        self._side = chosen.side
        tip = chosen.point(Landmark.INDEX_TIP)
        self._tip = (tip.x, tip.y)
        return chosen

    def _check_lost(self, now: float) -> None:
        if self._state == _IDLE or self._last_hand is None or now - self._last_hand < HAND_LOST_SEC:
            return
        if self._window is not None:
            debug_log("gesture snapping: window left where it is (hand lost)", "gestures")
        elif self._state == _CLOSING:
            debug_log("gesture snapping: pinch cancelled, hand lost", "gestures")
        self._window = None
        self._state = _IDLE
        self._side = None
        self._tip = None
        self._fx.reset()
        self._fy.reset()

    # -- the state machine -----------------------------------------------------------------------

    def _step(self, p: Tuple[float, float], ratio: float, palm: bool, t: float) -> None:
        s = self._s
        state = self._state
        if state == _IDLE:
            if not palm:
                self._begin(p)
            return
        if state == _AIMING:
            if palm:
                debug_log("gesture snapping: clutch (open palm)", "gestures")
                self._state = _IDLE
            elif ratio < s.pinch_release:
                self._state = _CLOSING
                self._anchor = self._cursor
                self._below_since = t if ratio < s.pinch_start else None
                self._ref = p
            else:
                self._move_by(p)
            return
        if state == _CLOSING:
            self._ref = p
            if palm:
                self._state = _IDLE
            elif ratio >= s.pinch_release:
                self._state = _AIMING
            elif ratio < s.pinch_start:
                if self._below_since is None:
                    self._below_since = t
                if t - self._below_since >= s.debounce:
                    self._grab()
            else:
                self._below_since = None
            return
        # Holding a window, or a pinch over nothing.
        if palm and self._window is not None:
            self._put_back()
            self._state = _IDLE
            return
        if ratio > s.pinch_release:
            self._ref = p  # hold the drop point still while the release is confirmed
            if self._above_since is None:
                self._above_since = t
            if t - self._above_since >= s.debounce:
                self._drop()
                self._state = _AIMING
            return
        self._above_since = None
        self._move_by(p)
        self._follow()

    def _begin(self, p: Tuple[float, float]) -> None:
        try:
            x, y = self._out.position()
        except Exception as exc:  # noqa: BLE001 - keep the last known position
            self._output_failed("read the pointer position", exc)
            x, y = self._cursor
        self._cursor = clamp_to_desktop(float(x), float(y), self._monitors)
        self._shown = (int(x), int(y))
        self._ref = p
        self._state = _AIMING

    def _move_by(self, p: Tuple[float, float]) -> None:
        x = self._cursor[0] + p[0] - self._ref[0]
        y = self._cursor[1] + p[1] - self._ref[1]
        self._ref = p
        self._cursor = clamp_to_desktop(x, y, self._monitors)
        self._show(self._cursor)

    def _show(self, point: Tuple[float, float]) -> None:
        target = (int(round(point[0])), int(round(point[1])))
        if target == self._shown:
            return
        try:
            self._out.move_to(*target)
        except Exception as exc:  # noqa: BLE001 - the next frame tries again
            self._output_failed("move the pointer", exc)
            return
        self._shown = target
        self._failing.discard("move the pointer")

    # -- windows ---------------------------------------------------------------------------------

    def _grab(self) -> None:
        self._cursor = self._anchor
        self._above_since = None
        x, y = int(round(self._anchor[0])), int(round(self._anchor[1]))
        try:
            window = self._out.grab(x, y)
        except Exception as exc:  # noqa: BLE001 - a pinch that grabs nothing
            self._output_failed("grab the window", exc)
            window = None
        if window is None:
            self._state = _HOLDING_NOTHING
            debug_log("gesture snapping: pinch over no window", "gestures")
            return
        self._window = window
        left, top = window.rect[0], window.rect[1]
        self._offset = (x - left, y - top)
        self._placed = (left, top)
        self._state = _HOLDING
        debug_log("gesture snapping: window grabbed", "gestures")

    def _follow(self) -> None:
        if self._window is None:
            return
        left = int(round(self._cursor[0] - self._offset[0]))
        top = int(round(self._cursor[1] - self._offset[1]))
        if (left, top) == self._placed:
            return
        try:
            self._out.move_window(self._window, left, top)
        except Exception as exc:  # noqa: BLE001 - let go of a window that refuses to move
            self._output_failed("move the window", exc)
            self._window = None
            self._state = _HOLDING_NOTHING
            return
        self._placed = (left, top)

    def _drop(self) -> None:
        window, self._window = self._window, None
        if window is None:
            return
        x, y = int(round(self._cursor[0])), int(round(self._cursor[1]))
        try:
            self._out.drop(window, x, y)
        except Exception as exc:  # noqa: BLE001
            self._output_failed("snap the window", exc)
            return
        debug_log("gesture snapping: window dropped", "gestures")

    def _put_back(self) -> None:
        window, self._window = self._window, None
        try:
            self._out.put_back(window)
        except Exception as exc:  # noqa: BLE001
            self._output_failed("put the window back", exc)
            return
        debug_log("gesture snapping: cancelled, window put back", "gestures")

    def _output_failed(self, what: str, exc: Exception) -> None:
        """Logged and printed once per kind of failure until it next succeeds, so retries stay quiet."""
        if what in self._failing:
            return
        self._failing.add(what)
        debug_log(f"gesture snapping could not {what} ({type(exc).__name__})", "gestures")
        try:
            print(f"🖐️ Gesture snapping could not {what}\n"
                  "   💡 Windows blocks moving apps that run as administrator, and input while the PC is locked",
                  flush=True)
        except Exception:  # noqa: BLE001 - a console that cannot print never stops tracking
            pass

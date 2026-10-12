"""Gesture snapping by raised fingers: one finger aims the pointer, a fist grabs the window under it and the window
follows the hand, raising a finger snaps it into a zone, two fingers scroll, an open hand pauses.

Pure gesture logic on ``HandFrame``s. The desktop is an output object (``desktop``, ``position``, ``move_to``,
``grab``, ``move_window``, ``drop``, ``scroll``) supplied by the caller, so this module knows nothing of
Windows or FancyZones. It never clicks. It is the only code that moves the pointer or windows this way, and
only from the user's own hand movements. See ``gestures.spec.md``, Gesture snapping.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from ..debug import debug_log
from .hand_tracking import Hand, HandFrame, Landmark

Rectangle = Tuple[int, int, int, int]

HAND_LOST_SEC = 0.25
# Leaving a scroll needs the new gesture to last at least this long: the raised fingers bend as the hand moves.
SCROLL_EXIT_SEC = 0.25
# After a gesture change the pointer's distance from the palm's place fades with this time constant.
CATCH_UP_SEC = 0.15
_PALM = (Landmark.WRIST, Landmark.INDEX_MCP, Landmark.MIDDLE_MCP, Landmark.RING_MCP, Landmark.PINKY_MCP)
WATCHDOG_SEC = 0.05
WHEEL_NOTCH = 120
# Wheel units are sent once at least this many have built up, so slow movements do not flood the window.
MIN_WHEEL_STEP = 30
_DERIVATIVE_CUTOFF_HZ = 1.0
FINGERS = ("INDEX", "MIDDLE", "RING", "PINKY")

ONE, TWO, FIST, OPEN, OTHER = "one finger", "two fingers", "fist", "open hand", "other"
_SHAPES: Dict[FrozenSet[str], str] = {
    frozenset({"INDEX"}): ONE,
    frozenset({"INDEX", "MIDDLE"}): TWO,
    frozenset(): FIST,
    frozenset(FINGERS): OPEN,
}

_IDLE, _AIMING, _HOLDING, _HOLDING_NOTHING, _SCROLLING = "idle", "aiming", "holding", "holding nothing", "scrolling"
_STATE_NAMES = {_IDLE: "paused", _AIMING: "aiming", _HOLDING: "holding", _HOLDING_NOTHING: "holding nothing",
                _SCROLLING: "scrolling"}


# --------------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowSnapSettings:
    """Thresholds and tuning. Times are in seconds; ``min_cutoff`` and ``beta`` tune the One Euro filter."""

    hand: str = "any"
    comfort_box: float = 0.5
    finger_up: float = 1.1
    finger_down: float = 0.95
    debounce: float = 0.1
    scroll_speed: float = 5.0
    min_cutoff: float = 1.0
    beta: float = 0.005

    @classmethod
    def from_config(cls, cfg: Any) -> "WindowSnapSettings":
        default = cls()

        def number(key: str, fallback: float) -> float:
            value = getattr(cfg, key, fallback)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback

        hand = getattr(cfg, "gesture_snap_hand", default.hand)
        up = number("gesture_snap_finger_up", default.finger_up)
        down = number("gesture_snap_finger_down", default.finger_down)
        if down >= up:
            down = up - 0.1
        return cls(hand=hand if hand in ("any", "left", "right") else default.hand,
                   comfort_box=number("gesture_snap_comfort_box", default.comfort_box),
                   finger_up=up, finger_down=down,
                   debounce=number("gesture_snap_debounce_ms", default.debounce * 1000) / 1000,
                   scroll_speed=number("gesture_snap_scroll_speed", default.scroll_speed),
                   min_cutoff=number("gesture_snap_smoothing", default.min_cutoff),
                   beta=number("gesture_snap_speed_response", default.beta))


# --------------------------------------------------------------------------------------------------
# Hand measures and geometry
# --------------------------------------------------------------------------------------------------

def _distance(a, b) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def finger_curl(hand: Hand, finger: str) -> float:
    """Wrist to fingertip over wrist to the finger's middle joint, on the world landmarks: well above 1 when the
    finger is raised, below 1 when it is folded."""
    wrist = hand.world_point(Landmark.WRIST)
    pip = _distance(wrist, hand.world_point(getattr(Landmark, f"{finger}_PIP")))
    if pip <= 1e-6:
        return 0.0
    return _distance(wrist, hand.world_point(getattr(Landmark, f"{finger}_TIP"))) / pip


def palm_centre(hand: Hand) -> Tuple[float, float]:
    """The image position of the middle of the palm (wrist and the four finger bases), which stays put when
    fingers fold or rise."""
    return (sum(hand.point(i).x for i in _PALM) / len(_PALM), sum(hand.point(i).y for i in _PALM) / len(_PALM))


def describe_hand(hand: Hand, settings: Optional["WindowSnapSettings"] = None) -> str:
    """Each finger's curl and the gesture they make on their own (a finger between the thresholds counts as
    down), for the diagnostic readout and preview."""
    s = settings or WindowSnapSettings()
    curls = {f: finger_curl(hand, f) for f in FINGERS}
    raised = frozenset(f for f, c in curls.items() if c > s.finger_up)
    names = {"INDEX": "index", "MIDDLE": "middle", "RING": "ring", "PINKY": "little"}
    return " ".join(f"{names[f]} {curls[f]:.2f}" for f in FINGERS) + f" · {shape_of(raised)}"


def shape_of(raised: FrozenSet[str]) -> str:
    """The gesture a set of raised fingers makes: one finger, two fingers, fist, open hand or other."""
    return _SHAPES.get(raised, OTHER)


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
    """Turns ``HandFrame``s into pointer moves, window grabs, window moves, snaps and scrolling on ``output``.

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
        left, right = min(m[0] for m in self._monitors), max(m[2] for m in self._monitors)
        top, bottom = min(m[1] for m in self._monitors), max(m[3] for m in self._monitors)
        self._desktop_width = right - left
        self._centre = ((left + right) / 2, (top + bottom) / 2)
        self._fx = OneEuroFilter(settings.min_cutoff, settings.beta)
        self._fy = OneEuroFilter(settings.min_cutoff, settings.beta)
        self._state = _IDLE
        self._drift: Tuple[float, float] = (0.0, 0.0)
        self._box: Optional[Tuple[float, float]] = None  # the camera position the comfort box is centred on
        self._last_t: Optional[float] = None
        self._side: Optional[str] = None
        self._tip: Optional[Tuple[float, float]] = None
        self._last_hand: Optional[float] = None
        self._raised: Dict[str, bool] = {}
        self._gesture: Optional[str] = None
        self._candidate: Optional[str] = None
        self._candidate_since = 0.0
        self._ref: Tuple[float, float] = (0.0, 0.0)
        self._cursor: Tuple[float, float] = (0.0, 0.0)
        self._shown: Optional[Tuple[int, int]] = None
        self._window: Any = None
        self._offset: Tuple[float, float] = (0.0, 0.0)
        self._placed: Optional[Tuple[int, int]] = None
        self._wheel = 0.0
        self._closed = False
        self._failing: set = set()

    @property
    def state(self) -> str:
        """For display: paused, aiming, hand changing, holding, holding nothing or scrolling."""
        if self._candidate is not None and self._state != _IDLE:
            return "hand changing"
        return _STATE_NAMES[self._state]

    @property
    def gesture(self) -> Optional[str]:
        """The gesture in force: one finger, two fingers, fist, open hand, other, or ``None`` without a hand."""
        return self._gesture

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
            # Absolute: the comfort box, centred where the palm was when the hand started driving, spans the
            # desktop's bounding rectangle.
            px, py = palm_centre(chosen)
            if self._box is None:
                self._box = (px, py)
                debug_log("gesture snapping: comfort box centred on the hand", "gestures")
            gain = self._desktop_width / (self._s.comfort_box * max(frame.width, 1))
            position = (self._fx(self._centre[0] + (px - self._box[0]) * frame.width * gain, frame.time),
                        self._fy(self._centre[1] + (py - self._box[1]) * frame.height * gain, frame.time))
            self._step(position, self._shape(chosen), frame.time, frame.height * gain)

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
        """The driving hand. Once a hand drives, only it is followed (the hand nearest its last palm position,
        so two hands with the same label never swap); another hand can take over only after it is lost."""
        wanted = self._s.hand
        candidates = [h for h in hands if wanted == "any" or h.side == wanted]
        if self._side is not None and self._tip is not None:
            candidates = [h for h in candidates if h.side == self._side]
            if not candidates:
                return None
            last_x, last_y = self._tip
            chosen = min(candidates, key=lambda h: (palm_centre(h)[0] - last_x) ** 2
                         + (palm_centre(h)[1] - last_y) ** 2)
        elif candidates:
            chosen = max(candidates, key=lambda h: h.score)
        else:
            return None
        self._side = chosen.side
        self._tip = palm_centre(chosen)
        return chosen

    def _shape(self, hand: Hand) -> str:
        """Each finger up or down with hysteresis (between the thresholds it keeps its state), then the shape."""
        for finger in FINGERS:
            curl = finger_curl(hand, finger)
            if curl > self._s.finger_up:
                self._raised[finger] = True
            elif curl < self._s.finger_down:
                self._raised[finger] = False
            else:
                self._raised.setdefault(finger, False)
        return shape_of(frozenset(f for f in FINGERS if self._raised[f]))

    def _check_lost(self, now: float) -> None:
        if self._last_hand is None or now - self._last_hand < HAND_LOST_SEC:
            return
        if self._state == _IDLE and self._gesture is None and self._candidate is None:
            return
        if self._window is not None:
            debug_log("gesture snapping: window left where it is (hand lost)", "gestures")
        if self._state == _SCROLLING:
            self._end_scroll()
        self._window = None
        self._state = _IDLE
        self._gesture = self._candidate = None
        self._box = None
        self._raised.clear()
        self._side = None
        self._tip = None
        self._fx.reset()
        self._fy.reset()

    # -- the state machine -----------------------------------------------------------------------

    def _step(self, p: Tuple[float, float], shape: str, t: float, scale: float) -> None:
        dt = 0.0 if self._last_t is None else max(t - self._last_t, 0.0)
        self._last_t = t
        if shape == self._gesture:
            self._candidate = None
        else:
            if shape != self._candidate:
                self._candidate, self._candidate_since = shape, t
            needed = max(self._s.debounce, SCROLL_EXIT_SEC) if self._state == _SCROLLING else self._s.debounce
            if t - self._candidate_since >= needed:
                self._candidate = None
                self._confirm(shape, p)
            # Until the new gesture counts the pointer (and a held window) stays still.
            self._ref = p
            return
        state = self._state
        if state == _AIMING:
            self._aim(p, dt)
        elif state == _HOLDING:
            self._aim(p, dt)
            self._follow()
        elif state == _SCROLLING:
            self._scroll_by(p, scale)
        else:
            self._ref = p

    def _confirm(self, shape: str, p: Tuple[float, float]) -> None:
        """A new gesture has lasted the debounce time: act on it."""
        previous, self._gesture = self._gesture, shape
        paused = self._state == _IDLE
        if self._state in (_HOLDING, _HOLDING_NOTHING):
            if not self._raised.get("INDEX"):
                return  # still a fist, perhaps with another finger loosening: keep holding
            self._drop()
            self._state = _AIMING
        if self._state == _SCROLLING and shape != TWO:
            self._end_scroll()
            if shape == FIST:
                # A fist grabs only after pointing: closing the hand from a scroll grabs nothing.
                self._state = _HOLDING_NOTHING
                return
            self._state = _AIMING
        if shape in (ONE, TWO, FIST):
            if paused:
                # Resuming after a pause: the pointer goes straight to the palm's place.
                self._drift = (0.0, 0.0)
                self._cursor = clamp_to_desktop(p[0], p[1], self._monitors)
                self._show(self._cursor)
            else:
                # The pointer continues from where it stood; the difference from the palm fades (see _aim).
                self._drift = (self._cursor[0] - p[0], self._cursor[1] - p[1])
        if shape == ONE:
            self._state = _AIMING
        elif shape == FIST:
            self._grab()
        elif shape == TWO:
            self._start_scroll()
        else:
            if previous in (ONE, TWO, FIST):
                debug_log(f"gesture snapping: paused ({shape})", "gestures")
            self._state = _IDLE

    def _aim(self, p: Tuple[float, float], dt: float) -> None:
        """Put the pointer at the palm's place, plus whatever difference is still fading after a gesture change."""
        fade = math.exp(-dt / CATCH_UP_SEC) if dt > 0 else 1.0
        self._drift = (self._drift[0] * fade, self._drift[1] * fade)
        if abs(self._drift[0]) < 0.5 and abs(self._drift[1]) < 0.5:
            self._drift = (0.0, 0.0)
        self._ref = p
        self._cursor = clamp_to_desktop(p[0] + self._drift[0], p[1] + self._drift[1], self._monitors)
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

    # -- scrolling -------------------------------------------------------------------------------

    def _start_scroll(self) -> None:
        self._wheel = 0.0
        self._state = _SCROLLING
        debug_log("gesture snapping: scroll started", "gestures")

    def _end_scroll(self) -> None:
        self._wheel = 0.0
        debug_log("gesture snapping: scroll ended", "gestures")

    def _scroll_by(self, p: Tuple[float, float], scale: float) -> None:
        """Vertical fingertip movement since the last frame, as a share of the camera's height, becomes wheel
        units: moving the hand up turns the wheel towards the user, which moves the content up."""
        moved = (p[1] - self._ref[1]) / max(scale, 1e-6)
        self._ref = p
        self._wheel += moved / 0.1 * self._s.scroll_speed * WHEEL_NOTCH
        if abs(self._wheel) < MIN_WHEEL_STEP:
            return
        step = int(self._wheel)
        try:
            self._out.scroll(step)
        except Exception as exc:  # noqa: BLE001 - the next movement tries again
            self._output_failed("scroll", exc)
            self._wheel = 0.0
            return
        self._wheel -= step
        self._failing.discard("scroll")

    # -- windows ---------------------------------------------------------------------------------

    def _grab(self) -> None:
        """Grab the window under the pointer, which has not moved since the hand started to close."""
        x, y = int(round(self._cursor[0])), int(round(self._cursor[1]))
        try:
            window = self._out.grab(x, y)
        except Exception as exc:  # noqa: BLE001 - a fist that grabs nothing
            self._output_failed("grab the window", exc)
            window = None
        else:
            self._failing.discard("grab the window")
        if window is None:
            self._state = _HOLDING_NOTHING
            debug_log("gesture snapping: fist over no window", "gestures")
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
        self._failing.discard("move the window")

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
        self._failing.discard("snap the window")
        debug_log("gesture snapping: window dropped", "gestures")

    def _output_failed(self, what: str, exc: Exception) -> None:
        """Logged and printed once per kind of failure until it next succeeds, so retries stay quiet."""
        if what in self._failing:
            return
        self._failing.add(what)
        debug_log(f"gesture snapping could not {what} ({type(exc).__name__})", "gestures")
        try:
            print(f"🖐️ Gesture snapping could not {what}\n"
                  "   💡 Windows blocks moving or scrolling apps that run as administrator, and input while the PC "
                  "is locked",
                  flush=True)
        except Exception:  # noqa: BLE001 - a console that cannot print never stops tracking
            pass

"""The air mouse: the index fingertip moves the pointer, a pinch clicks, a held pinch drags.

Pure gesture logic on ``HandFrame``s. The pointer itself is an output object (``desktop``, ``position``,
``move_to``, ``press``, ``release``) supplied by the caller, so this module knows nothing of Windows. It is
the only code that sends pointer input, and only from the user's own hand movements. See
``gestures.spec.md``, Air mouse.
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

_IDLE, _TRACKING, _ARMING, _PINCHED, _DRAGGING = "idle", "tracking", "arming", "pinched", "dragging"


# --------------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AirMouseSettings:
    """Thresholds and tuning. Times are in seconds; ``min_cutoff`` and ``beta`` tune the One Euro filter."""

    hand: str = "any"
    comfort_box: float = 0.5
    pinch_start: float = 0.25
    pinch_release: float = 0.40
    debounce: float = 0.05
    hold: float = 0.35
    min_cutoff: float = 1.0
    beta: float = 0.005

    @classmethod
    def from_config(cls, cfg: Any) -> "AirMouseSettings":
        default = cls()

        def number(key: str, fallback: float) -> float:
            value = getattr(cfg, key, fallback)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback

        hand = getattr(cfg, "air_mouse_hand", default.hand)
        start = number("air_mouse_pinch_start", default.pinch_start)
        release = number("air_mouse_pinch_release", default.pinch_release)
        if release <= start:
            release = start + 0.1
        return cls(hand=hand if hand in ("any", "left", "right") else default.hand,
                   comfort_box=number("air_mouse_comfort_box", default.comfort_box),
                   pinch_start=start, pinch_release=release,
                   debounce=number("air_mouse_debounce_ms", default.debounce * 1000) / 1000,
                   hold=number("air_mouse_hold_ms", default.hold * 1000) / 1000,
                   min_cutoff=number("air_mouse_smoothing", default.min_cutoff),
                   beta=number("air_mouse_speed_response", default.beta))


# --------------------------------------------------------------------------------------------------
# Hand measures and geometry
# --------------------------------------------------------------------------------------------------

def _distance(a, b) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def pinch_ratio(hand: Hand) -> float:
    """Thumb tip to index tip, as a fraction of the hand size (wrist to middle finger base)."""
    size = _distance(hand.world_point(Landmark.WRIST), hand.world_point(Landmark.MIDDLE_MCP))
    if size <= 1e-6:
        return math.inf
    return _distance(hand.world_point(Landmark.THUMB_TIP), hand.world_point(Landmark.INDEX_TIP)) / size


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

class AirMouse:
    """Turns ``HandFrame``s into pointer moves and left-button presses on ``output``.

    ``on_frame`` runs on the detection thread, ``tick`` on a watchdog thread and ``close`` on whichever
    thread stops the mode; one lock serialises them."""

    def __init__(self, output: Any, settings: AirMouseSettings, clock: Callable[[], float] = time.perf_counter):
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
        self._last_hand: Optional[float] = None
        self._ref: Tuple[float, float] = (0.0, 0.0)
        self._cursor: Tuple[float, float] = (0.0, 0.0)
        self._shown: Optional[Tuple[int, int]] = None
        self._anchor: Tuple[float, float] = (0.0, 0.0)
        self._below_since: Optional[float] = None
        self._above_since: Optional[float] = None
        self._pinch_started = 0.0
        self._pressed = False
        self._closed = False
        self._failed_once = False

    # -- entry points ----------------------------------------------------------------------------

    def on_frame(self, frame: HandFrame) -> None:
        with self._lock:
            if self._closed:
                return
            chosen = self._choose(frame.hands)
            if chosen is None:
                self._check_lost(frame.time)
                return
            self._last_hand = frame.time
            tip = chosen.point(Landmark.INDEX_TIP)
            gain = self._desktop_width / (self._s.comfort_box * max(frame.width, 1))
            position = (self._fx(tip.x * frame.width * gain, frame.time),
                        self._fy(tip.y * frame.height * gain, frame.time))
            self._step(position, pinch_ratio(chosen), is_open_palm(chosen), frame.time)

    def tick(self) -> None:
        """Watchdog: let go once the hand has been missing too long, even when no frames arrive."""
        with self._lock:
            if self._closed:
                return
            self._check_lost(self._clock())
            if self._pressed and self._state != _DRAGGING:
                self._release_button("retry")

    def close(self) -> None:
        """Stop for good, releasing the button if the air mouse holds it."""
        with self._lock:
            self._closed = True
            if self._pressed:
                self._release_button("stopped")
            self._state = _IDLE

    # -- hands -----------------------------------------------------------------------------------

    def _choose(self, hands: Sequence[Hand]) -> Optional[Hand]:
        wanted = self._s.hand
        candidates = [h for h in hands if wanted == "any" or h.side == wanted]
        if not candidates:
            return None
        for h in candidates:
            if h.side == self._side:
                return h
        chosen = max(candidates, key=lambda h: h.score)
        self._side = chosen.side
        return chosen

    def _check_lost(self, now: float) -> None:
        if self._state == _IDLE or self._last_hand is None or now - self._last_hand < HAND_LOST_SEC:
            return
        if self._state in (_ARMING, _PINCHED):
            debug_log("air mouse: pinch cancelled, hand lost", "gestures")
        self._reset("hand lost")

    def _reset(self, reason: str) -> None:
        if self._pressed:
            self._release_button(reason)
        self._state = _IDLE
        self._side = None
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
        if state == _TRACKING:
            if palm:
                debug_log("air mouse: clutch (open palm)", "gestures")
                self._state = _IDLE
            elif ratio < s.pinch_release:
                self._state = _ARMING
                self._anchor = self._cursor
                self._below_since = t if ratio < s.pinch_start else None
                self._ref = p
            else:
                self._move_by(p)
            return
        if state == _ARMING:
            self._ref = p
            if ratio >= s.pinch_release:
                self._state = _TRACKING
            elif ratio < s.pinch_start:
                if self._below_since is None:
                    self._below_since = t
                if t - self._below_since >= s.debounce:
                    self._state = _PINCHED
                    self._pinch_started = self._below_since
                    self._above_since = None
            else:
                self._below_since = None
            return
        if state == _PINCHED:
            self._ref = p
            if ratio > s.pinch_release:
                if self._above_since is None:
                    self._above_since = t
                if t - self._above_since >= s.debounce:
                    self._click()
                    self._state = _TRACKING
                return
            self._above_since = None
            if t - self._pinch_started >= s.hold:
                self._start_drag()
            return
        if state == _DRAGGING:
            if ratio > s.pinch_release:
                self._ref = p  # hold the drop point still while the release is confirmed
                if self._above_since is None:
                    self._above_since = t
                if t - self._above_since >= s.debounce:
                    self._release_button("drop")
                    self._state = _TRACKING
                return
            self._above_since = None
            self._move_by(p)

    def _begin(self, p: Tuple[float, float]) -> None:
        try:
            x, y = self._out.position()
        except Exception as exc:  # noqa: BLE001 - keep the last known position
            self._output_failed("read the pointer position", exc)
            x, y = self._cursor
        self._cursor = clamp_to_desktop(float(x), float(y), self._monitors)
        self._shown = (int(x), int(y))
        self._ref = p
        self._state = _TRACKING

    def _move_by(self, p: Tuple[float, float]) -> None:
        x = self._cursor[0] + p[0] - self._ref[0]
        y = self._cursor[1] + p[1] - self._ref[1]
        self._ref = p
        self._cursor = clamp_to_desktop(x, y, self._monitors)
        self._show(self._cursor)

    def _show(self, point: Tuple[float, float], force: bool = False) -> bool:
        target = (int(round(point[0])), int(round(point[1])))
        if target == self._shown and not force:
            return True
        try:
            self._out.move_to(*target)
        except Exception as exc:  # noqa: BLE001 - the next frame tries again
            self._output_failed("move the pointer", exc)
            return False
        self._shown = target
        return True

    # -- buttons ---------------------------------------------------------------------------------

    def _click(self) -> None:
        self._cursor = self._anchor
        if not self._show(self._anchor, force=True):
            return
        try:
            self._out.press()
        except Exception as exc:  # noqa: BLE001
            self._output_failed("click", exc)
            return
        self._pressed = True
        self._release_button("click")
        debug_log("air mouse: click", "gestures")

    def _start_drag(self) -> None:
        self._cursor = self._anchor
        self._state = _DRAGGING
        self._above_since = None
        if not self._show(self._anchor, force=True):
            return
        try:
            self._out.press()
        except Exception as exc:  # noqa: BLE001 - the drag moves the pointer without the button
            self._output_failed("press the button", exc)
            return
        self._pressed = True
        debug_log("air mouse: drag started", "gestures")

    def _release_button(self, reason: str) -> None:
        try:
            self._out.release()
        except Exception as exc:  # noqa: BLE001 - stays marked as pressed so the watchdog retries
            self._output_failed("release the button", exc)
            return
        self._pressed = False
        if reason != "click":
            debug_log(f"air mouse: button released ({reason})", "gestures")

    def _output_failed(self, what: str, exc: Exception) -> None:
        debug_log(f"air mouse could not {what} ({type(exc).__name__})", "gestures")
        if not self._failed_once:
            self._failed_once = True
            print(f"🖐️ Air mouse could not {what}\n"
                  "   💡 Windows blocks input to windows running as administrator", flush=True)

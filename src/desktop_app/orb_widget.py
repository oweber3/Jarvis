"""
Reactive orb for the Jarvis face window: a sphere of light that moves with the voice.

Split in layers so each can be improved independently:

- ``orb_state_for`` / ``OrbState``: maps the daemon's ``JarvisState`` onto the
  visual states (plus ``MUTED`` and ``ERROR``, which are set by override).
  ``state_colours`` and ``synthetic_speech_level`` are shared with the wake
  screen effect (``wake_overlay``) so both read as one look.
- ``build_sphere``: the fixed, evenly spread set of 3D points (no Qt).
- ``OrbModel``: pure animation maths (no Qt). Smooths glow, rotation, the
  voice level, the thinking glow and the wake flash towards per-state targets,
  and projects the sphere for each frame.
- ``OrbWidget``: a ``QPainter`` renderer that owns the timer, reads state and
  audio level, and draws the model as additive light over a dark backdrop.

Audio amplitude arrives through ``AudioLevelSource`` (push a 0..1 level from
any thread). When no fresh level exists, listening stays calm and speaking
uses a synthetic speech-like envelope.
"""

from __future__ import annotations

import math
import threading
import time as _time
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple, Optional, Tuple

import numpy as np

from PyQt6 import sip
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from desktop_app.themes import ORB_PALETTE
from jarvis.debug import debug_log


class OrbState(Enum):
    """Visual states of the orb."""
    OFFLINE = "offline"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DICTATING = "dictating"
    MUTED = "muted"
    ERROR = "error"


_JARVIS_TO_ORB = {
    "asleep": OrbState.OFFLINE,
    "idle": OrbState.IDLE,
    "listening": OrbState.LISTENING,
    "thinking": OrbState.THINKING,
    "speaking": OrbState.SPEAKING,
    "dictating": OrbState.DICTATING,
    "dictation_processing": OrbState.THINKING,
}

# States in which the orb is at rest; moving from one of these into any other
# state is a wake and fires the flash.
RESTING_STATES = frozenset({OrbState.OFFLINE, OrbState.IDLE, OrbState.MUTED, OrbState.ERROR})


def orb_state_for(jarvis_state_value: str) -> OrbState:
    """Map a ``JarvisState`` value to an orb state (unknown values read as offline)."""
    return _JARVIS_TO_ORB.get(jarvis_state_value, OrbState.OFFLINE)


@dataclass(frozen=True)
class _Look:
    """Per-state animation targets and colours."""
    glow: float          # overall brightness 0..1
    spin: float          # rotation, radians per second
    think: float         # inner glow and sweeping band of light 0..1
    voice_gain: float    # how strongly the surface follows the level 0..1
    stir: float          # how much the surface moves with no sound
    colour: str
    accent: str
    label: str
    swell: float = 1.0   # how strongly the voice reshapes the surface and speeds its flow


_LOOKS = {
    OrbState.OFFLINE: _Look(0.08, 0.05, 0.0, 0.0, 0.01, ORB_PALETTE["slate_dark"], ORB_PALETTE["slate"], "offline"),
    OrbState.IDLE: _Look(0.30, 0.25, 0.0, 0.0, 0.025, ORB_PALETTE["cyan"], ORB_PALETTE["sky"], "system online"),
    OrbState.LISTENING: _Look(0.70, 0.40, 0.0, 1.0, 0.045, ORB_PALETTE["cyan"], ORB_PALETTE["blue_light"], "listening"),
    OrbState.THINKING: _Look(0.60, 0.90, 1.0, 0.0, 0.03, ORB_PALETTE["indigo"], ORB_PALETTE["sky"], "thinking"),
    OrbState.SPEAKING: _Look(0.92, 0.45, 0.0, 1.0, 0.03, ORB_PALETTE["cyan_light"], ORB_PALETTE["blue"], "speaking",
                               swell=1.45),
    OrbState.DICTATING: _Look(0.75, 0.35, 0.0, 1.0, 0.045, ORB_PALETTE["green_light"], ORB_PALETTE["green"], "dictating"),
    OrbState.MUTED: _Look(0.14, 0.10, 0.0, 0.0, 0.01, ORB_PALETTE["slate_mid"], ORB_PALETTE["slate_light"], "muted"),
    OrbState.ERROR: _Look(0.45, 0.15, 0.0, 0.0, 0.02, ORB_PALETTE["red"], ORB_PALETTE["red_light"], "error"),
}

_MAX_DT = 0.1
_FLASH_DECAY = 2.2   # per second; the wake flash is gone within about two seconds


def state_colours(state: OrbState) -> Tuple[str, str]:
    """The ``(colour, accent)`` palette entries the orb paints ``state`` in."""
    look = _LOOKS[state]
    return look.colour, look.accent


def state_label(state: OrbState) -> str:
    """The footer label of ``state``."""
    return _LOOKS[state].label


def synthetic_speech_level(t: float) -> float:
    """Speech-like 0..1 envelope at time ``t`` for when no real amplitude is available."""
    syllables = 0.5 + 0.5 * math.sin(t * 9.0) * math.sin(t * 2.3 + 1.0)
    return 0.25 + 0.55 * max(0.0, syllables)


class AudioLevelSource:
    """Thread-safe holder of the most recent audio level (0..1).

    Producers call ``push`` from any thread (mic frames, TTS playback
    callback); the orb calls ``current``. Levels older than ``max_age_s``
    count as absent so a stalled producer never freezes the orb.
    """

    def __init__(self, max_age_s: float = 0.3):
        self._max_age_s = max_age_s
        self._lock = threading.Lock()
        self._level = 0.0
        self._stamp: Optional[float] = None

    def push(self, level: float, now: Optional[float] = None) -> None:
        stamp = _time.monotonic() if now is None else now
        with self._lock:
            self._level = min(1.0, max(0.0, float(level)))
            self._stamp = stamp

    def current(self, now: Optional[float] = None) -> Optional[float]:
        now = _time.monotonic() if now is None else now
        with self._lock:
            if self._stamp is None or now - self._stamp > self._max_age_s:
                return None
            return self._level


_audio_source: Optional[AudioLevelSource] = None
_audio_source_lock = threading.Lock()


def get_audio_level_source() -> AudioLevelSource:
    """Process-wide level source the orb reads by default."""
    global _audio_source
    with _audio_source_lock:
        if _audio_source is None:
            _audio_source = AudioLevelSource()
        return _audio_source


def _approach(value: float, target: float, rate: float, dt: float) -> float:
    """Exponential approach, frame-rate independent."""
    return value + (target - value) * (1.0 - math.exp(-rate * dt))


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


SPHERE_POINTS = 5000
_INNER_SHARE = 0.2            # a fifth of the points form the dimmer inner sphere
_INNER_RADIUS = 0.55
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))
_VIEW_TILT = math.radians(20)   # the sphere is seen from a little above


class SpherePoints(NamedTuple):
    """The fixed point set, built once: the outer sphere (radius 1) then the inner one."""
    position: "np.ndarray"   # (N, 3)
    inner: "np.ndarray"      # 1 for points of the inner sphere


class SphereFrame(NamedTuple):
    """One projected frame: unit coordinates (centre 0, rest radius 1), depth -1 (back) .. 1 (front)."""
    x: "np.ndarray"
    y: "np.ndarray"
    depth: "np.ndarray"
    light: "np.ndarray"      # 0..1
    inner: "np.ndarray"


def _fibonacci_sphere(count: int, offset: float) -> "np.ndarray":
    """``count`` points spread evenly over the unit sphere (equal area per point)."""
    i = np.arange(count)
    y = 1.0 - 2.0 * (i + 0.5) / count
    r = np.sqrt(1.0 - y * y)
    a = i * _GOLDEN_ANGLE + offset
    return np.stack([np.cos(a) * r, y, np.sin(a) * r], axis=1)


def build_sphere(count: int = SPHERE_POINTS) -> SpherePoints:
    """The orb's points: an evenly spread outer sphere and a smaller inner one. Nothing is generated per frame."""
    inner = int(count * _INNER_SHARE)
    outer = count - inner
    position = np.concatenate([_fibonacci_sphere(outer, 0.0), _fibonacci_sphere(inner, 1.0) * _INNER_RADIUS])
    return SpherePoints(position, np.r_[np.zeros(outer, np.int8), np.ones(inner, np.int8)])


_SPHERE: Optional[SpherePoints] = None


def shared_sphere() -> SpherePoints:
    """The process-wide point set (built on first use)."""
    global _SPHERE
    if _SPHERE is None:
        _SPHERE = build_sphere()
        debug_log(f"orb sphere built: {len(_SPHERE.position)} points", "desktop")
    return _SPHERE


def _surface_field(direction: "np.ndarray", phase: "np.ndarray") -> "np.ndarray":
    """A smooth, slowly changing shape over the sphere, about -1..1: where the surface rises and falls."""
    x, y, z = direction[:, 0], direction[:, 1], direction[:, 2]
    return (np.sin(x * 2.6 + phase * 1.7) * np.sin(y * 2.9 - phase * 1.3) * np.sin(z * 2.4 + phase * 2.1)
            + 0.55 * np.sin(x * 5.1 - phase * 2.6 + y * 1.5) * np.sin(z * 5.6 + phase * 2.2)
            + 0.3 * np.sin(y * 9.0 - phase * 4.0 + z * 2.0) * np.sin(x * 8.5 + phase * 3.1))


def _view(spin: float) -> "np.ndarray":
    """Rotation by ``spin`` about the vertical axis, seen from a little above."""
    cy, sy = math.cos(spin), math.sin(spin)
    ct, st = math.cos(_VIEW_TILT), math.sin(_VIEW_TILT)
    turn = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    tilt = np.array([[1, 0, 0], [0, ct, -st], [0, st, ct]])
    return tilt @ turn


class OrbModel:
    """Qt-free animation state for the orb."""

    def __init__(self) -> None:
        self.glow = 0.0
        self.voice = 0.0                     # smoothed voice level 0..1: the surface swells and ripples with it
        self.think = 0.0                     # thinking glow 0..1: inner light and a sweeping band
        self.flash = 0.0                     # wake flash 0..1, decays after a wake
        self.shimmer = 1.0                   # hologram brightness flicker, close to 1
        self.spin = 0.0                      # rotation in radians, unbounded
        self.time = 0.0
        self._spin_speed = 0.0
        self._stir = 0.0
        self._swell = 1.0
        self._phase = 0.0                    # how far the surface shape has moved; faster with the voice
        self._previous_state: Optional[OrbState] = None
        self.sphere = shared_sphere()
        position = self.sphere.position
        self._direction = position / np.linalg.norm(position, axis=1, keepdims=True)
        self._inner = self.sphere.inner.astype(bool)
        # The inner sphere moves out of step with the outer one.
        self._phase_offset = np.where(self._inner, 2.0, 0.0)

    def step(self, dt: float, state: OrbState, level: Optional[float]) -> None:
        dt = min(max(dt, 0.0), _MAX_DT)
        look = _LOOKS[state]
        self.time += dt

        previous, self._previous_state = self._previous_state, state
        if previous in RESTING_STATES and state not in RESTING_STATES:
            self.flash = 1.0
        self.flash = _clamp01(self.flash * math.exp(-_FLASH_DECAY * dt))

        t = self.time
        flicker = 0.5 + 0.5 * math.sin(t * 37.0) * math.sin(t * 11.3 + 0.7)
        self.shimmer = _clamp01(1.0 - 0.03 * flicker)

        self.glow = _clamp01(_approach(self.glow, look.glow, 4.0, dt))
        self.think = _clamp01(_approach(self.think, look.think, 5.0, dt))
        self._stir = _approach(self._stir, look.stir, 3.0, dt)
        self._swell = _approach(self._swell, look.swell, 3.0, dt)
        self._spin_speed = _approach(self._spin_speed, look.spin, 3.0, dt)
        self.spin += self._spin_speed * dt

        if level is None:
            level = synthetic_speech_level(t) if state is OrbState.SPEAKING else 0.0
        target = _clamp01(level) * look.voice_gain
        # Quick to rise, slower to fall, like a level meter.
        self.voice = _clamp01(_approach(self.voice, target, 18.0 if target > self.voice else 7.0, dt))
        self._phase += dt * (0.6 + 5.0 * self._swell * self.voice)

    def sphere_frame(self) -> SphereFrame:
        """Project the sphere for this frame: the surface shaped by the voice, turned, see-through."""
        t = self.time
        inner = self._inner
        shape = _surface_field(self._direction, self._phase + self._phase_offset)
        breath = (1.0 + 0.015 * math.sin(t * 1.3) - 0.03 * self.think * (0.5 + 0.5 * math.sin(t * 4.0))
                  + 0.08 * self.flash)
        amplitude = np.where(inner, 1.5, 1.0) * (self._stir + 0.22 * self._swell * self.voice)
        ripple = 0.05 * self.think * np.sin(self._direction[:, 1] * 10.0 - t * 6.0)
        scale = breath * (1.0 + amplitude * shape + ripple)
        turned = (self.sphere.position * scale[:, None]) @ _view(self.spin).T
        x, y, z = turned[:, 0], turned[:, 1], turned[:, 2]
        size = np.maximum(np.linalg.norm(turned, axis=1), 1e-6)
        depth = np.clip(z / size, -1.0, 1.0)
        front = (depth + 1.0) / 2.0
        limb = np.hypot(x, y) / size          # 0 facing the viewer, 1 at the outline
        # Front brighter than back, the outline brightest, and the raised parts of the surface catch more light.
        light = ((0.1 + 0.9 * front ** 1.7) * (0.4 + 0.6 * limb ** 4)
                 * (0.75 + 0.6 * np.maximum(shape, 0.0) * (0.3 + self.voice)))
        # The inner sphere is dim, and lights up from within while thinking.
        light = np.where(inner, light * (0.35 + 0.5 * self.think), light)
        # Thinking: a band of light sweeps over the outer sphere, top to bottom.
        band = (t * 0.65) % 1.0 * 2.4 - 1.2
        sweep = self.think * 0.9 * np.exp(-((self._direction[:, 1] - band) ** 2) / 0.012) * (0.3 + 0.7 * front)
        light = np.clip(light + np.where(inner, 0.0, sweep), 0.0, 1.0)
        return SphereFrame(x, -y, depth, light, self.sphere.inner)


def _with_alpha(colour, alpha: float) -> QColor:
    c = QColor(colour)
    c.setAlphaF(_clamp01(alpha))
    return c


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def _qt_points(rows: "np.ndarray") -> "sip.array":
    """``rows`` of (x, y) as a ``sip.array`` of ``QPointF`` that ``QPainter`` reads in place."""
    array = sip.array(QPointF, len(rows))
    if len(rows):
        np.frombuffer(sip.voidptr(array, rows.size * 8), dtype=np.float64).reshape(rows.shape)[:] = rows
    return array


class OrbWidget(QWidget):
    """Painter-based particle orb. Animates only while visible."""

    clicked = pyqtSignal()

    BG_COLOR = QColor(ORB_PALETTE["backdrop"])
    # Windows rounds coarse timers up to 15.6 ms steps: 30 ms runs at about 32 FPS, 62 ms at 16.
    ACTIVE_INTERVAL_MS = 30
    RESTING_INTERVAL_MS = 62
    STATE_POLL_S = 0.1
    COLOUR_RATE = 6.0
    _LEVELS = 7          # brightness steps the points are drawn in

    def __init__(self, parent=None, audio_source: Optional[AudioLevelSource] = None,
                 state_manager=None):
        super().__init__(parent)
        self.setMinimumSize(240, 260)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.model = OrbModel()
        self.orb_state = OrbState.OFFLINE
        self._override: Optional[OrbState] = None
        self._caption: Optional[str] = None
        self._audio = audio_source if audio_source is not None else get_audio_level_source()
        self._state_manager = state_manager
        self._since_poll = self.STATE_POLL_S
        self._colour = QColor(_LOOKS[OrbState.OFFLINE].colour)
        self._accent = QColor(_LOOKS[OrbState.OFFLINE].accent)
        self._last_tick = _time.monotonic()

        self._timer = QTimer(self)
        self._timer.setInterval(self.RESTING_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer)

    def mouseReleaseEvent(self, event) -> None:
        """A left click that ends over the orb emits ``clicked``."""
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    # -- public API ---------------------------------------------------------

    def set_state_override(self, state: Optional[OrbState]) -> None:
        """Force a visual state (e.g. MUTED, ERROR); ``None`` follows the daemon."""
        self._override = state

    def set_caption(self, caption: Optional[str]) -> None:
        """Show ``caption`` in the footer in place of the state label; ``None`` restores the label."""
        self._caption = caption
        self.update()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def frame_interval_ms(self) -> int:
        """Current frame interval: slower while the orb rests, smooth while it is active."""
        return self._timer.interval()

    def tick(self, dt: float) -> None:
        """Advance the animation by ``dt`` seconds and schedule a repaint."""
        previous = self.orb_state
        self._refresh_state(dt)
        if self.orb_state is not previous:
            debug_log(f"orb state: {previous.value} -> {self.orb_state.value}", "desktop")
        self.model.step(dt, self.orb_state, self._audio.current())
        resting = self.orb_state in RESTING_STATES and self.model.flash < 0.01
        interval = self.RESTING_INTERVAL_MS if resting else self.ACTIVE_INTERVAL_MS
        if interval != self._timer.interval():
            self._timer.setInterval(interval)
        look = _LOOKS[self.orb_state]
        mix = 1.0 - math.exp(-self.COLOUR_RATE * min(dt, 0.1))
        self._colour = _blend(self._colour, QColor(look.colour), mix)
        self._accent = _blend(self._accent, QColor(look.accent), mix)
        self.update()

    # -- lifecycle ----------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self._last_tick = _time.monotonic()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _on_timer(self) -> None:
        now = _time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        self.tick(dt)

    def _refresh_state(self, dt: float) -> None:
        if self._override is not None:
            self.orb_state = self._override
            return
        self._since_poll += dt
        if self._since_poll < self.STATE_POLL_S:
            return
        self._since_poll = 0.0
        try:
            manager = self._state_manager
            if manager is None:
                from desktop_app.face_widget import get_jarvis_state
                manager = self._state_manager = get_jarvis_state()
            self.orb_state = orb_state_for(manager.state.value)
        except Exception:
            self.orb_state = OrbState.OFFLINE

    # -- painting -----------------------------------------------------------

    def _energy(self) -> float:
        """Overall light level: the glow, lifted by the wake flash, with the hologram shimmer."""
        m = self.model
        return _clamp01(m.glow + 0.55 * m.flash) * m.shimmer

    def paintEvent(self, event):
        painter = QPainter(self)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, self.BG_COLOR)
        if w < 8 or h < 8:
            painter.end()
            return

        footer = min(40.0, h * 0.14)
        header = min(34.0, h * 0.12)
        cx = w / 2
        cy = header + (h - header - footer) / 2
        # The rest radius leaves room for the surface to swell with the voice.
        radius = max(10.0, min(w, h - header - footer) / 2 * 0.72)
        energy = self._energy()

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_atmosphere(painter, cx, cy, radius, energy)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        self._draw_sphere(painter, cx, cy, radius, energy)
        self._draw_flash(painter, cx, cy, radius)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_text(painter, w, h, header, footer, energy)
        painter.end()

    def _draw_atmosphere(self, p: QPainter, cx, cy, r, energy) -> None:
        """Soft light round the sphere with a glowing rim; both swell with the voice."""
        voice = self.model.voice
        reach = r * (1.0 + 0.1 * voice) * 1.75
        g = QRadialGradient(QPointF(cx, cy), reach)
        g.setColorAt(0.0, _with_alpha(self._colour, (0.04 + 0.05 * voice) * energy))
        g.setColorAt(0.45, _with_alpha(self._colour, (0.07 + 0.07 * voice) * energy))
        g.setColorAt(0.57, _with_alpha(self._colour, (0.2 + 0.18 * voice) * energy))
        g.setColorAt(0.75, _with_alpha(self._colour, (0.05 + 0.06 * voice) * energy))
        g.setColorAt(1.0, _with_alpha(self._colour, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), reach, reach)

    def _draw_sphere(self, p: QPainter, cx, cy, r, energy) -> None:
        """Every point as a dot of light, batched by sphere and brightness step; the brightest are whiter."""
        f = self.model.sphere_frame()
        light = np.clip(f.light * (0.3 + 0.7 * energy), 0.0, 1.0)
        # Steps on a square-root scale, so dim points still show and the bright ones stay distinct.
        level = np.minimum((np.sqrt(light) * self._LEVELS).astype(np.int16), self._LEVELS - 1)
        keys = f.inner.astype(np.int16) * self._LEVELS + level
        order = np.argsort(keys, kind="stable")
        bounds = np.searchsorted(keys[order], np.arange(2 * self._LEVELS + 1)).tolist()
        # One array for the whole frame, in batch order, that Qt reads in place.
        points = _qt_points(np.stack([f.x[order], f.y[order]], axis=1) * r + (cx, cy))
        white = QColor(ORB_PALETTE["white"])
        scale = max(0.6, r / 150.0)
        for key in range(2 * self._LEVELS):
            start, stop = bounds[key], bounds[key + 1]
            lv = key % self._LEVELS
            if lv == 0 or start == stop:
                continue
            base = self._accent if key >= self._LEVELS else self._colour
            whiten = max(0.0, (lv - 3) / (self._LEVELS - 4)) * 0.75
            pen = QPen(_with_alpha(_blend(base, white, whiten), min(1.0, ((lv + 0.6) / self._LEVELS) ** 2 * 1.15)),
                       scale * (1.0 + 0.2 * lv))
            # Dim dots are small squares drawn without antialiasing (cheap); bright ones are round.
            bright = lv >= 5
            pen.setCapStyle(Qt.PenCapStyle.RoundCap if bright else Qt.PenCapStyle.SquareCap)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, bright)
            p.setPen(pen)
            p.drawPoints(points[start:stop])

    def _draw_flash(self, p: QPainter, cx, cy, r) -> None:
        """Wake flash: a soft ring of light spreading out from the sphere's centre."""
        f = self.model.flash
        if f < 0.01:
            return
        ring_r = r * (0.15 + 1.0 * (1.0 - f))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        bright = _blend(self._colour, QColor(ORB_PALETTE["white"]), 0.6)
        for tone, alpha, width in ((self._colour, 0.3, max(6.0, r * 0.1)), (bright, 0.8, 1.5 + 2.0 * f)):
            p.setPen(QPen(_with_alpha(tone, alpha * f), width))
            p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

    def _draw_text(self, p: QPainter, w, h, header, footer, energy) -> None:
        dim = _with_alpha(self._accent, 0.55 + 0.4 * energy)

        title = QFont(self.font())
        title.setPixelSize(max(10, int(header * 0.40)))
        title.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
        title.setWeight(QFont.Weight.DemiBold)
        p.setFont(title)
        p.setPen(dim)
        p.drawText(QRectF(0, 0, w, header * 1.2), Qt.AlignmentFlag.AlignCenter, "J.A.R.V.I.S.")

        small = QFont(self.font())
        small.setPixelSize(max(9, int(footer * 0.3)))
        small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(small)
        label = (self._caption or state_label(self.orb_state)).upper()
        rect = QRectF(0, h - footer * 1.1, w, footer)
        p.setPen(_with_alpha(self._colour, 0.85))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # Flanking lines with end markers, the HUD readout look.
        text_w = QFontMetricsF(small).horizontalAdvance(label)
        mid_y = rect.center().y()
        gap, length = 12.0, min(48.0, w * 0.12)
        p.setPen(QPen(_with_alpha(self._colour, 0.25 + 0.4 * energy), 1.0))
        for direction in (-1, 1):
            near = w / 2 + direction * (text_w / 2 + gap)
            far = near + direction * length
            if 0 < far < w:
                p.drawLine(QPointF(near, mid_y), QPointF(far, mid_y))
                p.drawLine(QPointF(far, mid_y - 3), QPointF(far, mid_y + 3))

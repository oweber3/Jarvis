"""
Reactive orb for the Jarvis face window: a see-through holographic data sphere.

Split in layers so each can be improved independently:

- ``orb_state_for`` / ``OrbState``: maps the daemon's ``JarvisState`` onto the
  visual states (plus ``MUTED`` and ``ERROR``, which are set by override).
  ``state_colours`` and ``synthetic_speech_level`` are shared with the wake
  screen effect (``wake_overlay``) so both read as one look.
- ``build_fragments``: the fixed, seeded set of 3D shell fragments (no Qt).
- ``OrbModel``: pure animation maths (no Qt). Smooths glow, shell rotation,
  audio-reactive sectors and the wake flash towards per-state targets, and
  projects the shell for each frame.
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
from typing import List, NamedTuple, Optional, Tuple

import numpy as np

from PyQt6 import sip
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, pyqtSignal
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
    glow: float          # overall shell brightness 0..1
    ring_speed: float    # outer ring rotation, degrees per second
    scan_speed: float    # scanning sweep rotation, degrees per second
    scan_strength: float  # scanning sweep opacity 0..1
    bar_gain: float      # how strongly bars follow the level 0..1
    ripples: bool        # emit expanding listening ripples
    colour: str
    accent: str
    label: str


_LOOKS = {
    OrbState.OFFLINE: _Look(0.08, 2, 0, 0.0, 0.0, False, ORB_PALETTE["slate_dark"], ORB_PALETTE["slate"], "offline"),
    OrbState.IDLE: _Look(0.28, 8, 14, 0.10, 0.0, False, ORB_PALETTE["cyan"], ORB_PALETTE["sky"], "system online"),
    OrbState.LISTENING: _Look(0.70, 16, 20, 0.15, 1.0, True, ORB_PALETTE["cyan"], ORB_PALETTE["blue_light"], "listening"),
    OrbState.THINKING: _Look(0.55, 30, 220, 1.0, 0.0, False, ORB_PALETTE["indigo"], ORB_PALETTE["sky"], "thinking"),
    OrbState.SPEAKING: _Look(0.92, 20, 26, 0.20, 1.0, False, ORB_PALETTE["cyan_light"], ORB_PALETTE["blue"], "speaking"),
    OrbState.DICTATING: _Look(0.75, 14, 18, 0.15, 1.0, True, ORB_PALETTE["green_light"], ORB_PALETTE["green"], "dictating"),
    OrbState.MUTED: _Look(0.14, 4, 0, 0.0, 0.0, False, ORB_PALETTE["slate_mid"], ORB_PALETTE["slate_light"], "muted"),
    OrbState.ERROR: _Look(0.45, 6, 0, 0.0, 0.0, False, ORB_PALETTE["red"], ORB_PALETTE["red_light"], "error"),
}

NUM_BARS = 56
_RIPPLE_PERIOD_S = 1.6
_RIPPLE_LIFE_S = 2.2
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
    count as absent so a stalled producer never freezes the bars.
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


FRAG_ARC, FRAG_BLOCK, FRAG_SPARK, FRAG_STREAK, FRAG_RING = range(5)
_LAYER_SPEEDS = (1.0, -0.65, 1.45)    # each shell layer turns at its own speed and direction
_SHELL_TILT = math.radians(-18)       # the turning axis leans towards the viewer


class Fragments(NamedTuple):
    """The fixed set of 3D shell fragments, built once: endpoints on (or near) a unit sphere."""
    start: "np.ndarray"     # (N, 3)
    mid: "np.ndarray"       # (N, 3) halfway along the curve, so filaments bend with the sphere
    end: "np.ndarray"       # (N, 3)
    kind: "np.ndarray"      # FRAG_* per fragment
    layer: "np.ndarray"     # index into _LAYER_SPEEDS
    light: "np.ndarray"     # base brightness 0..1
    tone: "np.ndarray"      # 0 = state colour, 1 = accent
    twinkle: "np.ndarray"   # spark twinkle phase
    radius: "np.ndarray"    # distance from the centre, so each layer glows at its own rim


class ShellFrame(NamedTuple):
    """One frame of the shell, projected: unit coordinates (centre 0, rim 1), depth -1 (back) .. 1 (front)."""
    x1: "np.ndarray"
    y1: "np.ndarray"
    xm: "np.ndarray"
    ym: "np.ndarray"
    x2: "np.ndarray"
    y2: "np.ndarray"
    depth: "np.ndarray"
    light: "np.ndarray"
    kind: "np.ndarray"
    tone: "np.ndarray"


def _on_sphere(lat, lon, radius):
    return np.stack([radius * np.cos(lat) * np.sin(lon), radius * np.sin(lat),
                     radius * np.cos(lat) * np.cos(lon)], axis=-1)


def build_fragments(seed: int = 7) -> Fragments:
    """The shell's fragments: filament arcs, circuit blocks, sparks, flow streaks and broken inner rings.

    Seeded, so the orb looks the same on every start and nothing is generated per frame.
    """
    rng = np.random.default_rng(seed)
    parts = []

    def scatter(count, kind, length, radius, light):
        lat = np.arcsin(rng.uniform(-0.97, 0.97, count))
        lon = rng.uniform(0, math.tau, count)
        r = rng.uniform(*radius, count)
        span = rng.uniform(*length, count)
        along_lat = rng.random(count) < 0.8           # most filaments follow the latitude lines
        dlat = np.where(along_lat, 0.0, span)
        dlon = np.where(along_lat, span / np.maximum(np.cos(lat), 0.25), 0.0)
        lat2 = np.clip(lat + dlat, -1.5, 1.5)
        latm = np.clip(lat + dlat / 2, -1.5, 1.5)
        parts.append((_on_sphere(lat, lon, r), _on_sphere(latm, lon + dlon / 2, r), _on_sphere(lat2, lon + dlon, r),
                      np.full(count, kind), rng.uniform(*light, count)))

    # The outer shell: the densest layer of filaments, blocks and sparks.
    scatter(760, FRAG_ARC, (0.06, 0.32), (0.9, 1.0), (0.35, 1.0))
    scatter(260, FRAG_BLOCK, (0.015, 0.04), (0.86, 1.0), (0.4, 1.0))
    scatter(300, FRAG_SPARK, (0.0, 0.0), (0.86, 1.08), (0.5, 1.0))
    scatter(60, FRAG_ARC, (0.03, 0.12), (1.0, 1.14), (0.2, 0.6))        # ragged fragments past the rim
    scatter(90, FRAG_STREAK, (0.4, 0.9), (0.9, 1.06), (0.15, 0.4))      # faint flow trails
    # Nested inner layers fill the sphere, each a little sparser and dimmer than the one outside it.
    for low, high, arcs, blocks, sparks, light in ((0.75, 0.9, 300, 80, 110, (0.3, 0.85)),
                                                   (0.5, 0.75, 260, 60, 100, (0.25, 0.75)),
                                                   (0.25, 0.5, 170, 40, 80, (0.2, 0.65))):
        scatter(arcs, FRAG_ARC, (0.08, 0.45), (low, high), light)
        scatter(blocks, FRAG_BLOCK, (0.02, 0.05), (low, high), light)
        scatter(sparks, FRAG_SPARK, (0.0, 0.0), (low, high), light)

    # Broken inner rings at different tilts, a third of each missing.
    for tilt, radius in ((0.35, 0.82), (-0.9, 0.7), (1.25, 0.62), (0.1, 0.55), (-0.45, 0.46),
                         (0.8, 0.38), (-1.3, 0.3), (1.6, 0.77)):
        count = max(20, int(48 * radius))
        angles = np.linspace(0, math.tau, count, endpoint=False)
        keep = rng.random(count) > 0.33
        a1, a2 = angles[keep], angles[keep] + math.tau / count * 0.85
        def ring_point(a):
            x, y = radius * np.cos(a), radius * np.sin(a)
            return np.stack([x, y * math.cos(tilt), y * math.sin(tilt)], axis=-1)
        parts.append((ring_point(a1), ring_point((a1 + a2) / 2), ring_point(a2), np.full(keep.sum(), FRAG_RING),
                      rng.uniform(0.55, 1.0, keep.sum())))

    start = np.concatenate([p[0] for p in parts])
    mid = np.concatenate([p[1] for p in parts])
    end = np.concatenate([p[2] for p in parts])
    kind = np.concatenate([p[3] for p in parts]).astype(np.int8)
    light = np.concatenate([p[4] for p in parts])
    n = len(kind)
    layer = rng.integers(0, len(_LAYER_SPEEDS), n)
    layer[kind == FRAG_RING] = rng.integers(0, len(_LAYER_SPEEDS), (kind == FRAG_RING).sum())
    fragments = Fragments(start, mid, end, kind, layer.astype(np.int8), light, (rng.random(n) < 0.35).astype(np.int8),
                          rng.uniform(0, math.tau, n), np.linalg.norm(mid, axis=1))
    # Grouped by layer, so each layer turns as one contiguous block.
    order = np.argsort(fragments.layer, kind="stable")
    return Fragments(*(field[order] for field in fragments))


_FRAGMENTS: Optional[Fragments] = None


def shared_fragments() -> Fragments:
    """The process-wide fragment set (built on first use)."""
    global _FRAGMENTS
    if _FRAGMENTS is None:
        _FRAGMENTS = build_fragments()
        debug_log(f"orb shell built: {len(_FRAGMENTS.kind)} fragments", "desktop")
    return _FRAGMENTS


def _turn(yaw: float) -> "np.ndarray":
    """Rotation about the leaning vertical axis by ``yaw`` radians."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    ct, st = math.cos(_SHELL_TILT), math.sin(_SHELL_TILT)
    spin = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    lean = np.array([[1, 0, 0], [0, ct, -st], [0, st, ct]])
    return lean @ spin


class OrbModel:
    """Qt-free animation state for the orb."""

    def __init__(self) -> None:
        self.glow = 0.0
        self.ring_angle = 0.0
        self.scan_angle = 0.0
        self.scan_strength = 0.0
        self.pulse = 0.0                     # output-level pulse 0..1 (the haze swells with it)
        self.flash = 0.0                     # wake flash 0..1, decays after a wake
        self.shimmer = 1.0                   # hologram brightness flicker, close to 1
        self.bars: List[float] = [0.0] * NUM_BARS
        self.ripples: List[float] = []       # ages in seconds
        self.time = 0.0
        self._ripple_clock = 0.0
        self._ring_speed = 0.0
        self._scan_speed = 0.0
        self._speech_level = 0.0
        self._previous_state: Optional[OrbState] = None
        self._spin = 0.0                     # shell rotation in radians, unbounded so layers never jump
        self.fragments = shared_fragments()
        f = self.fragments
        self._points = np.stack([f.start, f.mid, f.end])      # (3, N, 3): every point turns in one product per layer
        bounds = np.searchsorted(f.layer, np.arange(len(_LAYER_SPEEDS) + 1))
        self._layer_spans = list(zip(bounds[:-1].tolist(), bounds[1:].tolist()))

    def _synthetic_level(self, state: OrbState) -> float:
        """Speech-like envelope used when no real amplitude is available."""
        if state is not OrbState.SPEAKING:
            return 0.0
        return synthetic_speech_level(self.time)

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
        glitch = 0.12 if math.sin(t * 0.9) * math.sin(t * 2.7 + 0.4) > 0.94 else 0.0
        self.shimmer = _clamp01(1.0 - 0.035 * flicker - glitch)

        self.glow = _clamp01(_approach(self.glow, look.glow, 4.0, dt))
        self._ring_speed = _approach(self._ring_speed, look.ring_speed, 3.0, dt)
        self._scan_speed = _approach(self._scan_speed, look.scan_speed, 3.0, dt)
        self.ring_angle = (self.ring_angle + self._ring_speed * dt) % 360.0
        self._spin += math.radians(self._ring_speed * dt)
        self.scan_angle = self.scan_angle + self._scan_speed * dt
        self.scan_strength = _approach(self.scan_strength, look.scan_strength, 5.0, dt)

        if level is None:
            level = self._synthetic_level(state)
        level = _clamp01(level) * look.bar_gain
        self._speech_level = _approach(self._speech_level, level, 14.0, dt)
        self.pulse = _approach(self.pulse, self._speech_level, 10.0, dt)

        for i in range(NUM_BARS):
            phase = i / NUM_BARS * math.tau
            shape = (
                0.55
                + 0.25 * math.sin(phase * 3 + self.time * 5.0)
                + 0.20 * math.sin(phase * 7 - self.time * 8.0)
            )
            target = _clamp01(self._speech_level * shape)
            rate = 22.0 if target > self.bars[i] else 7.0
            self.bars[i] = _clamp01(_approach(self.bars[i], target, rate, dt))

        self._step_ripples(dt, look.ripples)

    def shell_frame(self) -> ShellFrame:
        """Project the shell for this frame: turned layers, see-through depth, sectors swelling with the voice."""
        f = self.fragments
        turned = np.empty_like(self._points)
        for (low, high), speed in zip(self._layer_spans, _LAYER_SPEEDS):
            turned[:, low:high] = self._points[:, low:high] @ _turn(self._spin * speed).T
        start, mid, end = turned
        depth = np.clip(mid[:, 2], -1.0, 1.0)
        rho = np.hypot(mid[:, 0], mid[:, 1])
        sector = ((np.arctan2(mid[:, 1], mid[:, 0]) / math.tau) * NUM_BARS).astype(int) % NUM_BARS
        excite = np.asarray(self.bars)[sector]
        swell = 1.0 + 0.12 * excite
        front = (depth + 1.0) / 2.0
        # Front brighter than back (see-through depth). Each layer is brightest at its own rim, so
        # the nested layers read as shells inside shells, and the outer rim stays the brightest.
        limb = np.minimum(rho / np.maximum(f.radius, 0.2), 1.0)
        outer = np.minimum(f.radius, 1.0)
        light = (f.light * (0.18 + 0.82 * front ** 1.5) * (0.3 + 0.7 * limb ** 2.5)
                 * (0.45 + 0.55 * outer ** 2))
        # While thinking the inner layers light up from within.
        light *= 1.0 + 1.4 * self.scan_strength * (1.0 - outer)
        sparks = f.kind == FRAG_SPARK
        light[sparks] *= 0.55 + 0.45 * np.sin(self.time * 3.0 + f.twinkle[sparks])
        light = np.clip(light * (1.0 + 0.8 * excite), 0.0, 1.0)
        return ShellFrame(start[:, 0] * swell, -start[:, 1] * swell, mid[:, 0] * swell, -mid[:, 1] * swell,
                          end[:, 0] * swell, -end[:, 1] * swell, depth, light, f.kind, f.tone)

    def _step_ripples(self, dt: float, emitting: bool) -> None:
        self.ripples = [age + dt for age in self.ripples if age + dt < _RIPPLE_LIFE_S]
        if emitting:
            if not self.ripples and self._ripple_clock == 0.0:
                self.ripples.append(0.0)
            self._ripple_clock += dt
            if self._ripple_clock >= _RIPPLE_PERIOD_S:
                self._ripple_clock -= _RIPPLE_PERIOD_S
                self.ripples.append(0.0)
        else:
            self._ripple_clock = 0.0


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


def _qt_array(kind, rows: "np.ndarray") -> "sip.array":
    """``rows`` of coordinates as a ``sip.array`` of ``QLineF`` or ``QPointF`` that ``QPainter`` reads in place."""
    array = sip.array(kind, len(rows))
    if len(rows):
        np.frombuffer(sip.voidptr(array, rows.size * 8), dtype=np.float64).reshape(rows.shape)[:] = rows
    return array


def _polar(cx: float, cy: float, radius: float, degrees: float) -> QPointF:
    """Point at ``degrees`` (clockwise from 3 o'clock on screen) and ``radius`` from the centre."""
    a = math.radians(degrees)
    return QPointF(cx + math.cos(a) * radius, cy + math.sin(a) * radius)


class OrbWidget(QWidget):
    """Painter-based holographic orb. Animates only while visible."""

    clicked = pyqtSignal()

    BG_COLOR = QColor(ORB_PALETTE["backdrop"])
    # Windows rounds coarse timers up to 15.6 ms steps: 30 ms runs at about 32 FPS, 62 ms at 16.
    ACTIVE_INTERVAL_MS = 30
    RESTING_INTERVAL_MS = 62
    STATE_POLL_S = 0.1
    COLOUR_RATE = 6.0

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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, self.BG_COLOR)
        if w < 8 or h < 8:
            painter.end()
            return

        footer = min(40.0, h * 0.14)
        header = min(34.0, h * 0.12)
        cx = w / 2
        cy = header + (h - header - footer) / 2
        radius = max(10.0, min(w, h - header - footer) / 2 * 0.86)
        energy = self._energy()

        self._draw_atmosphere(painter, cx, cy, radius, energy)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        self._draw_ripples(painter, cx, cy, radius)
        self._draw_shell(painter, cx, cy, radius, energy)
        self._draw_flash(painter, cx, cy, radius)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._draw_text(painter, w, h, header, footer, energy)
        painter.end()

    def _draw_atmosphere(self, p: QPainter, cx, cy, r, energy) -> None:
        """Soft projected light round the sphere, strongest at its rim; it swells a little with the output level."""
        strength = 0.06 + 0.14 * energy + 0.06 * self.model.pulse
        g = QRadialGradient(QPointF(cx, cy), r * 1.5)
        g.setColorAt(0.0, _with_alpha(self._colour, strength * 0.35))
        g.setColorAt(0.62, _with_alpha(self._colour, strength))
        g.setColorAt(1.0, _with_alpha(self._colour, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r * 1.5, r * 1.5)

    def _draw_ripples(self, p: QPainter, cx, cy, r) -> None:
        """Listening: broken rings of light spreading out through the shell."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        for age in self.model.ripples:
            t = age / _RIPPLE_LIFE_S
            rr = r * (0.3 + 0.85 * t)
            fade = (1.0 - t) ** 1.5
            pen = QPen(_with_alpha(self._colour, 0.45 * fade), 1.2)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([6.0, 3.0, 1.0, 4.0])
            p.setPen(pen)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

    # Pen width (px at a 170 px radius) and cap of each fragment kind.
    _KIND_PENS = {
        FRAG_ARC: (1.1, Qt.PenCapStyle.FlatCap),     # flat: the two halves of a curve meet without a bright bead
        FRAG_BLOCK: (2.6, Qt.PenCapStyle.SquareCap),
        FRAG_STREAK: (0.8, Qt.PenCapStyle.FlatCap),
        FRAG_RING: (1.3, Qt.PenCapStyle.FlatCap),
        FRAG_SPARK: (2.4, Qt.PenCapStyle.RoundCap),
    }
    _LEVELS = 6

    def _draw_shell(self, p: QPainter, cx, cy, r, energy) -> None:
        """The data sphere: every fragment as a stroke of light, batched by kind, tone and brightness."""
        f = self.model.shell_frame()
        light = np.clip(f.light * (0.3 + 0.7 * energy), 0.0, 1.0)
        # Steps on a square-root scale: dim fragments still get a faint step of their own, so the
        # sphere reads as full even at rest, while the bright steps stay distinct.
        level = np.minimum((np.sqrt(light) * self._LEVELS).astype(np.int16), self._LEVELS - 1)
        # Long curves bend through their midpoint; short ones are near enough straight for one stroke.
        bent = (np.hypot(f.x2 - f.x1, f.y2 - f.y1) > 0.2) & (f.kind != FRAG_BLOCK)
        visible = np.nonzero(level > 0)[0]
        keys = (f.kind[visible].astype(np.int16) * 100 + f.tone[visible] * 10 + level[visible])
        order = np.argsort(keys, kind="stable")
        visible, keys = visible[order], keys[order]
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        # Every stroke of the frame goes into one array Qt reads in place, in batch order: sparks as
        # points, other fragments as one segment or, when bent, two through the midpoint.
        spark = f.kind[visible] == FRAG_SPARK
        segments = np.where(spark, 0, 1 + bent[visible])
        frag = np.repeat(visible, segments)
        second = np.r_[False, frag[1:] == frag[:-1]]
        to_mid = bent[frag] & ~second
        lines = np.stack([np.where(second, f.xm[frag], f.x1[frag]), np.where(second, f.ym[frag], f.y1[frag]),
                          np.where(to_mid, f.xm[frag], f.x2[frag]), np.where(to_mid, f.ym[frag], f.y2[frag])], axis=1)
        line_array = _qt_array(QLineF, lines * r + (cx, cy, cx, cy))
        points = np.stack([f.x1[visible[spark]], f.y1[visible[spark]]], axis=1)
        point_array = _qt_array(QPointF, points * r + (cx, cy))
        line_at = np.r_[0, np.cumsum(segments)].tolist()
        point_at = np.r_[0, np.cumsum(spark)].tolist()
        white = QColor(ORB_PALETTE["white"])
        scale = max(0.6, r / 170.0)
        for start, stop in zip(starts.tolist(), np.r_[starts[1:], len(keys)].tolist()):
            key = int(keys[start])
            kind, tone, lv = key // 100, (key // 10) % 10, key % 10
            base = self._accent if tone else self._colour
            alpha = ((lv + 0.5) / self._LEVELS) ** 2
            tint = _blend(base, white, 0.55 * (lv - 2) / (self._LEVELS - 3)) if lv > 2 else base
            width, cap = self._KIND_PENS[kind]
            if kind == FRAG_SPARK:
                points = point_array[point_at[start]:point_at[stop]]
                if lv >= 3:   # bright sparks get a soft halo
                    halo = QPen(_with_alpha(tint, alpha * 0.25), width * scale * 3.2)
                    halo.setCapStyle(cap)
                    p.setPen(halo)
                    p.drawPoints(points)
                pen = QPen(_with_alpha(tint, alpha), width * scale)
                pen.setCapStyle(cap)
                p.setPen(pen)
                p.drawPoints(points)
                continue
            lines = line_array[line_at[start]:line_at[stop]]
            if lv >= 5:       # bloom: only the very brightest filaments glow beyond their stroke
                bloom = QPen(_with_alpha(base, alpha * 0.18), width * scale * 3.5)
                bloom.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(bloom)
                p.drawLines(lines)
            pen = QPen(_with_alpha(tint, alpha), width * scale)
            pen.setCapStyle(cap)
            p.setPen(pen)
            # The faintest steps are drawn without antialiasing: invisible at their alpha, and far cheaper.
            p.setRenderHint(QPainter.RenderHint.Antialiasing, lv > 1)
            p.drawLines(lines)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def _draw_flash(self, p: QPainter, cx, cy, r) -> None:
        """Wake flash: a broken ring of light thrown out through the shell from its centre."""
        f = self.model.flash
        if f < 0.01:
            return
        progress = 1.0 - f
        ring_r = r * (0.15 + 0.95 * progress)
        p.setBrush(Qt.BrushStyle.NoBrush)
        bright = _blend(self._colour, QColor(ORB_PALETTE["white"]), 0.6)
        # A broken ring of light fragments, like the shell, turning as it spreads.
        for tone, alpha, width, pattern in ((self._colour, 0.35, max(6.0, r * 0.08), [3.0, 0.8, 1.2, 0.6]),
                                            (bright, 0.9, 2.0 + 2.0 * f, [10.0, 3.0, 4.0, 2.0, 1.0, 3.0])):
            pen = QPen(_with_alpha(tone, alpha * f), width)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern(pattern)
            pen.setDashOffset(progress * 40.0)
            p.setPen(pen)
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

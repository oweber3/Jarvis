"""
Wake screen effect: the edges of every screen breathe with holographic light while Jarvis is awake.

Layers (see ``wake_overlay.spec.md``):

- ``OverlayModel``: Qt-free. Engages on a wake (idle to listening), follows the
  conversation, fades out when it ends, and animates the breathing glow, the
  wake heartbeat and the audio level.
- ``edge_strips`` / ``frame_thickness`` / ``screens_to_cover``: Qt-free geometry.
- ``frame_interval_ms`` / ``ToneFade``: Qt-free pacing of the frames and of colour changes.
- ``FramePainter``: ``QPainter`` renderer of one edge band of a screen's frame, in screen coordinates.
- ``WakeOverlay``: the Qt controller. Watches the state channel, builds the
  click-through edge windows on wake and removes them once the frame has faded.
"""

from __future__ import annotations

import math
import os
import sys
import time as _time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QFileSystemWatcher, QObject, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QConicalGradient,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
)
from PyQt6.QtWidgets import QWidget

from desktop_app.orb_widget import (  # the orb's colour and easing helpers keep both visuals one look
    AudioLevelSource,
    OrbState,
    _approach,
    _clamp01,
    _with_alpha,
    orb_state_for,
    shared_voice_level,
    state_colours,
    synthetic_speech_level,
)
from desktop_app.themes import ORB_PALETTE
from jarvis.debug import debug_log

Rect = Tuple[int, int, int, int]
Palette = Tuple[str, str]          # the (colour, accent) the glow's two tones are tinted in
Tones = Sequence[Tuple[Palette, float]]

MAX_UNCHANGED_S = 120.0   # safety net: fade out if the state has not moved for this long
_MAX_DT = 0.25            # the longest stretch one frame accounts for: a late frame catches up, a sleep does not
_ENGAGED_STATES = frozenset({OrbState.LISTENING, OrbState.THINKING, OrbState.SPEAKING})


class _Rhythm:
    """How a state breathes: seconds per breath, how deep, the glow it rests at, how sharp its crest."""

    def __init__(self, period: float, depth: float, base: float, level_gain: float, sharpness: float):
        self.period, self.depth, self.base = period, depth, base
        self.level_gain, self.sharpness = level_gain, sharpness


_RHYTHMS = {
    OrbState.LISTENING: _Rhythm(3.2, 0.34, 0.42, 1.0, 1.6),   # calm, slow breathing
    OrbState.THINKING: _Rhythm(1.05, 0.32, 0.38, 0.3, 2.4),   # a quick, crisp pulse
    OrbState.SPEAKING: _Rhythm(1.6, 0.16, 0.44, 1.0, 1.6),    # follows the voice, a light breath under it
}
# The wake is a heartbeat: two beats a quarter of a second apart, the first the strongest.
_WAKE_BEATS = ((0.0, 1.0), (0.3, 0.75))
_BEAT_ATTACK_S = 0.07
_BEAT_DECAY_S = 0.3


# ------------------------------------------------------------------ geometry


def frame_thickness(width: int, height: int) -> int:
    """Depth of the edge bands the frame is drawn in."""
    return int(min(150.0, max(48.0, min(width, height) * 0.075)))


def edge_strips(width: int, height: int, thickness: int) -> List[Rect]:
    """The four non-overlapping edge bands ``(x, y, w, h)``: top and bottom full width, sides between."""
    t = thickness
    return [
        (0, 0, width, t),
        (0, height - t, width, t),
        (0, t, t, height - 2 * t),
        (width - t, t, t, height - 2 * t),
    ]


def screens_to_cover(screens: Sequence[Tuple[int, int, int, int, float]],
                     busy: Optional[Tuple[int, int, int, int]]) -> List[Rect]:
    """Logical ``(x, y, w, h)`` of each screen to frame, leaving out the one a full-screen app covers.

    ``screens`` are logical geometries with their device pixel ratio; ``busy`` is the native
    ``(left, top, right, bottom)`` of the covered monitor. Qt keeps a screen's native top-left
    and scales only its size, which is how the two are matched.
    """
    covered = []
    for x, y, w, h, dpr in screens:
        if busy is not None:
            native = (x, y, x + round(w * dpr), y + round(h * dpr))
            if all(abs(a - b) <= 2 for a, b in zip(native, busy)):
                continue
        covered.append((x, y, w, h))
    return covered


# --------------------------------------------------------------------- model


class OverlayModel:
    """Qt-free state of the wake screen effect."""

    def __init__(self) -> None:
        self.engaged = False
        self.presence = 0.0          # fade envelope 0..1
        self.flash = 0.0             # wake flash 0..1
        self.breath = 0.0            # where the glow is in its breath, 0 (out) .. 1 (crest)
        self.intensity = 0.0         # overall glow 0..1
        self.level = 0.0             # smoothed audio level 0..1
        self.pulse = 0.0             # heartbeat swell 0..1 (the wake beats)
        self.pulses_sent = 0         # beats so far: wake beats and breath crests
        self.time = 0.0
        self.look_state = OrbState.LISTENING
        self._last: Optional[OrbState] = None
        self._unchanged = 0.0
        self._phase = 0.0
        self._period = _RHYTHMS[OrbState.LISTENING].period
        self._depth = _RHYTHMS[OrbState.LISTENING].depth
        self._glow = 0.0
        self._sharpness = _RHYTHMS[OrbState.LISTENING].sharpness
        self._beats: List[Tuple[float, float]] = []   # wake beats: (seconds since the beat, strength)

    @property
    def visible(self) -> bool:
        return self.engaged or self.presence > 0.02

    def observe(self, state: OrbState) -> None:
        """Take the latest orb state; a change from idle to listening is a wake."""
        previous, self._last = self._last, state
        if self.engaged:
            if state in _ENGAGED_STATES:
                if state is not previous:
                    self._unchanged = 0.0
                self.look_state = state
            else:
                self.disengage()
        elif previous is OrbState.IDLE and state is OrbState.LISTENING:
            self.engaged = True
            self.look_state = state
            self._unchanged = 0.0
            self._phase = 0.0
            self.flash = 1.0
            self._beats = [(-delay, strength) for delay, strength in _WAKE_BEATS]

    def disengage(self) -> None:
        """Start fading out."""
        self.engaged = False
        self._beats = []

    def step(self, dt: float, level: Optional[float]) -> None:
        dt = min(max(dt, 0.0), _MAX_DT)
        self.time += dt
        rhythm = _RHYTHMS.get(self.look_state, _RHYTHMS[OrbState.LISTENING])

        if self.engaged:
            self._unchanged += dt
            if self._unchanged > MAX_UNCHANGED_S:
                debug_log("wake screen effect: state unchanged too long, fading out", "desktop")
                self.disengage()
        self.presence = _clamp01(_approach(self.presence, 1.0 if self.engaged else 0.0,
                                           7.0 if self.engaged else 5.0, dt))
        self.flash = _clamp01(self.flash * math.exp(-2.4 * dt))

        # The wake heartbeat: each beat swells fast and fades.
        swell = 0.0
        beats = []
        for age, strength in self._beats:
            if age <= 0.0 < age + dt:
                self.pulses_sent += 1
            age += dt
            if age >= 0.0:
                rise = min(1.0, age / _BEAT_ATTACK_S)
                swell += strength * rise * math.exp(-max(0.0, age - _BEAT_ATTACK_S) / _BEAT_DECAY_S)
            if age < 3.0:
                beats.append((age, strength))
        self._beats = beats
        self.pulse = _clamp01(swell)

        # Breathing: the phase runs continuously, so a change of rhythm never jumps.
        self._period = _approach(self._period, rhythm.period, 3.0, dt)
        self._depth = _approach(self._depth, rhythm.depth, 3.0, dt)
        self._sharpness = _approach(self._sharpness, rhythm.sharpness, 3.0, dt)
        previous_phase = self._phase
        self._phase += dt / self._period
        if self.engaged and int(self._phase + 0.5) > int(previous_phase + 0.5):
            self.pulses_sent += 1   # a breath crest
        wave = 0.5 - 0.5 * math.cos(math.tau * self._phase)
        self.breath = _clamp01(wave ** self._sharpness)   # a soft crest and a longer rest between breaths

        if level is None:
            level = synthetic_speech_level(self.time) if self.look_state is OrbState.SPEAKING else 0.0
        target = _clamp01(level) * rhythm.level_gain
        self.level = _clamp01(_approach(self.level, target, 12.0 if target > self.level else 5.0, dt))
        self._glow = _approach(self._glow, rhythm.base, 4.0, dt)
        self.intensity = _clamp01(self._glow + self._depth * self.breath + 0.5 * self.level
                                  + 0.35 * self.pulse + 0.25 * self.flash)


# -------------------------------------------------------------------- pacing

FRAME_FAST_MS = 16     # about 60 FPS while the light changes quickly: the wake beats and the fades
FRAME_STEADY_MS = 33   # about 30 FPS for the slow breathing, which is smooth at half the cost


def frame_interval_ms(model: OverlayModel) -> int:
    """How long to wait for the next frame: short while the light changes fast, longer once it breathes."""
    changing = model.flash > 0.03 or model.pulse > 0.02 or 0.02 < model.presence < 0.98
    return FRAME_FAST_MS if changing else FRAME_STEADY_MS


TONE_FADE_S = 0.35


class ToneFade:
    """Cross-fades the glow from one palette to the next.

    Rendering a palette takes real time, so a new one is asked for (``wanted``) and
    only starts fading in once the caller says it is ready; until then the palette
    on screen is left alone. The weights in ``tones`` always add up to one.
    """

    def __init__(self, palette: Palette) -> None:
        self._shown = palette
        self._target = palette
        self._incoming: Optional[Palette] = None
        self._mix = 0.0

    def retarget(self, palette: Palette) -> None:
        self._target = palette

    @property
    def wanted(self) -> Optional[Palette]:
        """The palette to get ready, if the one wanted is not on screen (or on its way in)."""
        if self._incoming is None and self._target != self._shown:
            return self._target
        return None

    @property
    def tones(self) -> List[Tuple[Palette, float]]:
        if self._incoming is None:
            return [(self._shown, 1.0)]
        weight = self._mix * self._mix * (3.0 - 2.0 * self._mix)
        return [(self._shown, 1.0 - weight), (self._incoming, weight)]

    def step(self, dt: float, ready: bool) -> None:
        """Advance by ``dt`` seconds; ``ready`` says the palette in ``wanted`` can be drawn."""
        if self._incoming is None:
            if self._target == self._shown or not ready:
                return
            self._incoming, self._mix = self._target, 0.0
        self._mix = min(1.0, self._mix + max(0.0, dt) / TONE_FADE_S)
        if self._mix >= 1.0:
            self._shown, self._incoming, self._mix = self._incoming, None, 0.0


# ------------------------------------------------------------------- painter


# The glow's falloff from the screen edge inward, as (fraction of its depth, strength).
_GLOW_PROFILE = ((0.0, 1.0), (0.04, 0.85), (0.15, 0.6), (0.4, 0.28), (0.7, 0.08), (1.0, 0.0))
_SHALLOW, _DEEP = 0.45, 1.0  # the glow's depth (share of the band) breathed out and breathed in
_DEPTHS = (_SHALLOW, _DEEP)
_SCANLINE_STEP = 3          # hologram scanlines, as on the orb
_SCANLINE_CUT = 0.22
_CUT_TABLE = bytes(round(v * (1.0 - _SCANLINE_CUT)) for v in range(256))   # dims every byte of a row at once
_ALPHA_STEPS = 128          # alpha steps finer than this are invisible, so such a change is not repainted


class FramePainter:
    """Paints one edge band of a screen's frame, in screen coordinates.

    The glow is rendered once per band at two depths and tinted per palette, and the
    breath cross-fades between the two depths, so the light swells inward and recedes
    while a frame costs no more than a few blits. Rendering is split into small steps
    (``prepare_next``) so it can be done before the light is shown or spread over frames;
    only the palettes still in use are kept.
    """

    def __init__(self, width: int, height: int, region: Optional[Rect] = None):
        self.width, self.height = width, height
        self.thickness = frame_thickness(width, height)
        self.region = QRectF(*(region or (0, 0, width, height)))
        self._glows: Dict[Palette, Dict[float, QImage]] = {}
        self._shown: Optional[tuple] = None

    # -- rendering ----------------------------------------------------------

    def _render(self, depth: float, palette: Palette) -> QImage:
        """The glow reaching ``depth`` of the band, broken by fine scanlines, in the palette's two tones
        that alternate round the screen."""
        r = self.region
        image = QImage(max(1, int(r.width())), max(1, int(r.height())), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.translate(-r.x(), -r.y())
        w, h, t = float(self.width), float(self.height), float(self.thickness) * depth
        ink = QColor(ORB_PALETTE["white"])
        p.setPen(Qt.PenStyle.NoPen)
        for start, end, rect in (
            (QPointF(0, 0), QPointF(0, t), QRectF(0, 0, w, t)),
            (QPointF(0, h), QPointF(0, h - t), QRectF(0, h - t, w, t)),
            (QPointF(0, 0), QPointF(t, 0), QRectF(0, 0, t, h)),
            (QPointF(w, 0), QPointF(w - t, 0), QRectF(w - t, 0, t, h)),
        ):
            if not rect.intersects(r):
                continue
            g = QLinearGradient(start, end)
            for at, strength in _GLOW_PROFILE:
                g.setColorAt(at, _with_alpha(ink, strength))
            p.setBrush(g)
            p.drawRect(rect)
        p.end()
        self._cut_scanlines(image)
        p = QPainter(image)
        p.translate(-r.x(), -r.y())
        tint = QConicalGradient(QPointF(self.width / 2, self.height / 2), 90)
        colour, accent = QColor(palette[0]), QColor(palette[1])
        for at, tone in ((0.0, colour), (0.25, accent), (0.5, colour), (0.75, accent), (1.0, colour)):
            tint.setColorAt(at, tone)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(r, tint)
        p.end()
        return image

    def _cut_scanlines(self, image: QImage) -> None:
        """Dim every third screen row of ``image``: a row at a time, which is far cheaper than painting lines."""
        stride, rows = image.bytesPerLine(), image.height()
        pixels = image.bits()
        pixels.setsize(image.sizeInBytes())
        view = memoryview(pixels)
        for row in range((-int(self.region.top())) % _SCANLINE_STEP, rows, _SCANLINE_STEP):
            start = row * stride
            view[start:start + stride] = bytes(view[start:start + stride]).translate(_CUT_TABLE)

    def is_ready(self, palette: Palette) -> bool:
        """Whether the band's glow is rendered in ``palette``."""
        return len(self._glows.get(palette, ())) == len(_DEPTHS)

    def prepare_next(self, palette: Palette) -> bool:
        """Render one more piece of the glow in ``palette``; true once nothing is left to render."""
        have = self._glows.setdefault(palette, {})
        for depth in _DEPTHS:
            if depth not in have:
                have[depth] = self._render(depth, palette)
                break
        return len(have) == len(_DEPTHS)

    def retain(self, palettes: Sequence[Palette]) -> None:
        """Forget the glow of every palette not in ``palettes``."""
        self._glows = {palette: images for palette, images in self._glows.items() if palette in palettes}

    # -- drawing ------------------------------------------------------------

    def needs_repaint(self, model: OverlayModel, tones: Tones) -> bool:
        """Whether this band looks different from when it was last asked."""
        def q(value: float) -> int:
            return round(value * _ALPHA_STEPS)

        key = (q(model.presence), q(model.intensity), q(max(model.breath, model.pulse)),
               tuple((palette, q(weight)) for palette, weight in tones))
        changed, self._shown = key != self._shown, key
        return changed

    def paint(self, p: QPainter, model: OverlayModel, tones: Tones) -> None:
        """Paint the band: the glow at its breathing strength, swollen inward by each breath and heartbeat."""
        a = model.presence
        if a <= 0.0:
            return
        r = self.region
        strength = _clamp01(a * (0.35 + 0.65 * model.intensity))
        swell = max(model.breath, model.pulse)
        p.save()
        for palette, weight in tones:
            for depth, share in ((_SHALLOW, 1.0 - swell), (_DEEP, swell)):
                opacity = strength * share * weight
                if opacity > 0.004:
                    while not self.is_ready(palette):   # normally rendered ahead of time
                        self.prepare_next(palette)
                    p.setOpacity(opacity)
                    p.drawImage(QPointF(r.x(), r.y()), self._glows[palette][depth])
        p.restore()


# ---------------------------------------------------------------- controller


_WINDOW_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.WindowTransparentForInput
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.NoDropShadowWindowHint
)


class _EdgeWindow(QWidget):
    """One click-through, never-focused band of a screen's frame."""

    def __init__(self, overlay: "WakeOverlay", screen: Rect, strip: Rect):
        super().__init__(None, _WINDOW_FLAGS)
        for attribute in (Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_NoSystemBackground):
            self.setAttribute(attribute)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Jarvis wake effect")
        self._overlay = overlay
        self._strip = strip
        self._painter = FramePainter(screen[2], screen[3], strip)
        self.setGeometry(screen[0] + strip[0], screen[1] + strip[1], strip[2], strip[3])

    def prepare_next(self, palette: Palette) -> bool:
        return self._painter.prepare_next(palette)

    def retain(self, palettes: Sequence[Palette]) -> None:
        self._painter.retain(palettes)

    def is_ready(self, palette: Palette) -> bool:
        return self._painter.is_ready(palette)

    def is_prepared(self) -> bool:
        """Whether everything the window draws now is already rendered."""
        return all(self._painter.is_ready(palette) for palette, _ in self._overlay.tones)

    def refresh(self) -> None:
        """Schedule a repaint if this band looks different now."""
        if self._overlay.needs_repaint(self._painter):
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.translate(-self._strip[0], -self._strip[1])
        self._overlay.paint_screen(p, self._painter)
        p.end()
        self._overlay.note_painted()


CONFIG_KEY = "wake_overlay_enabled"


def configured_enabled() -> bool:
    """Whether the effect is switched on in the config file (on when unset or unreadable)."""
    try:
        from jarvis.config import _config_path, _load_json, get_default_config
        stored = _load_json(_config_path()).get(CONFIG_KEY)
        return bool(get_default_config()[CONFIG_KEY] if stored is None else stored)
    except Exception as exc:
        debug_log(f"wake screen effect: could not read its setting: {exc}", "desktop")
        return True


def _default_state() -> OrbState:
    from desktop_app.face_widget import get_jarvis_state
    return orb_state_for(get_jarvis_state().state.value)


def _default_state_file() -> Optional[str]:
    """The file the daemon writes its state to, which the effect watches for changes."""
    try:
        from desktop_app.face_widget import _get_jarvis_state_file
        return _get_jarvis_state_file()
    except Exception as exc:
        debug_log(f"wake screen effect: no state file to watch: {exc}", "desktop")
        return None


def _default_screens() -> List[Rect]:
    screens = []
    for screen in QGuiApplication.screens():
        g = screen.geometry()
        screens.append((g.x(), g.y(), g.width(), g.height(), screen.devicePixelRatio()))
    busy = None
    if sys.platform == "win32":
        from desktop_app.win32_overlay import fullscreen_monitor_rect
        busy = fullscreen_monitor_rect()
    covered = screens_to_cover(screens, busy)
    if len(covered) < len(screens):
        debug_log("wake screen effect: skipping a screen with a full-screen app", "desktop")
    return covered


class WakeOverlay(QObject):
    """Shows the wake screen effect on every screen while Jarvis is awake."""

    WATCH_INTERVAL_MS = 100       # the state poll: a safety net under the file watch
    FILE_SETTLE_MS = 8            # let a write to the state file finish before reading it
    FIRST_PAINT_TIMEOUT_S = 0.25  # start animating even if the first paint never reports in
    PREPARE_BUDGET_S = 0.004      # rendering a new palette takes at most this much of any frame

    def __init__(self, state_reader: Optional[Callable[[], OrbState]] = None,
                 audio_source: Optional[AudioLevelSource] = None,
                 screens: Optional[Callable[[], List[Rect]]] = None,
                 enabled: bool = True, parent: Optional[QObject] = None,
                 clock: Optional[Callable[[], float]] = None,
                 state_file: Optional[str] = None,
                 watch_interval_ms: Optional[int] = None):
        super().__init__(parent)
        self._read_state = state_reader or _default_state
        self._audio = audio_source if audio_source is not None else shared_voice_level()
        self._screens = screens or _default_screens
        self._clock = clock or _time.monotonic
        self._enabled = bool(enabled)
        self._started = False
        self.model = OverlayModel()
        self._windows: List[_EdgeWindow] = []
        self._fade = ToneFade(state_colours(OrbState.LISTENING))
        self._kept: tuple = ()
        self._shown_at = self._last_frame = self._clock()
        self._painted = False       # a window has painted since the light was shown
        self._animating = False     # the animation clock is running: it starts at the first painted frame
        # Only the real state channel is watched; a test or caller with its own reader is polled alone.
        self._state_file = state_file if state_file is not None else (
            None if state_reader is not None else _default_state_file())

        self._watch = QTimer(self)
        self._watch.setInterval(watch_interval_ms or self.WATCH_INTERVAL_MS)
        self._watch.timeout.connect(self.poll)
        self._file_watch = QFileSystemWatcher(self)
        self._file_watch.fileChanged.connect(self._on_state_file_changed)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(self.FILE_SETTLE_MS)
        self._settle.timeout.connect(self.poll)
        self._frame = QTimer(self)
        self._frame.setTimerType(Qt.TimerType.PreciseTimer)   # a coarse timer drifts by whole frames
        self._frame.setInterval(FRAME_FAST_MS)
        self._frame.timeout.connect(self._on_frame)

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Begin watching for a wake (if enabled)."""
        self._started = True
        if self._enabled:
            self._begin_watching()

    def shutdown(self) -> None:
        """Stop watching and remove anything on screen."""
        self._started = False
        self._end_watching()
        self._teardown()

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        debug_log(f"wake screen effect {'enabled' if enabled else 'disabled'}", "desktop")
        self.model = OverlayModel()
        if enabled:
            if self._started:
                self._begin_watching()
        else:
            self._end_watching()
            self._teardown()

    def is_enabled(self) -> bool:
        return self._enabled

    def is_watching(self) -> bool:
        return self._watch.isActive()

    def is_animating(self) -> bool:
        return self._frame.isActive()

    def frame_interval(self) -> int:
        """Milliseconds between frames right now."""
        return self._frame.interval()

    def windows(self) -> List[QWidget]:
        return list(self._windows)

    @property
    def tones(self) -> List[Tuple[Palette, float]]:
        """The palettes the glow is drawn in, with their weights (they add up to one)."""
        return self._fade.tones

    def poll(self) -> None:
        """Read the state once; shows the frame on a wake."""
        if not self._enabled:
            return
        self._watch_state_file()
        try:
            state = self._read_state()
        except Exception:
            state = OrbState.OFFLINE
        was_engaged = self.model.engaged
        self.model.observe(state)
        if self.model.engaged and not was_engaged:
            debug_log("wake screen effect: wake detected", "desktop")
            self._show()
        elif was_engaged and not self.model.engaged:
            debug_log(f"wake screen effect: conversation ended ({state.value}), fading out", "desktop")

    def tick(self, dt: float) -> None:
        """Advance the animation by ``dt`` seconds; removes the windows once faded out."""
        if not self._windows:
            return
        self.model.step(dt, self._audio.current())
        self._fade.retarget(state_colours(self.model.look_state))
        wanted = self._fade.wanted
        self._fade.step(dt, wanted is not None and self._prepare(wanted))
        if not self.model.visible:
            self._teardown()
            return
        self._release_unused_palettes()
        for window in self._windows:
            window.refresh()
        interval = frame_interval_ms(self.model)
        if interval != self._frame.interval():
            self._frame.setInterval(interval)

    def needs_repaint(self, painter: FramePainter) -> bool:
        return painter.needs_repaint(self.model, self.tones)

    def paint_screen(self, p: QPainter, painter: FramePainter) -> None:
        painter.paint(p, self.model, self.tones)

    def note_painted(self) -> None:
        """A window has painted: from now on the animation counts time."""
        if not self._painted:
            self._painted = True
            self._start_clock()

    # -- internals ----------------------------------------------------------

    def _begin_watching(self) -> None:
        self._watch.start()
        self._watch_state_file()
        debug_log("wake screen effect: watching for a wake"
                  f"{' (state file watch on)' if self._file_watch.files() else ''}", "desktop")

    def _end_watching(self) -> None:
        self._watch.stop()
        self._settle.stop()
        if self._file_watch.files():
            self._file_watch.removePaths(self._file_watch.files())

    def _watch_state_file(self) -> None:
        """Have Qt tell us when the state file changes (and again if it was replaced), so a wake is seen at once."""
        path = self._state_file
        if path and path not in self._file_watch.files() and os.path.exists(path):
            self._file_watch.addPath(path)

    def _on_state_file_changed(self, _path: str) -> None:
        self._settle.start()

    def _start_clock(self) -> None:
        self._animating = True
        self._last_frame = self._clock()

    def _prepare(self, palette: Palette) -> bool:
        """Render more of ``palette`` for every window within this frame's budget; true when all is ready."""
        deadline = self._clock() + self.PREPARE_BUDGET_S
        for window in self._windows:
            while not window.is_ready(palette):
                if self._clock() >= deadline:
                    return False
                window.prepare_next(palette)
        return True

    def _release_unused_palettes(self) -> None:
        keep = tuple(palette for palette, _ in self._fade.tones)
        if self._fade.wanted is not None:
            keep += (self._fade.wanted,)
        if keep != self._kept:
            self._kept = keep
            for window in self._windows:
                window.retain(keep)

    def _show(self) -> None:
        if self._windows:
            return  # still fading out from the last conversation: carry on with the same windows
        began = self._clock()
        try:
            screens = self._screens()
        except Exception as exc:
            debug_log(f"wake screen effect: could not read screens: {exc}", "desktop")
            screens = []
        for screen in screens:
            thickness = frame_thickness(screen[2], screen[3])
            for strip in edge_strips(screen[2], screen[3], thickness):
                if strip[2] <= 0 or strip[3] <= 0:
                    continue
                self._windows.append(_EdgeWindow(self, screen, strip))
        if not self._windows:
            return
        # Everything is rendered before anything is shown, so the first frames cost no more than the rest.
        palette = state_colours(self.model.look_state)
        self._fade = ToneFade(palette)
        self._kept = (palette,)
        for window in self._windows:
            while not window.prepare_next(palette):
                pass
        rendered = self._clock()
        never_activate = None
        if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
            from desktop_app.win32_overlay import make_never_activate
            never_activate = make_never_activate
        self._painted = self._animating = False
        for window in self._windows:
            if never_activate is not None:
                never_activate(int(window.winId()))
            window.show()
        self._shown_at = self._last_frame = self._clock()
        self._frame.setInterval(FRAME_FAST_MS)
        self._frame.start()
        debug_log(f"wake screen effect shown on {len(screens)} screen(s): rendered in "
                  f"{(rendered - began) * 1000:.0f} ms, windows up in {(self._shown_at - rendered) * 1000:.0f} ms",
                  "desktop")

    def _teardown(self) -> None:
        self._frame.stop()
        if not self._windows:
            return
        count = len(self._windows)
        for window in self._windows:
            window.hide()
            window.close()
            window.deleteLater()
        self._windows = []
        debug_log(f"wake screen effect removed ({count} windows)", "desktop")

    def _on_frame(self) -> None:
        now = self._clock()
        if not self._animating:
            # The animation counts from the first frame that is really on screen, never from the build.
            if not self._painted and now - self._shown_at < self.FIRST_PAINT_TIMEOUT_S:
                return
            self._start_clock()
        dt, self._last_frame = now - self._last_frame, now
        self.tick(dt)

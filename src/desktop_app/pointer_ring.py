"""
Pointer ring: a small glowing ring around the mouse pointer while gesture snapping is on.

Layers (see ``pointer_ring.spec.md``):

- ``RingModel``: Qt-free. Shows while the gesture snapping flag and the
  ``gesture_snap_pointer_ring`` switch are both on, fades in and out, and
  animates a gentle breathing glow.
- ``ring_rect``: Qt-free geometry, the ring's window centred on the pointer.
- ``PointerRing``: the Qt controller. Polls the flag, shows one small
  click-through window and keeps it on the pointer while visible.

``python -m desktop_app.pointer_ring`` shows the ring on its own while the flag is on.
"""

from __future__ import annotations

import math
import sys
import time as _time
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QConicalGradient, QCursor, QGuiApplication, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget

from desktop_app.orb_widget import (  # the orb's colours keep the ring, the orb and the wake effect one look
    OrbState,
    _clamp01,
    _with_alpha,
    state_colours,
)
from desktop_app.themes import ORB_PALETTE
from jarvis.debug import debug_log

Rect = Tuple[int, int, int, int]

CONFIG_KEY = "gesture_snap_pointer_ring"
RING_SIZE = 64            # logical pixels; Qt scales logical pixels to each screen's DPI
FADE_S = 0.2              # fade in and fade out
BREATH_PERIOD_S = 2.4     # one gentle breath
PAINT_INTERVAL_S = 1 / 24  # the glow changes slowly, so it is repainted far less often than the window follows
_MAX_DT = 0.1
_HOLLOW = 0.36            # share of the radius left clear around the pointer's tip
_RING_AT = 0.62           # where the thin bright ring sits, as a share of the radius


# ------------------------------------------------------------------ geometry


def ring_rect(cursor: Tuple[int, int], size: int = RING_SIZE) -> Rect:
    """Logical ``(x, y, w, h)`` of the ring's window, centred on the pointer at ``cursor``."""
    x, y = cursor
    return (int(x) - size // 2, int(y) - size // 2, size, size)


# --------------------------------------------------------------------- model


class RingModel:
    """Qt-free state of the pointer ring."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.flag_on = False
        self.presence = 0.0     # fade envelope 0..1
        self.breath = 0.0       # 0 (out) .. 1 (crest)
        self.time = 0.0

    @property
    def wanted(self) -> bool:
        return self.enabled and self.flag_on

    @property
    def visible(self) -> bool:
        return self.wanted or self.presence > 0.0

    @property
    def glow(self) -> float:
        """Overall strength of the ring 0..1: the fade times a breathing glow that never goes dark."""
        return _clamp01(self.presence * (0.6 + 0.4 * self.breath))

    def observe(self, flag_on: bool) -> None:
        """Take the latest gesture snapping flag."""
        self.flag_on = bool(flag_on)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def step(self, dt: float) -> None:
        dt = min(max(dt, 0.0), _MAX_DT)
        self.time += dt
        delta = dt / FADE_S
        self.presence = _clamp01(self.presence + (delta if self.wanted else -delta))
        self.breath = 0.5 - 0.5 * math.cos(math.tau * self.time / BREATH_PERIOD_S)


# ---------------------------------------------------------------- the window


_WINDOW_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.WindowTransparentForInput
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.NoDropShadowWindowHint
)


class _RingWindow(QWidget):
    """The one click-through, never-focused window the ring is drawn in."""

    def __init__(self, ring: "PointerRing"):
        super().__init__(None, _WINDOW_FLAGS)
        for attribute in (Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_NoSystemBackground):
            self.setAttribute(attribute)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Jarvis pointer ring")
        self._ring = ring
        self.resize(RING_SIZE, RING_SIZE)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_ring(p, self.width(), self._ring.model)
        p.end()


def paint_ring(p: QPainter, size: int, model: RingModel) -> None:
    """A soft glow round a thin bright ring, in the orb's two holographic tones, hollow in the middle."""
    strength = model.glow
    if strength <= 0.0:
        return
    colour, accent = (QColor(c) for c in state_colours(OrbState.LISTENING))
    centre = QPointF(size / 2.0, size / 2.0)
    radius = size / 2.0

    glow = QRadialGradient(centre, radius)
    for at, alpha in ((0.0, 0.0), (_HOLLOW, 0.0), (_HOLLOW + 0.08, 0.18), (_RING_AT, 0.62),
                      (_RING_AT + 0.14, 0.28), (1.0, 0.0)):
        glow.setColorAt(at, _with_alpha(colour, alpha * strength))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(glow)
    p.drawEllipse(centre, radius, radius)

    # The thin ring alternates the two tones round the circle and turns slowly, like the orb's shimmer.
    white = QColor(ORB_PALETTE["white"])
    tint = QConicalGradient(centre, (model.time * 40.0) % 360.0)
    for at, tone in ((0.0, colour), (0.25, accent), (0.5, white), (0.75, accent), (1.0, colour)):
        tint.setColorAt(at, _with_alpha(tone, 0.95 * strength))
    pen = QPen(tint, 2.0)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    r = radius * _RING_AT
    p.drawEllipse(QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r))


# ---------------------------------------------------------------- controller


def configured_enabled() -> bool:
    """Whether the ring is switched on in the config file (the default when unset, unreadable or not a bool)."""
    try:
        from jarvis.config import _config_path, _load_json, get_default_config
        stored = _load_json(_config_path()).get(CONFIG_KEY)
        return stored if isinstance(stored, bool) else bool(get_default_config()[CONFIG_KEY])
    except Exception as exc:
        debug_log(f"pointer ring: could not read its setting: {exc}", "desktop")
        return True


def _default_flag() -> bool:
    from jarvis.gestures.snap_state import is_on
    return bool(is_on())


def _default_cursor() -> Tuple[int, int]:
    pos = QCursor.pos()
    return pos.x(), pos.y()


class PointerRing(QObject):
    """Shows a glowing ring round the mouse pointer while gesture snapping is on."""

    POLL_INTERVAL_MS = 200   # about five reads of the flag a second
    FRAME_INTERVAL_MS = 16   # about 60 Hz, only while the ring is visible

    def __init__(self, flag_reader: Optional[Callable[[], bool]] = None,
                 cursor: Optional[Callable[[], Tuple[int, int]]] = None,
                 enabled: bool = True, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._read_flag = flag_reader or _default_flag
        self._cursor = cursor or _default_cursor
        self._started = False
        self.model = RingModel(enabled=enabled)
        self._window: Optional[_RingWindow] = None
        self._flag_error_logged = False
        self._last_frame = _time.monotonic()
        self._since_paint = 0.0

        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_INTERVAL_MS)
        self._poll.timeout.connect(self.poll)
        self._frame = QTimer(self)
        self._frame.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame.setInterval(self.FRAME_INTERVAL_MS)
        self._frame.timeout.connect(self._on_frame)

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Begin watching the gesture snapping flag (if enabled)."""
        self._started = True
        if self.model.enabled:
            self._poll.start()

    def shutdown(self) -> None:
        """Stop watching and remove the ring."""
        self._started = False
        self._poll.stop()
        self._frame.stop()
        self.model.presence = 0.0
        if self._window is not None:
            self._window.hide()
            self._window.close()
            self._window.deleteLater()
            self._window = None

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.model.enabled:
            return
        self.model.set_enabled(enabled)
        debug_log(f"pointer ring {'enabled' if enabled else 'disabled'}", "desktop")
        if enabled:
            if self._started:
                self._poll.start()
        else:
            self._poll.stop()
            self.model.presence = 0.0
            self._hide()

    def is_enabled(self) -> bool:
        return self.model.enabled

    def is_polling(self) -> bool:
        return self._poll.isActive()

    def is_following(self) -> bool:
        return self._frame.isActive()

    def window(self) -> Optional[QWidget]:
        return self._window

    def poll(self) -> None:
        """Read the flag once; shows the ring when gesture snapping starts."""
        if not self.model.enabled:
            return
        try:
            on = bool(self._read_flag())
            self._flag_error_logged = False
        except Exception as exc:
            if not self._flag_error_logged:
                debug_log(f"pointer ring: gesture snapping flag unreadable, counting it as off: {exc}", "desktop")
                self._flag_error_logged = True
            on = False
        was = self.model.flag_on
        self.model.observe(on)
        if on and not was:
            debug_log("pointer ring: gesture snapping on", "desktop")
        elif was and not on:
            debug_log("pointer ring: gesture snapping off, fading out", "desktop")
        if self.model.wanted and not self._frame.isActive():
            self._show()

    def tick(self, dt: float) -> None:
        """Advance the animation by ``dt`` seconds and keep the ring on the pointer; hides it once faded."""
        if self._window is None or not self._window.isVisible():
            return
        self.model.step(dt)
        if not self.model.visible:
            self._hide()
            return
        self._follow()
        self._since_paint += dt
        if self._since_paint >= PAINT_INTERVAL_S:
            self._since_paint = 0.0
            self._window.update()

    # -- internals ----------------------------------------------------------

    def _show(self) -> None:
        if self._window is None:
            self._window = _RingWindow(self)
            if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
                from desktop_app.win32_overlay import make_never_activate
                make_never_activate(int(self._window.winId()))
        self._follow()
        if not self._window.isVisible():
            self._window.show()
        self._last_frame = _time.monotonic()
        self._frame.start()

    def _hide(self) -> None:
        self._frame.stop()
        if self._window is not None and self._window.isVisible():
            self._window.hide()
            debug_log("pointer ring hidden", "desktop")

    def _follow(self) -> None:
        try:
            x, y, _, _ = ring_rect(self._cursor(), self._window.width())
        except Exception:
            return
        if (self._window.x(), self._window.y()) != (x, y):
            self._window.move(x, y)

    def _on_frame(self) -> None:
        now = _time.monotonic()
        dt, self._last_frame = now - self._last_frame, now
        self.tick(dt)


# ------------------------------------------------------------- manual check


def main() -> int:
    """Show the ring while gesture snapping is on (run ``python -m jarvis.gestures --snap`` alongside)."""
    import signal

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ring = PointerRing(enabled=True)
    ring.start()
    print("🖱️ Pointer ring: shown round the pointer while gesture snapping is on", flush=True)
    print("   ⌨️ Ctrl+C to quit", flush=True)

    shown = {"on": False}

    def report() -> None:
        if ring.model.wanted != shown["on"]:
            shown["on"] = ring.model.wanted
            print("   🟢 Gesture snapping on: ring shown" if shown["on"]
                  else "   ⚪ Gesture snapping off: ring hidden", flush=True)

    watch = QTimer()
    watch.timeout.connect(report)   # also lets Python handle Ctrl+C between Qt events
    watch.start(200)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    try:
        app.exec()
    finally:
        ring.shutdown()
        print("🖱️ Pointer ring stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

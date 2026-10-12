"""The diagnostic preview: the camera's mirrored image with the detected hand drawn on it.

Used only by ``python -m jarvis.gestures --preview``, which the user opens to see what the camera sees.
Frames are drawn on a copy and shown live; nothing is kept beyond the newest frame or saved, and the window is
kept out of screen captures. See
``gestures.spec.md``, Diagnostic command.
"""
from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Optional

from .window_snap import describe_hand, palm_centre
from .hand_tracking import HandFrame

_BONES = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
          (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20))
_BONE = (255, 200, 0)      # BGR
_JOINT = (255, 255, 255)
_TIP = (0, 220, 255)


def caption(hand_frame: Optional[HandFrame], state: str, settings: Any = None) -> str:
    """One line about the strongest hand: side, each finger's curl, the gesture and the gesture snapping state."""
    if hand_frame is None or not hand_frame.hands:
        return f"no hand  ·  {state}"
    hand = max(hand_frame.hands, key=lambda h: h.score)
    return f"{hand.side} hand  ·  {describe_hand(hand, settings)}  ·  {state}"


def draw_overlay(image: Any, hand_frame: Optional[HandFrame]) -> Any:
    """A mirrored copy of the BGR ``image`` with every hand's landmarks drawn on it."""
    import cv2

    drawn = cv2.flip(image, 1)
    height, width = drawn.shape[:2]
    for hand in (hand_frame.hands if hand_frame is not None else ()):
        pixels = [(int(p.x * width), int(p.y * height)) for p in hand.points]
        for a, b in _BONES:
            cv2.line(drawn, pixels[a], pixels[b], _BONE, 2, cv2.LINE_AA)
        for point in pixels:
            cv2.circle(drawn, point, 3, _JOINT, -1, cv2.LINE_AA)
        px, py = palm_centre(hand)
        cv2.circle(drawn, (int(px * width), int(py * height)), 9, _TIP, 2, cv2.LINE_AA)  # what the pointer follows
    return drawn


class FrameTap:
    """Wraps a camera so the preview can show the newest frame it delivered (only the newest is kept)."""

    def __init__(self, camera: Any):
        self._camera = camera
        self._lock = threading.Lock()
        self._latest: Any = None

    def open(self) -> bool:
        return self._camera.open()

    def read(self) -> Any:
        frame = self._camera.read()
        if frame is not None:
            with self._lock:
                self._latest = frame.copy()
        return frame

    def close(self) -> None:
        self._camera.close()
        with self._lock:
            self._latest = None

    def take(self) -> Any:
        with self._lock:
            return self._latest


def make_window() -> Any:
    """The preview window (needs a ``QApplication``): kept above other windows, with ``picture`` and ``text``
    labels. On Windows it is kept out of screen captures every time it is shown, so its camera image never
    reaches a screenshot, a screen share or a model."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

    class PreviewWindow(QWidget):
        def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
            super().showEvent(event)
            _exclude_from_capture(int(self.winId()))

    window = PreviewWindow()
    window.setWindowTitle("Jarvis hand tracking preview")
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    window.picture, window.text = QLabel("Waiting for the camera…"), QLabel()
    window.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.text.setStyleSheet("font-size: 16px; padding: 6px;")
    layout = QVBoxLayout(window)
    layout.addWidget(window.picture)
    layout.addWidget(window.text)
    return window


_warned = False


def _exclude_from_capture(hwnd: int) -> None:
    global _warned
    if sys.platform != "win32":
        return
    from ..platform.windows.window_snap import exclude_from_capture
    if not exclude_from_capture(hwnd) and not _warned:
        _warned = True
        print("   ⚠️ The preview could not be hidden from screenshots and screen sharing", flush=True)


def run_window(tap: FrameTap, latest_hands: Callable[[], Optional[HandFrame]], state: Callable[[], str],
               seconds: Optional[float], every_second: Optional[Callable[[], None]] = None,
               settings: Any = None) -> None:
    """Show the preview, kept above other windows, until it is closed or ``seconds`` pass (runs Qt on this
    thread). ``every_second`` runs about once a second, for the console readout."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QImage, QPixmap
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    window = make_window()
    picture, text = window.picture, window.text

    def refresh() -> None:
        if state() == "off":  # crossed wrists turned gesture snapping off
            window.close()
            return
        image = tap.take()
        hands = latest_hands()
        text.setText(caption(hands, state(), settings))
        if image is None:
            return
        import cv2
        rgb = cv2.cvtColor(draw_overlay(image, hands), cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        picture.setPixmap(QPixmap.fromImage(QImage(rgb.data, width, height, 3 * width,
                                                   QImage.Format.Format_RGB888).copy()))

    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(33)
    ticker = QTimer()
    if every_second is not None:
        ticker.timeout.connect(every_second)
        ticker.start(1000)
    if seconds:
        QTimer.singleShot(int(seconds * 1000), window.close)
    window.resize(680, 560)
    window.show()
    window.raise_()
    window.activateWindow()
    app.exec()
    timer.stop()
    ticker.stop()

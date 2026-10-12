"""The diagnostic preview draws the hand on the mirrored camera image; nothing is kept or saved."""
import numpy as np

from jarvis.gestures.preview import caption, draw_overlay
from tests.synthetic_hands import frame, hand


def test_the_hand_is_drawn_where_the_detector_found_it():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    shown = hand(0.25, 0.5)
    drawn = draw_overlay(image, frame(0.0, shown))
    tip = shown.point(8)  # the index fingertip
    x, y = int(tip.x * 640), int(tip.y * 480)
    assert drawn[y - 3:y + 4, x - 3:x + 4].any()
    assert not image.any()  # the camera frame itself is untouched


def test_the_image_is_mirrored_like_the_landmarks():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    image[:, :10] = 255  # a bright stripe on the camera's left edge
    drawn = draw_overlay(image, frame(0.0))
    assert drawn[:, -10:].all() and not drawn[:, :10].any()


def test_the_caption_reports_the_fingers_gesture_and_state():
    text = caption(frame(0.0, hand(0.5, 0.5, gesture="two")), "scrolling")
    assert "right" in text and "index 1.35" in text and "two fingers" in text and "scrolling" in text
    assert "no hand" in caption(frame(0.0), "paused")

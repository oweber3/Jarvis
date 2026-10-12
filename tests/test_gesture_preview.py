"""The diagnostic preview draws the hand on the mirrored camera image; nothing is kept or saved."""
import numpy as np

from jarvis.gestures.preview import caption, draw_overlay
from tests.synthetic_hands import frame, hand


def test_the_hand_is_drawn_where_the_detector_found_it():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    drawn = draw_overlay(image, frame(0.0, hand(0.25, 0.5)))
    x, y = int(0.25 * 640), int(0.5 * 480)
    assert drawn[y - 3:y + 4, x - 3:x + 4].any()
    assert not image.any()  # the camera frame itself is untouched


def test_the_image_is_mirrored_like_the_landmarks():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    image[:, :10] = 255  # a bright stripe on the camera's left edge
    drawn = draw_overlay(image, frame(0.0))
    assert drawn[:, -10:].all() and not drawn[:, :10].any()


def test_the_caption_reports_pinch_pose_and_state():
    text = caption(frame(0.0, hand(0.5, 0.5, pinch=0.1)), "pinched")
    assert "right" in text and "0.10" in text and "pinched" in text
    assert "no hand" in caption(frame(0.0), "paused")

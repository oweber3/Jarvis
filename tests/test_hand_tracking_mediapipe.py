"""The real MediaPipe Hand Landmarker on photos of hands.

Skipped when MediaPipe or the cached, verified model is missing: these tests never download anything.
"""
import hashlib
import threading
from pathlib import Path

import pytest

from jarvis.gestures import hand_tracking as ht
from jarvis.gestures.hand_tracking import HandTracker, Landmark
from jarvis.utils import model_files

FIXTURES = Path(__file__).parent / "fixtures" / "hands"


def _cached_model():
    path = model_files.models_dir() / ht.HAND_MODEL_FILENAME
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() == ht.HAND_MODEL_SHA256:
            return path
    except OSError:
        pass
    return None


MODEL = _cached_model()
mediapipe = pytest.importorskip("mediapipe", reason="MediaPipe is not installed")
cv2 = pytest.importorskip("cv2")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(MODEL is None, reason="the hand model is not cached; run python -m jarvis.gestures once")]


def _photo(name):
    image = cv2.imread(str(FIXTURES / name))
    assert image is not None
    return image


@pytest.fixture
def hands():
    return ht.MediaPipeHands(str(MODEL))


class TestDetection:
    def test_a_raised_index_finger_is_found_with_its_tip_above_the_wrist(self, hands):
        found = hands(_photo("pointing_up.jpg"), 1.0)
        assert len(found) == 1
        hand = found[0]
        assert hand.score > 0.9
        assert len(hand.points) == 21 and len(hand.world) == 21
        assert hand.point(Landmark.INDEX_TIP).y < hand.point(Landmark.WRIST).y
        assert all(abs(c) < 0.2 for p in hand.world for c in p), "world points are metres around the hand"

    def test_a_mirrored_camera_frame_swaps_the_side_and_mirrors_x(self, hands):
        photo = _photo("pointing_up.jpg")
        plain = hands(photo, 1.0)[0]
        mirrored = hands(cv2.flip(photo, 1), 2.0)[0]
        assert {plain.side, mirrored.side} == {"left", "right"}
        tip, mirrored_tip = plain.point(Landmark.INDEX_TIP), mirrored.point(Landmark.INDEX_TIP)
        assert mirrored_tip.x == pytest.approx(1 - tip.x, abs=0.03)
        assert mirrored_tip.y == pytest.approx(tip.y, abs=0.03)

    def test_two_hands_are_told_apart(self, hands):
        found = hands(_photo("woman_hands.jpg"), 1.0)
        assert sorted(h.side for h in found) == ["left", "right"]

    def test_a_repeated_capture_time_is_accepted(self, hands):
        photo = _photo("pointing_up.jpg")
        hands(photo, 5.0)
        assert len(hands(photo, 5.0)) == 1

    def test_a_photo_with_no_hand_finds_nothing(self, hands):
        assert hands(_photo("pointing_up.jpg")[:40, :40].copy(), 1.0) == ()


class _StillCamera:
    def __init__(self, image):
        self.image = image

    def open(self):
        return True

    def read(self):
        return self.image.copy()

    def close(self):
        pass


def test_the_photo_command_prints_the_hands_it_found(capsys):
    from jarvis.gestures import __main__ as cli

    assert cli.main(["--image", str(FIXTURES / "woman_hands.jpg")]) == 0
    out = capsys.readouterr().out
    assert "2 hands" in out
    assert "left" in out and "right" in out


def test_the_pipeline_delivers_the_real_detector_landmarks():
    tracker = HandTracker(0, camera_factory=lambda index: _StillCamera(_photo("pointing_up.jpg")), max_fps=30)
    frames, done = [], threading.Event()

    def collect(frame):
        frames.append(frame)
        if len(frames) >= 3:
            done.set()

    tracker.subscribe(collect)
    assert tracker.start() is True
    try:
        assert done.wait(10)
    finally:
        tracker.stop()
    assert all(len(f.hands) == 1 for f in frames)

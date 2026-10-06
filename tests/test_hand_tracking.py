"""The hand-tracking pipeline against a fake camera and a fake detector (no webcam, no MediaPipe)."""
import threading
import time

import numpy as np
import pytest

from jarvis.gestures import hand_tracking as ht
from jarvis.gestures.hand_tracking import Hand, HandFrame, HandTracker, Landmark, Point
from jarvis.utils.model_files import ModelUnavailable


def _frame(n):
    """A tiny BGR frame that carries its sequence number."""
    return np.full((4, 6, 3), n % 256, dtype=np.uint8)


def _hand_for(frame):
    n = float(frame[0, 0, 0])
    return Hand(side="right", score=0.9,
                points=tuple(Point(n / 1000, 0.5, 0.0) for _ in range(21)),
                world=tuple(Point(0.0, 0.0, 0.0) for _ in range(21)))


class FakeCamera:
    """Delivers numbered frames. ``fail_opens`` opens fail first; ``drop_after`` frames, it goes dark once."""

    def __init__(self, fail_opens=0, drop_after=None):
        self.fail_opens = fail_opens
        self.drop_after = drop_after
        self.opened = 0
        self.closed = 0
        self.is_open = False
        self.n = 0
        self._dropped = False

    def open(self):
        self.opened += 1
        if self.fail_opens > 0:
            self.fail_opens -= 1
            return False
        self.is_open = True
        return True

    def read(self):
        if not self.is_open:
            return None
        if self.drop_after is not None and self.n >= self.drop_after and not self._dropped:
            self._dropped = True
            return None
        self.n += 1
        return _frame(self.n)

    def close(self):
        self.closed += 1
        self.is_open = False


class FakeHands:
    """Finds one hand in frames whose number is odd, after ``delay`` seconds; raises on ``bad`` frames."""

    def __init__(self, delay=0.0, bad=()):
        self.delay = delay
        self.bad = set(bad)
        self.seen = []

    def __call__(self, frame, timestamp):
        n = int(frame[0, 0, 0])
        self.seen.append(n)
        if self.delay:
            time.sleep(self.delay)
        if n in self.bad:
            raise RuntimeError("detector failed")
        return (_hand_for(frame),) if n % 2 else ()


class Collector:
    def __init__(self):
        self.frames = []
        self.event = threading.Event()
        self.want = 1

    def __call__(self, frame):
        self.frames.append(frame)
        if len(self.frames) >= self.want:
            self.event.set()

    def wait_for(self, count, timeout=3.0):
        self.want = count
        if len(self.frames) >= count:
            return True
        self.event.clear()
        return self.event.wait(timeout) or len(self.frames) >= count


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(ht, "_BACKOFF_FIRST_SEC", 0.05)
    monkeypatch.setattr(ht, "_BACKOFF_MAX_SEC", 0.1)


def _tracker(camera=None, hands=None, **kwargs):
    camera = camera or FakeCamera()
    hands = hands or FakeHands()
    tracker = HandTracker(0, hands_factory=lambda: hands, camera_factory=lambda index: camera, **kwargs)
    return tracker, camera, hands


@pytest.mark.unit
class TestDelivery:
    def test_subscribers_receive_hand_frames_in_capture_order(self):
        tracker, camera, _ = _tracker(max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        assert tracker.start() is True
        try:
            assert got.wait_for(6)
        finally:
            tracker.stop()
        times = [f.time for f in got.frames]
        assert times == sorted(times) and len(set(times)) == len(times)
        assert all(isinstance(f, HandFrame) and (f.width, f.height) == (6, 4) for f in got.frames)

    def test_frames_without_hands_are_delivered_too(self):
        tracker, _, _ = _tracker(max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(6)
        finally:
            tracker.stop()
        counts = {len(f.hands) for f in got.frames}
        assert counts == {0, 1}

    def test_hands_carry_the_detector_landmarks(self):
        tracker, _, _ = _tracker(max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(4)
        finally:
            tracker.stop()
        hand = next(f.hands[0] for f in got.frames if f.hands)
        assert hand.side == "right"
        assert hand.point(Landmark.INDEX_TIP) == hand.points[8]
        assert hand.world_point(Landmark.WRIST) == hand.world[0]

    def test_a_slow_detector_works_on_the_newest_frame_and_skips_stale_ones(self):
        tracker, camera, hands = _tracker(hands=FakeHands(delay=0.05), max_fps=500)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(5)
        finally:
            tracker.stop()
        assert hands.seen == sorted(hands.seen)
        assert camera.n > len(hands.seen) + 5, "frames captured while detecting are replaced, not queued"

    def test_frames_are_read_no_faster_than_max_fps(self):
        tracker, camera, _ = _tracker(max_fps=10)
        tracker.start()
        time.sleep(0.55)
        tracker.stop()
        assert 3 <= camera.n <= 8

    def test_a_failing_subscriber_does_not_stop_the_others(self):
        tracker, _, _ = _tracker(max_fps=200)

        def broken(frame):
            raise ValueError("bug in a subscriber")

        got = Collector()
        tracker.subscribe(broken)
        tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(3)
        finally:
            tracker.stop()

    def test_an_unsubscribed_callback_receives_nothing_more(self):
        tracker, _, _ = _tracker(max_fps=200)
        got = Collector()
        unsubscribe = tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(2)
            unsubscribe()
            count = len(got.frames)
            time.sleep(0.15)
            assert len(got.frames) <= count + 1  # at most one frame already in flight
        finally:
            tracker.stop()

    def test_a_frame_the_detector_cannot_read_is_skipped_and_reported_once(self, capsys):
        tracker, _, hands = _tracker(hands=FakeHands(bad={2, 3, 4}), max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(4)
        finally:
            tracker.stop()
        out = capsys.readouterr().out
        assert out.count("✋") == 1
        delivered = {round(f.hands[0].points[0].x * 1000) for f in got.frames if f.hands}
        assert 3 not in delivered and delivered, "the unreadable frame is skipped, later ones still arrive"


@pytest.mark.unit
class TestLifecycle:
    def test_stop_releases_the_camera(self):
        tracker, camera, _ = _tracker(max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        assert got.wait_for(1)
        assert tracker.running
        tracker.stop()
        assert not tracker.running
        assert camera.closed >= 1 and not camera.is_open

    def test_stop_when_not_running_is_harmless(self):
        tracker, camera, _ = _tracker()
        tracker.stop()
        assert camera.opened == 0

    def test_starting_twice_opens_one_camera(self):
        tracker, camera, _ = _tracker(max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        assert tracker.start() is True
        assert tracker.start() is True
        try:
            assert got.wait_for(2)
        finally:
            tracker.stop()
        assert camera.opened == 1

    def test_it_can_be_started_again_after_stopping(self):
        tracker, camera, _ = _tracker(max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        assert got.wait_for(1)
        tracker.stop()
        tracker.start()
        try:
            assert got.wait_for(len(got.frames) + 2)
        finally:
            tracker.stop()
        assert not camera.is_open


@pytest.mark.unit
class TestCameraTrouble:
    def test_a_camera_that_will_not_open_is_retried_and_reported_once(self, capsys):
        tracker, camera, _ = _tracker(camera=FakeCamera(fail_opens=3), max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        assert tracker.start() is True
        try:
            assert got.wait_for(2)
        finally:
            tracker.stop()
        assert camera.opened == 4
        out = capsys.readouterr().out
        assert out.count("cannot use camera 0") == 1
        assert out.count("is working again") == 1

    def test_a_camera_that_goes_dark_is_reopened(self, capsys):
        tracker, camera, _ = _tracker(camera=FakeCamera(drop_after=2), max_fps=200)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        try:
            assert got.wait_for(5)
        finally:
            tracker.stop()
        assert camera.opened == 2
        out = capsys.readouterr().out
        assert out.count("cannot use camera 0") == 1


@pytest.mark.unit
class TestStayingOff:
    def test_without_mediapipe_it_says_why_once_and_never_opens_the_camera(self, capsys):
        camera = FakeCamera()

        def no_mediapipe():
            raise ImportError("No module named 'mediapipe'")

        tracker = HandTracker(0, hands_factory=no_mediapipe, camera_factory=lambda index: camera)
        assert tracker.start() is False
        assert tracker.start() is False
        assert not tracker.running
        assert camera.opened == 0
        out = capsys.readouterr().out
        assert out.count("✋") == 1
        assert "mediapipe" in out.lower()

    def test_without_the_model_it_says_why_and_never_opens_the_camera(self, capsys):
        camera = FakeCamera()

        def no_model():
            raise ModelUnavailable("the hand tracking model could not be downloaded (offline)")

        tracker = HandTracker(0, hands_factory=no_model, camera_factory=lambda index: camera)
        assert tracker.start() is False
        assert camera.opened == 0
        out = capsys.readouterr().out
        assert out.count("✋") == 1
        assert "model" in out.lower()

    def test_a_detector_that_will_not_load_keeps_it_off(self, capsys):
        def broken():
            raise RuntimeError("native library failed")

        tracker = HandTracker(0, hands_factory=broken, camera_factory=lambda index: FakeCamera())
        assert tracker.start() is False
        assert "✋" in capsys.readouterr().out

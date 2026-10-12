"""The tracker searches fewer frames while there are no hands, and one hand is enough for gesture snapping."""
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.gestures import hand_tracking as ht
from jarvis.gestures.hand_tracking import HandTracker, IdleGate
from jarvis.gestures.window_snap_service import snap_tracker
from tests.test_hand_tracking import Collector, FakeCamera, _hand_for


class TestIdleGate:
    def _gate(self, fps=5.0, after=3.0):
        return IdleGate(idle_fps=fps, idle_after=after)

    def test_every_frame_is_searched_until_no_hand_has_been_seen_for_the_wait(self):
        gate = self._gate()
        t = 0.0
        while t < 3.0:
            assert gate.should_search(t) and not gate.idle
            gate.searched(t, found=False)
            t += 1 / 30

    def test_then_it_searches_no_more_often_than_the_idle_rate(self):
        gate = self._gate(fps=5.0, after=1.0)
        searched = 0
        t = 0.0
        while t < 11.0:
            if gate.should_search(t):
                gate.searched(t, found=False)
                if t >= 1.0:
                    searched += 1
            t += 1 / 30
        assert gate.idle
        assert 38 <= searched <= 51, "no more than 5 a second over the 10 idle seconds, and not far fewer"

    def test_a_hand_found_while_idle_brings_back_every_frame_at_once(self):
        gate = self._gate(fps=5.0, after=1.0)
        t = 0.0
        while t < 5.0:
            if gate.should_search(t):
                gate.searched(t, found=False)
            t += 1 / 30
        assert gate.idle
        while not gate.should_search(t):
            t += 1 / 30
        gate.searched(t, found=True)
        assert not gate.idle
        for i in range(1, 30):
            assert gate.should_search(t + i / 30), "no frame is skipped once a hand is back"
            gate.searched(t + i / 30, found=True)

    def test_a_hand_seen_now_and_then_keeps_it_from_idling(self):
        gate = self._gate(after=3.0)
        t = 0.0
        while t < 30.0:
            assert gate.should_search(t)
            gate.searched(t, found=int(t * 30) % 60 == 0)  # a hand for one frame every two seconds
            assert not gate.idle
            t += 1 / 30

    def test_it_waits_from_the_start_when_no_hand_was_ever_seen(self):
        gate = self._gate(after=2.0)
        assert gate.should_search(100.0) and not gate.idle
        gate.searched(100.0, found=False)
        assert gate.should_search(101.9)
        gate.searched(101.9, found=False)
        assert not gate.idle
        gate.should_search(102.0)
        assert gate.idle


class _Hands:
    """Sees one hand only while ``present`` is set; counts what it is asked to search."""

    def __init__(self):
        self.present = threading.Event()
        self.searched = 0

    def __call__(self, frame, timestamp):
        self.searched += 1
        return (_hand_for(frame),) if self.present.is_set() else ()


def _idle_tracker(hands, **kwargs):
    camera = FakeCamera()
    tracker = HandTracker(0, hands_factory=lambda: hands, camera_factory=lambda index: camera,
                          max_fps=200, **kwargs)
    return tracker, camera


@pytest.mark.unit
class TestTrackerIdling:
    def test_without_hands_most_frames_stop_being_searched_once_idle(self):
        hands = _Hands()
        tracker, camera = _idle_tracker(hands, idle_fps=5, idle_after=0.1)
        tracker.start()
        try:
            time.sleep(1.2)
            assert tracker.idle
        finally:
            tracker.stop()
        assert camera.n > 40
        assert hands.searched < camera.n * 0.35, "searched only a share of the frames the camera delivered"

    def test_a_hand_that_appears_while_idle_is_delivered_and_every_frame_is_searched_again(self):
        hands = _Hands()
        tracker, camera = _idle_tracker(hands, idle_fps=5, idle_after=0.1)
        got = Collector()
        tracker.subscribe(got)
        tracker.start()
        try:
            time.sleep(0.6)
            assert tracker.idle
            hands.present.set()
            deadline = time.perf_counter() + 2.0
            while not any(f.hands for f in got.frames) and time.perf_counter() < deadline:
                time.sleep(0.01)
            assert any(f.hands for f in got.frames), "the hand is noticed"
            before = len(got.frames)
            time.sleep(0.3)
            assert not tracker.idle
            assert len(got.frames) - before > 8, "full rate again (idling would give one or two in 0.3 s)"
        finally:
            tracker.stop()

    def test_the_default_searches_every_frame_without_hands_too(self):
        hands = _Hands()
        tracker, camera = _idle_tracker(hands)
        tracker.start()
        try:
            time.sleep(0.6)
            assert not tracker.idle
        finally:
            tracker.stop()
        assert hands.searched >= camera.n * 0.7


@pytest.mark.unit
class TestHowManyHands:
    def test_the_default_detector_is_built_for_the_hands_asked_for(self, monkeypatch):
        built = []

        class Detector:
            def __init__(self, model_path, max_hands=2):
                built.append(max_hands)

            def __call__(self, frame, timestamp):
                return ()

            def close(self):
                pass

        monkeypatch.setattr(ht, "MediaPipeHands", Detector)
        monkeypatch.setattr(ht, "ensure_hand_model", lambda fetch=None: "model.task")
        monkeypatch.setitem(__import__("sys").modules, "mediapipe", SimpleNamespace())
        for asked, expected in ((None, 2), (1, 1), (2, 2)):
            kwargs = {} if asked is None else {"max_hands": asked}
            tracker = HandTracker(0, camera_factory=lambda index: FakeCamera(), **kwargs)
            assert tracker.start() is True
            tracker.stop()
            assert built[-1] == expected


def _cfg(hand="any"):
    return SimpleNamespace(gesture_snap_hand=hand)


@pytest.mark.unit
class TestSnapTracker:
    def test_one_hand_is_enough_when_any_hand_may_drive(self):
        tracker = snap_tracker(_cfg("any"), 0)
        assert tracker.max_hands == 1

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_a_named_side_needs_both_hands_to_be_seen(self, side):
        assert snap_tracker(_cfg(side), 0).max_hands == 2

    def test_a_setting_that_is_not_a_side_counts_as_any(self):
        assert snap_tracker(_cfg("sideways"), 0).max_hands == 1

    def test_it_idles_while_there_are_no_hands(self):
        tracker = snap_tracker(_cfg(), 0)
        assert tracker.idle_fps == ht.IDLE_FPS

    def test_the_camera_index_and_test_doubles_pass_through(self):
        camera = FakeCamera()
        tracker = snap_tracker(_cfg(), 3, hands_factory=lambda: (lambda frame, t: ()),
                               camera_factory=lambda index: camera)
        assert tracker.start() is True
        tracker.stop()
        assert camera.opened >= 1

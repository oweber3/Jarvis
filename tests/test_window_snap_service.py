"""WindowSnapService: the camera runs only while the mode is on, and stopping always lets go of a held window."""
import time

import pytest

from jarvis.gestures.window_snap_service import WindowSnapService, WindowSnapUnavailable
from tests.synthetic_hands import FakeDesktop, FakeWindow, frame, hand


class FakeTracker:
    """Stands in for HandTracker: ``deliver`` pushes a frame to the subscribers as the detection thread would."""

    instances = []

    def __init__(self, camera=0, *, works=True):
        self.camera = camera
        self.works = works
        self.running = False
        self.subscribers = []
        self.stopped = 0
        FakeTracker.instances.append(self)

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback) if callback in self.subscribers else None

    def start(self):
        self.running = self.works
        return self.works

    def stop(self):
        self.running = False
        self.stopped += 1

    def deliver(self, hand_frame):
        for callback in list(self.subscribers):
            callback(hand_frame)


@pytest.fixture
def desk():
    return FakeDesktop(windows=[FakeWindow((0, 0, 1920, 1080))])


def service(mock_config, desk, *, works=True):
    FakeTracker.instances = []
    return WindowSnapService(mock_config, output_factory=lambda: desk,
                             tracker_factory=lambda camera: FakeTracker(camera, works=works))


def grab_and_hold(tracker):
    t = time.perf_counter()
    tracker.deliver(frame(t, hand(0.5, 0.5)))
    for i in range(1, 6):
        tracker.deliver(frame(t + i / 30, hand(0.5, 0.5, gesture="fist")))


def test_starting_turns_the_camera_on_and_hands_move_the_pointer(mock_config, desk):
    mock_config.gesture_snap_camera = 1
    svc = service(mock_config, desk)
    assert svc.start() == "started" and svc.running
    tracker = FakeTracker.instances[-1]
    assert tracker.running and tracker.camera == 1
    t = time.perf_counter()
    for i in range(10):
        tracker.deliver(frame(t + i / 30, hand(0.5 + i * 0.01, 0.5)))
    assert desk.of("pointer")
    svc.stop()


def test_starting_twice_keeps_one_camera(mock_config, desk):
    svc = service(mock_config, desk)
    svc.start()
    assert svc.start() == "already"
    assert len(FakeTracker.instances) == 1
    svc.stop()


def test_stopping_lets_go_of_a_held_window_and_releases_the_camera(mock_config, desk):
    svc = service(mock_config, desk)
    svc.start()
    tracker = FakeTracker.instances[-1]
    grab_and_hold(tracker)
    assert svc.state == "holding"
    assert svc.stop() is True
    assert not tracker.running and not svc.running and svc.state == "off"
    count = len(desk.events)
    tracker.deliver(frame(time.perf_counter() + 2, hand(0.7, 0.5, gesture="fist")))  # a late frame changes nothing
    assert len(desk.events) == count and desk.of("drop") == []


def test_stopping_when_off_is_harmless(mock_config, desk):
    svc = service(mock_config, desk)
    assert svc.stop() is False
    assert desk.events == []


def test_hand_tracking_that_cannot_start_leaves_nothing_running(mock_config, desk):
    svc = service(mock_config, desk, works=False)
    with pytest.raises(WindowSnapUnavailable):
        svc.start()
    assert not svc.running and FakeTracker.instances[-1].subscribers == []


def test_a_tracker_that_raises_on_start_leaves_nothing_running(mock_config, desk):
    class Exploding(FakeTracker):
        def start(self):
            raise RuntimeError("boom")
    FakeTracker.instances = []
    svc = WindowSnapService(mock_config, output_factory=lambda: desk, tracker_factory=lambda c: Exploding(c))
    with pytest.raises(WindowSnapUnavailable):
        svc.start()
    assert not svc.running and FakeTracker.instances[-1].subscribers == []


def test_displays_that_cannot_be_read_never_start_the_camera(mock_config):
    def broken():
        raise OSError("no displays")
    FakeTracker.instances = []
    svc = WindowSnapService(mock_config, output_factory=broken, tracker_factory=lambda camera: FakeTracker(camera))
    with pytest.raises(WindowSnapUnavailable):
        svc.start()
    assert FakeTracker.instances == [] and not svc.running


def test_the_watchdog_lets_go_when_frames_stop_arriving(mock_config, desk):
    svc = service(mock_config, desk)
    svc.start()
    grab_and_hold(FakeTracker.instances[-1])
    assert svc.state == "holding"
    deadline = time.monotonic() + 3
    while svc.state == "holding" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert svc.state == "paused"
    svc.stop()


def test_it_can_be_started_again_after_stopping(mock_config, desk):
    svc = service(mock_config, desk)
    svc.start()
    svc.stop()
    assert svc.start() == "started" and FakeTracker.instances[-1].running
    svc.stop()

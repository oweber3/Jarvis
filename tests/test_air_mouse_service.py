"""AirMouseService: the camera runs only while the mode is on, and stopping always lets go of the button."""
import time

import pytest

from jarvis.gestures.air_mouse_service import AirMouseService, AirMouseUnavailable
from tests.synthetic_hands import FakePointer, frame, hand


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
def pointer():
    return FakePointer()


def service(mock_config, pointer, *, works=True):
    FakeTracker.instances = []
    return AirMouseService(mock_config, output_factory=lambda: pointer,
                           tracker_factory=lambda camera: FakeTracker(camera, works=works))


def press_and_hold(tracker):
    t = time.perf_counter()
    tracker.deliver(frame(t, hand(0.5, 0.5)))
    for i in range(1, 25):
        tracker.deliver(frame(t + i / 30, hand(0.5, 0.5, pinch=0.1)))


def test_starting_turns_the_camera_on_and_hands_move_the_pointer(mock_config, pointer):
    mock_config.air_mouse_camera = 1
    svc = service(mock_config, pointer)
    assert svc.start() == "started" and svc.running
    tracker = FakeTracker.instances[-1]
    assert tracker.running and tracker.camera == 1
    t = time.perf_counter()
    for i in range(10):
        tracker.deliver(frame(t + i / 30, hand(0.5 + i * 0.01, 0.5)))
    assert pointer.moves()
    svc.stop()


def test_starting_twice_keeps_one_camera(mock_config, pointer):
    svc = service(mock_config, pointer)
    svc.start()
    assert svc.start() == "already"
    assert len(FakeTracker.instances) == 1
    svc.stop()


def test_stopping_releases_a_held_button_and_the_camera(mock_config, pointer):
    svc = service(mock_config, pointer)
    svc.start()
    tracker = FakeTracker.instances[-1]
    press_and_hold(tracker)
    assert pointer.down
    assert svc.stop() is True
    assert not pointer.down and not tracker.running and not svc.running
    tracker.deliver(frame(time.perf_counter() + 2, hand(0.7, 0.5)))  # a late frame changes nothing
    assert not pointer.down and pointer.events[-1][0] == "release"


def test_stopping_when_off_is_harmless(mock_config, pointer):
    svc = service(mock_config, pointer)
    assert svc.stop() is False
    assert pointer.events == []


def test_hand_tracking_that_cannot_start_leaves_nothing_running(mock_config, pointer):
    svc = service(mock_config, pointer, works=False)
    with pytest.raises(AirMouseUnavailable):
        svc.start()
    assert not svc.running and FakeTracker.instances[-1].subscribers == []


def test_displays_that_cannot_be_read_never_start_the_camera(mock_config):
    def broken():
        raise OSError("no displays")
    FakeTracker.instances = []
    svc = AirMouseService(mock_config, output_factory=broken, tracker_factory=lambda camera: FakeTracker(camera))
    with pytest.raises(AirMouseUnavailable):
        svc.start()
    assert FakeTracker.instances == [] and not svc.running


def test_the_watchdog_lets_go_when_frames_stop_arriving(mock_config, pointer):
    svc = service(mock_config, pointer)
    svc.start()
    press_and_hold(FakeTracker.instances[-1])
    assert pointer.down
    deadline = time.monotonic() + 3
    while pointer.down and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pointer.down
    svc.stop()


def test_it_can_be_started_again_after_stopping(mock_config, pointer):
    svc = service(mock_config, pointer)
    svc.start()
    svc.stop()
    assert svc.start() == "started" and FakeTracker.instances[-1].running
    svc.stop()

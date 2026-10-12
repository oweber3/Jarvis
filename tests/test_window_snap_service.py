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


def test_the_shared_flag_is_on_while_running_and_off_after_stopping(mock_config, desk, monkeypatch, tmp_path):
    from jarvis.gestures import snap_state
    monkeypatch.setattr(snap_state, "_channel", snap_state.SnapStateChannel(str(tmp_path / "flag")))
    svc = service(mock_config, desk)
    assert not snap_state.is_on()
    svc.start()
    assert snap_state.is_on()
    time.sleep(1.3)  # the watchdog keeps the heartbeat fresh
    assert snap_state.is_on()
    svc.stop()
    assert not snap_state.is_on()


def test_no_thread_of_the_mode_is_left_running_once_it_is_off(mock_config, desk):
    import threading

    from jarvis.gestures.window_snap_service import snap_tracker
    from tests.test_hand_tracking import FakeCamera, FakeHands

    names = ("hand-capture", "hand-detect", "gesture-snap-watchdog")

    def alive():
        return {t for t in threading.enumerate() if t.name in names}

    before = alive()
    svc = WindowSnapService(mock_config, output_factory=lambda: desk,
                            tracker_factory=lambda camera: snap_tracker(
                                mock_config, camera, hands_factory=lambda: FakeHands(),
                                camera_factory=lambda index: FakeCamera()))
    assert svc.start() == "started"
    deadline = time.perf_counter() + 2.0
    while len({t.name for t in alive() - before}) < 3 and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert {t.name for t in alive() - before} == set(names), "the camera, the detector and the watchdog run while on"
    svc.stop()
    assert alive() - before == set(), "nothing keeps polling once it is off"


def test_a_daemon_restarted_in_the_same_process_uses_the_settings_saved_before_the_restart(monkeypatch):
    """The installed app runs the daemon in a thread; Restart runs ``daemon.main`` again in the same process. The
    shutdown releases the camera and leaves nothing running, and the next start builds gesture snapping from the
    new settings (here the camera)."""
    import threading
    from types import SimpleNamespace

    import jarvis.platform.windows.window_snap as windows_output
    from jarvis.gestures import window_snap_service
    from jarvis.tools import registry

    trackers = []

    def fake_snap_tracker(cfg, camera, **kwargs):
        trackers.append(FakeTracker(camera))
        return trackers[-1]

    class OneMonitor:
        def __init__(self, *args, **kwargs):
            pass

        def desktop(self):
            return [(0, 0, 1920, 1080)]

    monkeypatch.setattr(windows_output, "WindowsSnapOutput", OneMonitor)
    monkeypatch.setattr(window_snap_service, "snap_tracker", fake_snap_tracker)
    monkeypatch.setattr(window_snap_service, "_service", None)
    monkeypatch.setattr(registry, "BUILTIN_TOOLS", dict(registry.BUILTIN_TOOLS))

    registry.configure_gesture_tool(SimpleNamespace(gesture_snap_enabled=True, gesture_snap_camera=0),
                                    platform="win32")
    assert window_snap_service.get_service().start() == "started"
    window_snap_service.stop_service()  # daemon shutdown
    assert trackers[0].running is False and trackers[0].stopped == 1, "the camera is released at shutdown"
    assert window_snap_service.get_service() is None
    assert not any(t.name == "gesture-snap-watchdog" for t in threading.enumerate())

    registry.configure_gesture_tool(SimpleNamespace(gesture_snap_enabled=True, gesture_snap_camera=2),
                                    platform="win32")
    service = window_snap_service.get_service()
    try:
        assert service.start() == "started"
        assert trackers[-1].camera == 2
    finally:
        window_snap_service.stop_service()


def _wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    return condition()


def _show(tracker, seconds, *hands):
    t = time.perf_counter()
    for i in range(int(seconds * 30)):
        tracker.deliver(frame(t + i / 30, *hands))


def test_a_raised_open_hand_calls_the_talk_handler(mock_config, desk, monkeypatch):
    from jarvis.gestures import window_snap_service
    asked = []
    monkeypatch.setattr(window_snap_service, "_talk_handler", None)
    window_snap_service.set_talk_handler(lambda: asked.append("listen"))
    svc = service(mock_config, desk)
    svc.start()
    _show(FakeTracker.instances[-1], 1.3, hand(0.5, 0.5, gesture="open"))
    assert _wait_for(lambda: asked == ["listen"])
    svc.stop()


def test_a_raised_hand_without_a_talk_handler_says_so_once(mock_config, desk, monkeypatch, capsys):
    from jarvis.gestures import window_snap_service
    monkeypatch.setattr(window_snap_service, "_talk_handler", None)
    svc = service(mock_config, desk)
    svc.start()
    tracker = FakeTracker.instances[-1]
    for _ in range(2):
        _show(tracker, 1.3, hand(0.5, 0.5, gesture="open"))
        _show(tracker, 0.3, hand(0.5, 0.5))
    time.sleep(0.2)
    svc.stop()
    assert capsys.readouterr().out.count("Listening by raising a hand") == 1


def test_crossed_wrists_turn_the_mode_off_and_release_the_camera(mock_config, desk):
    from tests.test_window_snap_hands import crossed
    svc = service(mock_config, desk)
    svc.start()
    tracker = FakeTracker.instances[-1]
    _show(tracker, 0.9, *crossed())
    assert _wait_for(lambda: not svc.running and not tracker.running)


def test_a_click_reaches_the_pointer_only_while_the_mode_is_on(mock_config, desk):
    svc = service(mock_config, desk)
    assert svc.click("click") == "off"
    svc.start()
    assert svc.click("double_click") == "clicked"
    assert desk.of("click") == [("click", "double_click", desk.at)]
    svc.stop()
    assert svc.click("click") == "off"

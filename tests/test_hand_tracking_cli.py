"""``python -m jarvis.gestures``: the live readout and recording, against a fake camera and detector."""
import numpy as np
import pytest

from jarvis.gestures import __main__ as cli
from jarvis.gestures.hand_tracking import Hand, HandTracker, Point
from jarvis.gestures.recording import read_recording


class _Camera:
    def open(self):
        return True

    def read(self):
        return np.zeros((4, 6, 3), dtype=np.uint8)

    def close(self):
        pass


def _right_hand(frame, timestamp):
    return (Hand(side="right", score=0.96,
                 points=tuple(Point(0.25, 0.75, 0.0) for _ in range(21)),
                 world=tuple(Point(0.0, 0.0, 0.0) for _ in range(21))),)


def _fake_tracker(camera_index):
    return HandTracker(camera_index, hands_factory=lambda: _right_hand,
                       camera_factory=lambda index: _Camera(), max_fps=50)


@pytest.mark.unit
class TestLive:
    def test_live_mode_reports_hands_and_frame_rate_then_stops(self, capsys):
        assert cli.main(["--camera", "2", "--seconds", "1.2"], make_tracker=_fake_tracker) == 0
        out = capsys.readouterr().out
        assert "camera 2" in out
        assert "right hand appeared" in out
        assert "fps" in out and "0.25" in out and "0.75" in out
        assert "Stopped" in out

    def test_live_mode_can_record_what_it_saw(self, tmp_path, capsys):
        path = tmp_path / "session.jsonl"
        assert cli.main(["--seconds", "0.6", "--record", str(path)], make_tracker=_fake_tracker) == 0
        frames = list(read_recording(path))
        assert len(frames) > 5
        assert all(f.hands[0].side == "right" for f in frames)
        assert str(path) in capsys.readouterr().out

    def test_a_tracker_that_cannot_start_exits_with_an_error(self, capsys):
        def no_mediapipe():
            raise ImportError("No module named 'mediapipe'")

        def broken(camera_index):
            return HandTracker(camera_index, hands_factory=no_mediapipe, camera_factory=lambda index: _Camera())

        assert cli.main(["--seconds", "0.2"], make_tracker=broken) == 1
        assert "mediapipe" in capsys.readouterr().out.lower()


def _pinching_tracker(camera_index):
    from tests.synthetic_hands import hand
    tracker = HandTracker(camera_index, hands_factory=lambda: lambda frame, t: (hand(0.5, 0.5, pinch=0.1),),
                          camera_factory=lambda index: _Camera(), max_fps=50)
    return tracker


@pytest.mark.unit
class TestAirMouse:
    def test_the_air_mouse_runs_reports_the_pinch_and_lets_go_when_it_stops(self, capsys):
        from tests.synthetic_hands import FakePointer
        pointer = FakePointer()
        made = []

        def tracker(camera_index):
            made.append(camera_index)
            return _pinching_tracker(camera_index)

        assert cli.main(["--air-mouse", "--camera", "3", "--seconds", "1.3"], make_tracker=tracker,
                        make_pointer=lambda: pointer) == 0
        out = capsys.readouterr().out
        assert made == [3]
        assert "Air mouse on" in out and "pinch 0.1" in out and "Air mouse off" in out
        assert pointer.buttons() and not pointer.down  # the held pinch pressed, stopping released

    def test_an_air_mouse_that_cannot_start_exits_with_an_error(self, capsys):
        from tests.synthetic_hands import FakePointer

        def no_mediapipe():
            raise ImportError("No module named 'mediapipe'")

        def broken(camera_index):
            return HandTracker(camera_index, hands_factory=no_mediapipe, camera_factory=lambda index: _Camera())

        assert cli.main(["--air-mouse", "--seconds", "0.2"], make_tracker=broken, make_pointer=FakePointer) == 1
        assert "could not start" in capsys.readouterr().out

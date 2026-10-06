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

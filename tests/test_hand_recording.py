"""Hand landmark recordings: landmarks only, written as JSON Lines and read back unchanged."""
import json

import pytest

from jarvis.gestures.hand_tracking import Hand, HandFrame, Point
from jarvis.gestures.recording import HandRecorder, read_recording


def _hand(side, base):
    points = tuple(Point(round(base + i / 100, 5), round(0.5 - i / 200, 5), round(-i / 1000, 5)) for i in range(21))
    world = tuple(Point(round(i / 1000, 5), round(-i / 2000, 5), round(i / 4000, 5)) for i in range(21))
    return Hand(side=side, score=0.97, points=points, world=world)


FRAMES = [
    HandFrame(time=100.0, width=640, height=480, hands=()),
    HandFrame(time=100.04, width=640, height=480, hands=(_hand("right", 0.3),)),
    HandFrame(time=100.08, width=640, height=480, hands=(_hand("left", 0.1), _hand("right", 0.6))),
]


def _record(path, frames=FRAMES):
    with HandRecorder(path) as recorder:
        for frame in frames:
            recorder(frame)


@pytest.mark.unit
class TestRecordings:
    def test_frames_read_back_unchanged_with_times_from_the_first_frame(self, tmp_path):
        path = tmp_path / "hands.jsonl"
        _record(path)
        back = list(read_recording(path))
        assert [f.hands for f in back] == [f.hands for f in FRAMES]
        assert [(f.width, f.height) for f in back] == [(640, 480)] * 3
        assert [round(f.time, 6) for f in back] == [0.0, 0.04, 0.08]

    def test_a_recording_holds_only_landmarks(self, tmp_path):
        path = tmp_path / "hands.jsonl"
        _record(path)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert lines[0] == {"format": "jarvis-hand-frames", "version": 1}
        for line in lines[1:]:
            assert set(line) == {"t", "w", "h", "hands"}
            for hand in line["hands"]:
                assert set(hand) == {"side", "score", "points", "world"}

    def test_coordinates_are_rounded_so_files_stay_small(self, tmp_path):
        frame = HandFrame(time=1.0, width=640, height=480, hands=(Hand(
            side="right", score=0.987654321,
            points=tuple(Point(0.123456789, 0.5, 0.0) for _ in range(21)),
            world=tuple(Point(0.0, 0.0, 0.0) for _ in range(21))),))
        path = tmp_path / "hands.jsonl"
        _record(path, [frame])
        hand = next(read_recording(path)).hands[0]
        assert hand.points[0].x == 0.12346

    def test_a_file_that_is_not_a_recording_is_refused(self, tmp_path):
        path = tmp_path / "other.jsonl"
        path.write_text('{"t": 0, "w": 1, "h": 1, "hands": []}\n', encoding="utf-8")
        with pytest.raises(ValueError):
            list(read_recording(path))

    def test_a_recording_from_a_newer_version_is_refused(self, tmp_path):
        path = tmp_path / "newer.jsonl"
        path.write_text('{"format": "jarvis-hand-frames", "version": 2}\n', encoding="utf-8")
        with pytest.raises(ValueError):
            list(read_recording(path))

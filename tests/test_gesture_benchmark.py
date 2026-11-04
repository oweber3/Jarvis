"""Gesture snapping on real hands: landmarks of thousands of people's hands from HaGRID photos.

The fixture holds landmarks only, and its people are split in two so thresholds tuned on one half are checked on
people they never saw. See ``tests/fixtures/hand_landmarks/README.md``."""
import gzip
import json
from collections import Counter

from tests.hand_benchmark import EXPECTED, FIXTURE, GESTURE_LABELS, benchmark, load_benchmark, series


# --- the fixture -----------------------------------------------------------------------------------

def test_the_fixture_holds_landmarks_only():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as f:
        header = json.loads(f.readline())
        rows = [json.loads(line) for line in f]
    assert header["format"] == "jarvis-hand-benchmark"
    allowed = {"id", "user", "split", "label", "extra", "side", "score", "points", "world", "recogniser"}
    for row in rows:
        assert set(row) <= allowed
        assert len(row["points"]) == 21 and len(row["world"]) == 21
        assert all(len(p) == 3 and all(isinstance(v, (int, float)) for v in p) for p in row["points"] + row["world"])


def test_the_benchmark_covers_every_gesture_with_many_people():
    hands = benchmark()
    counts = Counter(s.label for s in hands)
    for label in GESTURE_LABELS:
        assert counts[label] >= 600, label
        assert len({s.user for s in hands if s.label == label}) == counts[label]  # one photo per person
    assert counts["no_gesture"] >= 300
    assert set(EXPECTED) == set(GESTURE_LABELS)


def test_tuning_and_held_out_people_never_overlap():
    hands = load_benchmark()
    tune = {s.user for s in hands if s.split == "tune"}
    test = {s.user for s in hands if s.split == "test"}
    assert tune and test and not tune & test
    assert 0.4 < len(test) / (len(tune) + len(test)) < 0.6


def test_replayable_people_have_several_photos_of_each_gesture():
    people = series()
    assert len(people) >= 60
    for gestures in people.values():
        assert len(gestures["fist"]) >= 2 and len(gestures["one"]) >= 2 and len(gestures["open"]) >= 2

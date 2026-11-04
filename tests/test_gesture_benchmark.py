"""Gesture snapping on real hands: landmarks of thousands of people's hands from HaGRID photos.

The fixture holds landmarks only, and its people are split in two so thresholds tuned on one half are checked on
people they never saw. See ``tests/fixtures/hand_landmarks/README.md``."""
import gzip
import json
from collections import Counter

import pytest

from jarvis.gestures.window_snap import FIST, FingerReader, WindowSnapSettings, read_shape
from tests.hand_benchmark import EXPECTED, FIXTURE, GESTURE_LABELS, benchmark, load_benchmark, series
from tests.synthetic_hands import hand as synthetic_hand

SETTINGS = WindowSnapSettings()
pytestmark = pytest.mark.unit


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


# --- reading one frame -----------------------------------------------------------------------------
# Every reading uses the default thresholds on the held-out people only: the thresholds were tuned on the others.

def held_out(*labels):
    return [s.hand for s in benchmark("test") if s.label in labels]


def reader_after(gesture):
    """A reader that has just seen a clear ``gesture`` (one or fist), as on the frame before."""
    reader = FingerReader(SETTINGS)
    reader.read(synthetic_hand(gesture=gesture))
    return reader


def lets_go_after_a_fist(hand):
    reader = reader_after("fist")
    reader.read(hand)
    return reader.lets_go


def share(hands, test):
    return sum(1 for h in hands if test(h)) / len(hands)


def test_on_held_out_people_almost_every_gesture_reads_as_asked():
    hands = [s for s in benchmark("test") if s.label in GESTURE_LABELS]
    assert share(hands, lambda s: read_shape(s.hand, SETTINGS) == EXPECTED[s.label]) >= 0.98


def test_on_held_out_people_a_fist_is_read_as_a_fist_and_other_gestures_are_not():
    fists, others = held_out("fist"), held_out("one", "peace", "palm", "stop")
    read_as_fist = [h for h in fists + others if read_shape(h, SETTINGS) == FIST]
    precision = sum(1 for h in read_as_fist if h in fists) / len(read_as_fist)
    assert precision >= 0.995
    assert share(fists, lambda h: read_shape(h, SETTINGS) == FIST) >= 0.965


def test_a_relaxed_hand_coming_into_view_is_seldom_taken_for_a_fist():
    # A hand nobody asked to make a gesture (resting, half curled) appearing in the view must not grab.
    assert share(held_out("no_gesture"), lambda h: read_shape(h, SETTINGS) == FIST) <= 0.15


def test_closing_a_pointing_hand_reads_as_a_fist():
    assert share(held_out("fist"), lambda h: reader_after("one").read(h) == FIST) >= 0.965


def test_a_held_fist_is_seldom_read_as_letting_go():
    assert share(held_out("fist"), lets_go_after_a_fist) <= 0.012


def test_opening_a_held_fist_reads_as_letting_go():
    assert share(held_out("one", "peace", "palm", "stop"), lets_go_after_a_fist) >= 0.995

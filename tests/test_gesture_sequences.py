"""Grabbing and letting go over time, replayed from real hands: each person's own HaGRID hands pointing, closing
into a fist, holding it with jitter, misread frames and detector dropouts, and opening again (see
``tests/hand_sequences.py``). Every scenario runs for every replayable person with fixed seeds.

The timings were tuned on the benchmark's ``tune`` people; the ``test`` people check them on hands they never saw."""
import random
import statistics
import zlib

import pytest

from tests.hand_sequences import Script, loose_fists, people, replay

PEOPLE = people()
LOOSE = loose_fists()
SEEDS = (1, 2)


def scripts(name, build, fps=18.0, split=None):
    for person in PEOPLE:
        if split and person.split != split:
            continue
        for seed in SEEDS:
            script = Script(random.Random(zlib.crc32(f"{name}|{person.user}|{seed}|{fps}".encode())), fps=fps)
            build(script, person)
            yield script, replay(script)


def close_and_hold(misreads):
    def build(s, p):
        s.hold(p.one, 1.0).mark("closing").change(p.one, p.fist)
        s.hold(p.fist, 2.5, misreads=LOOSE, misread_rate=misreads, burst=2, dropout_rate=0.05)
    return build


def hold_then_open(misreads, burst, seconds_open=1.5):
    def build(s, p):
        s.hold(p.one, 1.0).change(p.one, p.fist).hold(p.fist, 1.0)
        s.hold(p.fist, 3.0, misreads=misreads(p), misread_rate=0.1, burst=burst, gap=4, dropout_rate=0.05)
        s.mark("opening").change(p.fist, p.open)
        s.hold(p.open + p.one + (p.peace or p.one), seconds_open, misreads=p.fist, misread_rate=0.1, burst=1,
               gap=2, dropout_rate=0.05)
    return build


def grabbed_once(s, r):
    return len(r.grabs) == 1 and r.grabs[0] > s.marks["closing"]


def let_go_once_after_opening(s, r):
    return len(r.grabs) == 1 and len(r.drops) == 1 and r.drops[0] >= s.marks["opening"]


def test_there_are_enough_people_to_replay():
    assert len(PEOPLE) >= 60
    assert {p.split for p in PEOPLE} == {"tune", "test"}


# --- grabbing --------------------------------------------------------------------------------------

@pytest.mark.parametrize("fps", [12.0, 18.0, 30.0])
def test_closing_a_pointing_hand_grabs_once_and_promptly(fps):
    runs = list(scripts("grab", close_and_hold(0.1), fps))
    ok = [r.grabs[0] - s.marks["closing"] for s, r in runs if grabbed_once(s, r)]
    assert len(ok) >= 0.98 * len(runs)
    assert statistics.median(ok) <= 0.6


@pytest.mark.parametrize("fps", [12.0, 18.0, 30.0])
def test_a_fist_misread_a_quarter_of_the_time_still_grabs(fps):
    runs = list(scripts("grab through misreads", close_and_hold(0.25), fps))
    assert sum(1 for s, r in runs if grabbed_once(s, r)) >= 0.95 * len(runs)


@pytest.mark.parametrize("fps", [12.0, 18.0, 30.0])
def test_a_pointing_hand_misread_as_a_fist_for_a_few_frames_never_grabs(fps):
    # Measured on a real webcam: a moving pointing hand reads as folded for 1 to 3 frames at a time.
    def build(s, p):
        s.hold(p.one, 6.0, misreads=p.fist, misread_rate=0.25, burst=3, gap=3, dropout_rate=0.03,
               drift=(0.02, 0.0))
    assert all(r.grabs == [] for _, r in scripts("pointing", build, fps))


# --- holding and letting go ------------------------------------------------------------------------

@pytest.mark.parametrize("fps", [12.0, 18.0, 30.0])
def test_a_single_misread_frame_never_lets_go(fps):
    runs = list(scripts("one bad frame", hold_then_open(lambda p: p.open + p.one, burst=1), fps))
    assert all(not [d for d in r.drops if d < s.marks["opening"]] for s, r in runs)


@pytest.mark.parametrize("fps", [18.0, 30.0])
def test_a_held_window_survives_misreads_of_up_to_three_frames(fps):
    for pool in (lambda p: p.open + p.one, lambda p: LOOSE):
        runs = list(scripts("bursts", hold_then_open(pool, burst=3), fps))
        assert sum(1 for s, r in runs if not [d for d in r.drops if d < s.marks["opening"]]) >= 0.98 * len(runs)


@pytest.mark.parametrize("fps", [12.0, 15.0, 18.0])
def test_a_held_fist_the_detector_misses_now_and_then_keeps_holding(fps):
    # On the benchmark the detector found no hand in 7% of fist photos, against 0.4% of open palms.
    def build(s, p):
        s.hold(p.one, 1.0).change(p.one, p.fist).hold(p.fist, 1.0).mark("held").hold(p.fist, 5.0, dropout_rate=0.07)
    runs = list(scripts("dropouts", build, fps))
    assert sum(1 for _, r in runs if len(r.grabs) == 1 and not r.drops and r.state == "holding") >= 0.98 * len(runs)


@pytest.mark.parametrize("fps", [12.0, 18.0, 30.0])
def test_opening_the_hand_lets_go_once_and_promptly(fps):
    # The opening hand flickers between one finger, two fingers and an open hand, with misread fists.
    runs = list(scripts("open", hold_then_open(lambda p: p.open + p.one, burst=1), fps))
    delays = [r.drops[0] - s.marks["opening"] for s, r in runs if let_go_once_after_opening(s, r)]
    assert len(delays) >= 0.98 * len(runs)
    assert statistics.median(delays) <= 0.5 and statistics.quantiles(delays, n=10)[-1] <= 0.9


# --- held-out people ------------------------------------------------------------------------------

def test_the_tuned_timings_hold_for_people_never_tuned_on():
    grabs = list(scripts("held-out grab", close_and_hold(0.1), split="test"))
    assert sum(1 for s, r in grabs if grabbed_once(s, r)) >= 0.97 * len(grabs)
    opens = list(scripts("held-out open", hold_then_open(lambda p: p.open + p.one, burst=2), split="test"))
    assert sum(1 for s, r in opens if let_go_once_after_opening(s, r)) >= 0.97 * len(opens)

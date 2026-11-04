"""Real hands for gesture tests: MediaPipe Hand Landmarker landmarks from HaGRID photos of many people.

``load_benchmark`` reads ``tests/fixtures/hand_landmarks/hagrid_landmarks.jsonl.gz`` (built by
``scripts/gesture_benchmark.py build``; source and licence in that folder's README) into ``Sample``s whose
``hand`` is a ``Hand`` exactly as the tracker delivers it. Landmarks only: the fixture holds no images."""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from jarvis.gestures.hand_tracking import Hand, Point

FIXTURE = Path(__file__).parent / "fixtures" / "hand_landmarks" / "hagrid_landmarks.jsonl.gz"
FORMAT = "jarvis-hand-benchmark"

# What gesture snapping should read for each gesture the person was asked to make. ``no_gesture`` hands are
# relaxed second hands with no gesture asked for, so they have no right answer.
EXPECTED = {"fist": "fist", "one": "one finger", "peace": "two fingers", "palm": "open hand", "stop": "open hand"}
GESTURE_LABELS = tuple(EXPECTED)


@dataclass(frozen=True)
class Sample:
    id: str
    user: int
    split: str  # "tune" or "test": half the people each, so tuning never sees the held-out people
    label: str
    hand: Hand
    recogniser: Optional[Tuple[str, float]]  # the MediaPipe Gesture Recognizer's reading, when recorded
    # One photo per person and gesture makes the benchmark; extra photos of the same people only serve the
    # replayed sequences (see ``series``).
    extra: bool = False


@lru_cache(maxsize=4)
def load_benchmark(path: str = str(FIXTURE)) -> Tuple[Sample, ...]:
    samples = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        header = json.loads(f.readline())
        if header.get("format") != FORMAT or header.get("version") != 1:
            raise ValueError("not a hand benchmark fixture")
        for line in f:
            row = json.loads(line)
            hand = Hand(side=row["side"], score=row["score"],
                        points=tuple(Point(*p) for p in row["points"]),
                        world=tuple(Point(*p) for p in row["world"]))
            recogniser = tuple(row["recogniser"]) if row.get("recogniser") else None
            samples.append(Sample(row["id"], row["user"], row["split"], row["label"], hand, recogniser,
                                  bool(row.get("extra"))))
    return tuple(samples)


def benchmark(split: str = "all", path: str = str(FIXTURE)) -> Tuple[Sample, ...]:
    """The benchmark proper: one photo per person and gesture, of ``split`` ("tune", "test" or "all")."""
    return tuple(s for s in load_benchmark(path) if not s.extra and split in ("all", s.split))


def series(path: str = str(FIXTURE)) -> Dict[int, Dict[str, Tuple[Hand, ...]]]:
    """Each replayable person's hands by gesture: everyone with at least two photos each of a fist, one finger and
    an open hand (palm and stop together as ``open``)."""
    people: Dict[int, Dict[str, list]] = {}
    for s in load_benchmark(path):
        if s.label == "no_gesture":
            continue
        people.setdefault(s.user, {}).setdefault("open" if s.label in ("palm", "stop") else s.label, []).append(s)
    return {user: {label: tuple(s.hand for s in found) for label, found in gestures.items()}
            for user, gestures in people.items()
            if all(len(gestures.get(label, ())) >= 2 for label in ("fist", "one", "open"))}

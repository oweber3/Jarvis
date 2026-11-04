"""Replayed hand sequences from real landmarks: a person's own hand pointing, closing into a fist, holding it with
natural jitter, misread frames and dropouts, and opening again, as a webcam would deliver them.

Every frame is one of the person's own HaGRID hands (``tests/hand_benchmark.series``) moved so its palm follows a
scripted path, with small random jitter on every point. Changes of gesture are blended over a few frames from one
real hand to another. Misread frames are real hands too: the person's own hand of another gesture, or another person's
real fist with a finger that does not read clearly folded. Dropouts are frames where the detector found no hand."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from jarvis.gestures.hand_tracking import Hand, HandFrame, Point
from jarvis.gestures.window_snap import (
    FINGERS, WindowSnap, WindowSnapSettings, finger_curl, palm_centre,
)
from tests.hand_benchmark import benchmark, load_benchmark, series
from tests.synthetic_hands import FakeDesktop, FakeWindow

FPS = 18.0  # measured on a real webcam with a hand in view: about 15 to 18 frames a second
CHANGE_SEC = 0.15  # how long a hand takes to close into a fist or open


def place(hand: Hand, x: float, y: float) -> Hand:
    """The same hand with its palm centre moved to image position ``(x, y)``."""
    px, py = palm_centre(hand)
    return Hand(hand.side, hand.score, tuple(Point(p.x + x - px, p.y + y - py, p.z) for p in hand.points),
                hand.world)


def blend(a: Hand, b: Hand, f: float) -> Hand:
    """Part way from hand ``a`` to hand ``b`` (``f`` 0 to 1), both at ``a``'s palm centre."""
    b = place(b, *palm_centre(a))

    def mix(p: Point, q: Point) -> Point:
        return Point(p.x + (q.x - p.x) * f, p.y + (q.y - p.y) * f, p.z + (q.z - p.z) * f)
    return Hand(a.side, a.score, tuple(mix(p, q) for p, q in zip(a.points, b.points)),
                tuple(mix(p, q) for p, q in zip(a.world, b.world)))


def jitter(hand: Hand, rng: random.Random, world: float = 0.0015, image: float = 0.002) -> Hand:
    """Frame-to-frame tracking noise: each point moved by a few millimetres (world) and pixels (image)."""
    return Hand(hand.side, hand.score,
                tuple(Point(p.x + rng.gauss(0, image), p.y + rng.gauss(0, image), p.z) for p in hand.points),
                tuple(Point(p.x + rng.gauss(0, world), p.y + rng.gauss(0, world), p.z + rng.gauss(0, world))
                      for p in hand.world))


def loose_fists() -> Tuple[Hand, ...]:
    """Real fists with a finger that does not read clearly folded: the hardest frames of a held fist."""
    return tuple(s.hand for s in benchmark() if s.label == "fist" and not clear(s.hand, "fist"))


@dataclass
class Person:
    """One person's own hands by gesture: fist, one, peace (may be empty) and open."""

    user: int
    split: str  # "tune" or "test", as in the benchmark
    fist: Tuple[Hand, ...]
    one: Tuple[Hand, ...]
    open: Tuple[Hand, ...]
    peace: Tuple[Hand, ...] = ()


# The fingers each gesture raises, and the curls a clearly made gesture shows whatever thresholds gesture
# snapping uses: raised fingers above CLEAR_UP, folded ones below CLEAR_DOWN.
_RAISES = {"fist": (), "one": ("INDEX",), "peace": ("INDEX", "MIDDLE"), "open": FINGERS}
CLEAR_UP, CLEAR_DOWN = 1.25, 0.95


def clear(hand: Hand, label: str) -> bool:
    """Whether ``hand`` clearly makes gesture ``label`` (fist, one, peace or open)."""
    return all((finger_curl(hand, f) > CLEAR_UP) if f in _RAISES[label] else (finger_curl(hand, f) < CLEAR_DOWN)
               for f in FINGERS)


def people() -> List[Person]:
    """Everyone whose own photos give at least two clear hands each of a fist, one finger and an open hand.
    HaGRID photos that do not clearly make their gesture (a "one" with the middle finger half up, say) are left
    out, so a sequence misreads only where a test puts misread frames."""
    found = []
    splits = {s.user: s.split for s in load_benchmark()}
    for user, gestures in sorted(series().items()):
        clean = {label: tuple(h for h in gestures.get(label, ()) if clear(h, label)) for label in _RAISES}
        if all(len(clean[label]) >= 2 for label in ("fist", "one", "open")):
            found.append(Person(user, splits[user], clean["fist"], clean["one"], clean["open"], clean["peace"]))
    return found


@dataclass
class Script:
    """A sequence of frames at ``fps``: each a hand or ``None`` (no hand found), with a note of when each phase
    began so tests can measure delays."""

    rng: random.Random
    fps: float = FPS
    frames: List[Optional[Hand]] = field(default_factory=list)
    marks: dict = field(default_factory=dict)
    x: float = 0.5
    y: float = 0.5

    @property
    def now(self) -> float:
        return len(self.frames) / self.fps

    def mark(self, name: str) -> "Script":
        self.marks[name] = self.now
        return self

    def _frame(self, hand: Optional[Hand]) -> None:
        self.frames.append(None if hand is None else jitter(place(hand, self.x, self.y), self.rng))

    def hold(self, hands: Sequence[Hand], seconds: float, *, misreads: Sequence[Hand] = (),
             misread_rate: float = 0.0, burst: int = 1, gap: int = 0, dropout_rate: float = 0.0,
             drift: Tuple[float, float] = (0.0, 0.0)) -> "Script":
        """Hold a gesture for ``seconds``, a random one of ``hands`` each frame. With ``misread_rate``, bursts of
        1 to ``burst`` frames from ``misreads`` replace it, at least ``gap`` good frames apart; with
        ``dropout_rate``, bursts of 1 or 2 frames have no hand. ``drift`` moves the palm (image units a second)."""
        count = int(round(seconds * self.fps))
        since_burst = gap
        i = 0
        while i < count:
            self.x += drift[0] / self.fps
            self.y += drift[1] / self.fps
            roll = self.rng.random()
            if misreads and since_burst >= gap and roll < misread_rate:
                for _ in range(min(self.rng.randint(1, burst), count - i)):
                    self._frame(self.rng.choice(misreads))
                    i += 1
                since_burst = 0
                continue
            if roll > 1 - dropout_rate:
                for _ in range(min(self.rng.randint(1, 2), count - i)):
                    self._frame(None)
                    i += 1
                continue
            self._frame(self.rng.choice(hands))
            since_burst += 1
            i += 1
        return self

    def change(self, start: Sequence[Hand], end: Sequence[Hand], seconds: float = CHANGE_SEC) -> "Script":
        """Change gesture over ``seconds``, blending from one real hand to another."""
        a, b = self.rng.choice(start), self.rng.choice(end)
        frames = max(1, int(round(seconds * self.fps)))
        for i in range(1, frames + 1):
            self._frame(blend(a, b, i / (frames + 1)))
        return self


@dataclass
class Replay:
    """What gesture snapping did with a script: grabs and drops with their times."""

    grabs: List[float]
    drops: List[float]
    state: str


def replay(script: Script, settings: Optional[WindowSnapSettings] = None,
           on_frame: Optional[Callable[[WindowSnap, float], None]] = None) -> Replay:
    """Feed the script to gesture snapping over a desktop filled by one window, on the frames' own clock."""
    desk = FakeDesktop(windows=[FakeWindow((0, 0, 1920, 1080))])
    clock = [0.0]
    snap = WindowSnap(desk, settings or WindowSnapSettings(), clock=lambda: clock[0])
    grabs, drops = [], []
    for i, hand in enumerate(script.frames):
        t = (i + 1) / script.fps
        clock[0] = t
        before = len(desk.events)
        snap.on_frame(HandFrame(t, 640, 480, () if hand is None else (hand,)))
        snap.tick()
        for event in desk.events[before:]:
            if event[0] == "grab" and event[1] is not None:
                grabs.append(t)
            elif event[0] == "drop":
                drops.append(t)
        if on_frame:
            on_frame(snap, t)
    return Replay(grabs, drops, snap.state)

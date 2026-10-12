"""Synthetic hands and a fake pointer for air mouse tests.

``hand`` builds a ``Hand`` whose index fingertip is at an image position and whose world landmarks give a
chosen pinch ratio and pose, so tests can describe gestures in the user's terms (point here, pinch, open
the palm) without a camera."""
from __future__ import annotations

from typing import List, Optional, Tuple

from jarvis.gestures.hand_tracking import Hand, HandFrame, Landmark, Point

HAND_SIZE = 0.09  # wrist to middle finger base, metres
_BASES = {"INDEX": (-0.02, -0.085), "MIDDLE": (0.0, -0.09), "RING": (0.02, -0.085), "PINKY": (0.04, -0.075)}


def _finger(base, extended: bool):
    x, y = base
    if extended:
        return [Point(x, y, 0.0), Point(x, y - 0.03, 0.0), Point(x, y - 0.05, 0.0), Point(x, y - 0.07, 0.0)]
    # Curled: the tip folds back towards the palm.
    return [Point(x, y, 0.0), Point(x, y - 0.03, -0.01), Point(x, y - 0.015, -0.03), Point(x, y + 0.005, -0.02)]


def hand(x: float = 0.5, y: float = 0.5, *, pinch: float = 1.0, palm: bool = False, side: str = "right",
         score: float = 0.9, depth_error: float = 0.0) -> Hand:
    """A hand with its index fingertip at image position ``(x, y)``.

    ``pinch`` is the thumb-to-index distance as a fraction of the hand size, as the camera sees it.
    ``palm`` extends all four fingers (an open palm); otherwise only the index finger is out (pointing).
    ``depth_error`` (metres) puts the thumb tip that far off in the tracker's depth estimate only, as
    happens when the hand is side-on to the camera."""
    world: List[Optional[Point]] = [None] * 21
    world[Landmark.WRIST] = Point(0.0, 0.0, 0.0)
    for name, base in _BASES.items():
        extended = palm or name == "INDEX"
        for offset, point in enumerate(_finger(base, extended)):
            world[getattr(Landmark, f"{name}_MCP") + offset] = point
    tip = world[Landmark.INDEX_TIP]
    thumb_tip = Point(tip.x - pinch * HAND_SIZE, tip.y, tip.z)
    world[Landmark.THUMB_CMC] = Point(-0.03, -0.02, 0.0)
    world[Landmark.THUMB_MCP] = Point(-0.05, -0.04, 0.0)
    world[Landmark.THUMB_IP] = Point((thumb_tip.x - 0.05) / 2, (thumb_tip.y - 0.04) / 2, 0.0)
    world[Landmark.THUMB_TIP] = thumb_tip
    # Image points: the shape scaled into a 640 x 480 frame around the fingertip, with square pixels.
    points = tuple(Point(x + (p.x - tip.x) * 1.5, y + (p.y - tip.y) * 2, p.z) for p in world)
    world[Landmark.THUMB_TIP] = Point(thumb_tip.x, thumb_tip.y, thumb_tip.z + depth_error)
    return Hand(side=side, score=score, points=points, world=tuple(world))


def frame(t: float, *hands: Hand, width: int = 640, height: int = 480) -> HandFrame:
    return HandFrame(time=t, width=width, height=height, hands=tuple(hands))


class FakePointer:
    """Records what the air mouse does to the pointer. Monitors are ``(left, top, right, bottom)``."""

    def __init__(self, monitors=((0, 0, 1920, 1080),), at=(960, 540)):
        self.monitors = [tuple(m) for m in monitors]
        self.at = tuple(at)
        self.down = False
        self.events: List[tuple] = []
        self.fail = False

    def desktop(self):
        return list(self.monitors)

    def position(self) -> Tuple[int, int]:
        return self.at

    def move_to(self, x: int, y: int) -> None:
        if self.fail:
            raise OSError("refused")
        self.at = (x, y)
        self.events.append(("move", x, y))

    def press(self) -> None:
        if self.fail:
            raise OSError("refused")
        self.down = True
        self.events.append(("press", *self.at))

    def release(self) -> None:
        self.down = False
        self.events.append(("release", *self.at))

    def buttons(self) -> List[tuple]:
        return [e for e in self.events if e[0] in ("press", "release")]

    def moves(self) -> List[tuple]:
        return [e for e in self.events if e[0] == "move"]

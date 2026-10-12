"""Synthetic hands and a fake desktop for gesture snapping tests.

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
         score: float = 0.9, depth_error: float = 0.0, fist: bool = False, curl: Optional[float] = None) -> Hand:
    """A hand with its index fingertip at image position ``(x, y)``.

    ``pinch`` is the thumb-to-index distance as a fraction of the hand size, as the camera sees it.
    ``palm`` extends all four fingers (an open palm); ``fist`` curls the index finger too; otherwise only the
    index finger is out (pointing).
    ``depth_error`` (metres) puts the thumb tip that far off in the tracker's depth estimate only, as
    happens when the hand is side-on to the camera. ``curl`` sets the index curl exactly (the tip on the line
    from the wrist through the index middle joint, at that multiple of the joint's distance)."""
    world: List[Optional[Point]] = [None] * 21
    world[Landmark.WRIST] = Point(0.0, 0.0, 0.0)
    for name, base in _BASES.items():
        extended = palm or (name == "INDEX" and not fist)
        for offset, point in enumerate(_finger(base, extended)):
            world[getattr(Landmark, f"{name}_MCP") + offset] = point
    if curl is not None:
        pip = world[Landmark.INDEX_PIP]
        world[Landmark.INDEX_TIP] = Point(pip.x * curl, pip.y * curl, pip.z * curl)
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


class FakeWindow:
    """A window on the fake desktop: its frame ``rect`` and whether it is maximised."""

    def __init__(self, rect, maximised=False, restored=None):
        self.rect = tuple(rect)
        self.maximised = maximised
        self.restored = tuple(restored) if restored else self.rect


class FakeDesktop:
    """The output gesture snapping drives, recording what it does. Monitors are ``(left, top, right, bottom)``;
    ``windows`` are listed front to back."""

    def __init__(self, monitors=((0, 0, 1920, 1080),), at=(960, 540), windows=()):
        self.monitors = [tuple(m) for m in monitors]
        self.at = tuple(at)
        self.windows = list(windows)
        self.events: List[tuple] = []
        self.fail = False
        self.fail_window_moves = False

    def desktop(self):
        return list(self.monitors)

    def position(self) -> Tuple[int, int]:
        return self.at

    def move_to(self, x: int, y: int) -> None:
        if self.fail:
            raise OSError("refused")
        self.at = (x, y)
        self.events.append(("pointer", x, y))

    def grab(self, x: int, y: int):
        for window in self.windows:
            left, top, right, bottom = window.rect
            if left <= x < right and top <= y < bottom:
                window.original = (window.rect, window.maximised)
                if window.maximised:  # restored under the pointer, keeping its share of the width
                    share = (x - left) / (right - left)
                    w = window.restored[2] - window.restored[0]
                    h = window.restored[3] - window.restored[1]
                    new_left = int(x - share * w)
                    window.rect = (new_left, y - 10, new_left + w, y - 10 + h)
                    window.maximised = False
                self.windows.remove(window)
                self.windows.insert(0, window)
                self.events.append(("grab", window))
                return window
        self.events.append(("grab", None))
        return None

    def move_window(self, window, left: int, top: int) -> None:
        if self.fail_window_moves:
            raise OSError("refused")
        width, height = window.rect[2] - window.rect[0], window.rect[3] - window.rect[1]
        window.rect = (left, top, left + width, top + height)
        self.events.append(("window", window, left, top))

    def scroll(self, delta: int) -> None:
        if self.fail:
            raise OSError("refused")
        self.events.append(("scroll", delta))

    def scrolled(self) -> int:
        return sum(e[1] for e in self.events if e[0] == "scroll")

    def drop(self, window, x: int, y: int) -> None:
        self.events.append(("drop", window, x, y))

    def of(self, kind: str) -> List[tuple]:
        return [e for e in self.events if e[0] == kind]

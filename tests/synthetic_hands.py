"""Synthetic hands and a fake desktop for gesture snapping tests.

``hand`` builds a ``Hand`` whose palm centre is at an image position and whose world landmarks raise or
fold each finger, so tests can describe gestures in the user's terms (one finger, a fist, two fingers, an open
hand) without a camera."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from jarvis.gestures.hand_tracking import Hand, HandFrame, Landmark, Point

FINGERS = ("INDEX", "MIDDLE", "RING", "PINKY")
_BASES = {"INDEX": (-0.02, -0.085), "MIDDLE": (0.0, -0.09), "RING": (0.02, -0.085), "PINKY": (0.04, -0.075)}
# Which fingers each gesture raises.
GESTURES = {"one": {"INDEX"}, "two": {"INDEX", "MIDDLE"}, "fist": set(), "open": set(FINGERS),
            "rock": {"INDEX", "PINKY"}}
UP, DOWN = 1.35, 0.75  # finger curls measured on a real webcam with the palm to the camera


def hand(x: float = 0.5, y: float = 0.5, *, gesture: str = "one", side: str = "right", score: float = 0.9,
         curls: Optional[Dict[str, float]] = None, tilt: float = 0.0,
         wrist: Optional[Tuple[float, float]] = None) -> Hand:
    """A hand with its palm centre at image position ``(x, y)`` making ``gesture`` (one, two, fist, open or
    rock). ``curls`` sets chosen fingers' curl exactly (wrist to tip over wrist to middle joint). ``tilt`` leans
    the hand in the image by that many degrees (positive leans the fingers to the user's right); ``wrist`` places
    the wrist at that image position instead of placing the palm centre."""
    raised = GESTURES[gesture]
    wanted = {name: (UP if name in raised else DOWN) for name in FINGERS}
    wanted.update({k.upper(): v for k, v in (curls or {}).items()})
    world: List[Optional[Point]] = [None] * 21
    world[Landmark.WRIST] = Point(0.0, 0.0, 0.0)
    for name, (bx, by) in _BASES.items():
        mcp, pip = Point(bx, by, 0.0), Point(bx, by - 0.03, 0.0)
        ratio = wanted[name]
        tip = Point(pip.x * ratio, pip.y * ratio, 0.0)
        dip = Point((pip.x + tip.x) / 2, (pip.y + tip.y) / 2, -0.005)
        for offset, point in enumerate((mcp, pip, dip, tip)):
            world[getattr(Landmark, f"{name}_MCP") + offset] = point
    world[Landmark.THUMB_CMC] = Point(-0.03, -0.02, 0.0)
    world[Landmark.THUMB_MCP] = Point(-0.05, -0.04, 0.0)
    world[Landmark.THUMB_IP] = Point(-0.06, -0.06, 0.0)
    world[Landmark.THUMB_TIP] = Point(-0.065, -0.08, 0.0)
    palm = [world[i] for i in (Landmark.WRIST, Landmark.INDEX_MCP, Landmark.MIDDLE_MCP, Landmark.RING_MCP,
                               Landmark.PINKY_MCP)]
    cx, cy = sum(p.x for p in palm) / 5, sum(p.y for p in palm) / 5
    # Image points: the shape scaled into a 640 x 480 frame around the palm centre, with square pixels.
    angle = math.radians(tilt)

    def placed(p: Point) -> Tuple[float, float]:
        # In square pixels of a 640 x 480 frame, turned clockwise on screen by ``tilt``.
        dx, dy = (p.x - cx) * 1.5 * 640, (p.y - cy) * 2 * 480
        rx, ry = dx * math.cos(angle) - dy * math.sin(angle), dx * math.sin(angle) + dy * math.cos(angle)
        return rx / 640, ry / 480

    if wrist is not None:
        wx, wy = placed(world[Landmark.WRIST])
        x, y = wrist[0] - wx, wrist[1] - wy
    points = tuple(Point(x + placed(p)[0], y + placed(p)[1], p.z) for p in world)
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

    def zoom(self, delta: int) -> None:
        self.events.append(("zoom", delta))

    def zoomed(self) -> int:
        return sum(e[1] for e in self.events if e[0] == "zoom")

    def click(self, kind: str) -> None:
        if self.fail:
            raise OSError("refused")
        self.events.append(("click", kind, self.at))

    def of(self, kind: str) -> List[tuple]:
        return [e for e in self.events if e[0] == kind]

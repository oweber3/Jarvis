"""Hand landmark recordings: a ``HandFrame`` stream as JSON Lines, landmarks only. See ``gestures.spec.md``."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator, Optional, Union

from .hand_tracking import Hand, HandFrame, Point

FORMAT = "jarvis-hand-frames"
VERSION = 1
_DECIMALS = 5


def _points(points) -> list:
    return [[round(c, _DECIMALS) for c in point] for point in points]


class HandRecorder:
    """A ``HandTracker`` subscriber that appends each ``HandFrame`` to ``path``."""

    def __init__(self, path: Union[str, Path]):
        self._file = open(path, "w", encoding="utf-8", buffering=1)
        self._file.write(json.dumps({"format": FORMAT, "version": VERSION}) + "\n")
        self._start: Optional[float] = None
        self._lock = threading.Lock()

    def __call__(self, frame: HandFrame) -> None:
        with self._lock:
            if self._file.closed:
                return
            if self._start is None:
                self._start = frame.time
            line = {
                "t": round(frame.time - self._start, 6),
                "w": frame.width,
                "h": frame.height,
                "hands": [{"side": hand.side, "score": round(hand.score, _DECIMALS),
                           "points": _points(hand.points), "world": _points(hand.world)}
                          for hand in frame.hands],
            }
            self._file.write(json.dumps(line, separators=(",", ":")) + "\n")

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def __enter__(self) -> "HandRecorder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def read_recording(path: Union[str, Path]) -> Iterator[HandFrame]:
    """The ``HandFrame``s of a recording, in order. Raises ``ValueError`` for a file that is not one."""
    with open(path, "r", encoding="utf-8") as handle:
        try:
            header = json.loads(handle.readline())
        except json.JSONDecodeError:
            raise ValueError("not a hand recording") from None
        if not isinstance(header, dict) or header.get("format") != FORMAT:
            raise ValueError("not a hand recording")
        version = header.get("version")
        if not isinstance(version, int) or version > VERSION:
            raise ValueError(f"unsupported hand recording version {version!r}")
        for line in handle:
            if not line.strip():
                continue
            data = json.loads(line)
            yield HandFrame(
                time=float(data["t"]), width=int(data["w"]), height=int(data["h"]),
                hands=tuple(Hand(side=hand["side"], score=float(hand["score"]),
                                 points=tuple(Point(*p) for p in hand["points"]),
                                 world=tuple(Point(*p) for p in hand["world"]))
                            for hand in data["hands"]))

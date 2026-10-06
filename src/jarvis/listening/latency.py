"""Per-turn voice latency marks, reported through ``debug_log``.

A turn is anchored at the moment the user stopped speaking (the last voiced
VAD frame). Each stage logs its own duration and the elapsed time since that
anchor, so one ``latency`` log category shows where a turn spent its time.
Nothing is persisted or sent anywhere.
"""

from __future__ import annotations

import time
from typing import Optional

from ..debug import debug_log


class TurnLatency:
    """Elapsed-time marks for one voice turn, anchored at end of speech."""

    def __init__(self, speech_end: float) -> None:
        self.speech_end = speech_end

    def elapsed_ms(self, now: Optional[float] = None) -> float:
        return ((time.time() if now is None else now) - self.speech_end) * 1000.0

    def mark(self, stage: str, duration_ms: Optional[float] = None) -> None:
        took = f" took {duration_ms:.0f} ms," if duration_ms is not None else ""
        debug_log(f"⏱️ {stage}:{took} +{self.elapsed_ms():.0f} ms after speech end", "latency")

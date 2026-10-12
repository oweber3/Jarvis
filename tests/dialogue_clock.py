"""A clock for the dialogue memory that tests can move forward, to age turns past the dialogue window."""
from __future__ import annotations

import time as real_time


class DialogueClock:
    """Stands in for ``time`` in ``jarvis.memory.conversation``; ``advance`` moves it forward."""

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return real_time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


def install(monkeypatch) -> DialogueClock:
    from jarvis.memory import conversation
    clock = DialogueClock()
    monkeypatch.setattr(conversation, "time", clock)
    return clock

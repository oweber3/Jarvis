"""Shared fakes for the scheduler tests: a hand-moved clock and a recorder for announcements."""
from __future__ import annotations

from datetime import datetime
from typing import List


class FakeClock:
    """Wall-clock seconds that move only when a test says so."""

    def __init__(self, start: float):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Announcements:
    """Collects what the scheduler would speak and show."""

    def __init__(self):
        self.said: List[str] = []

    def __call__(self, text: str) -> None:
        self.said.append(text)


def local_ts(year, month, day, hour=0, minute=0, second=0) -> float:
    """Epoch seconds of a local wall-clock time, as the scheduler reads local times."""
    return datetime(year, month, day, hour, minute, second).timestamp()


def make_service(tmp_path, clock, announce, **kwargs):
    from jarvis.scheduler.service import SchedulerService
    from jarvis.scheduler.store import ScheduleStore
    store = ScheduleStore(str(tmp_path / "jarvis.db"))
    kwargs.setdefault("run_jobs_inline", True)
    return SchedulerService(store, announce, clock=clock, **kwargs)

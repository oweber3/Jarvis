"""The scheduler: one thread in the daemon that fires timers, alarms and reminders and starts scheduled
routines at their time of day.

Times are wall-clock epoch seconds, checked against the clock on every wake, so a clock change or the
computer sleeping never makes an item fire early, and an item that came due while asleep fires on the
first wake after. An item fires exactly once: it is marked done in the store before it is announced.
See ``scheduler.spec.md``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import time
from typing import Any, Callable, Iterable, List, Optional

from ..debug import debug_log
from . import phrases
from .store import CANCELLED, DONE, MISSED, Item, ScheduleStore

# An item that came due at most this long before Jarvis started fires normally; older ones are missed.
MISSED_GRACE_SEC = 120.0
# A scheduled routine runs if Jarvis notices its time within this window (after sleep, say); never later.
ROUTINE_GRACE_SEC = 600.0
# The longest the thread sleeps between checks, so clock changes and sleep are noticed promptly.
MAX_WAIT_SEC = 30.0
# Finished items are deleted this long after they finished.
RETENTION_SEC = 30 * 86400.0
# How long shutdown waits for a scheduled routine in flight to stop.
JOB_STOP_WAIT_SEC = 5.0
# The longest timer or the furthest reminder accepted.
MAX_AHEAD_SEC = 366 * 86400.0


def routine_runs_between(schedule, start: float, end: float) -> List[float]:
    """The local times in ``(start, end]`` at which ``schedule`` (a ``routines.definitions.Schedule``)
    runs, as epoch seconds, oldest first."""
    if schedule is None or not schedule.enabled or end <= start:
        return []
    runs = []
    day = datetime.fromtimestamp(start).date()
    last = datetime.fromtimestamp(end).date()
    while day <= last:
        if day.weekday() in schedule.days:
            moment = datetime(day.year, day.month, day.day, schedule.hour, schedule.minute).timestamp()
            if start < moment <= end:
                runs.append(moment)
        day += timedelta(days=1)
    return runs


class SchedulerService:
    def __init__(self, store: ScheduleStore, announce: Callable[[str], None], *,
                 clock: Callable[[], float] = time.time,
                 routines: Optional[Callable[[], Iterable[Any]]] = None,
                 run_routine: Optional[Callable[[Any], None]] = None,
                 run_jobs_inline: bool = False) -> None:
        self.store = store
        self._announce = announce
        self._clock = clock
        self._routines = routines
        self._run_routine = run_routine
        self._inline = run_jobs_inline
        self._wake = threading.Condition()
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self._jobs: Optional[ThreadPoolExecutor] = None
        self._last_job = None
        self._tick_lock = threading.Lock()
        self._routines_checked = clock()

    # --- lifecycle --------------------------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def start(self, *, thread: bool = True) -> None:
        """Announce items missed while Jarvis was off (once), prune old ones, then start checking."""
        now = self._clock()
        self._routines_checked = now
        self.store.prune(now - RETENTION_SEC)
        self._announce_missed(now)
        if thread:
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, name="jarvis-scheduler", daemon=True)
            self._thread.start()
        debug_log(f"scheduler started ({len(self.store.pending())} pending)", "scheduler")

    def stop(self) -> None:
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._jobs is not None:
            # A scheduled routine in flight stops between steps; wait a bounded time for it to let go of the
            # query lock and the database before shutdown goes on.
            from ..routines import runner
            runner.stop_running()
            self._jobs.shutdown(wait=False, cancel_futures=True)
            if self._last_job is not None:
                try:
                    self._last_job.result(timeout=JOB_STOP_WAIT_SEC)
                except Exception:  # noqa: BLE001 - timeout or the job's own failure, already logged
                    debug_log("scheduled routine still running at shutdown", "scheduler")
            self._jobs = None
        self.store.close()
        debug_log("scheduler stopped", "scheduler")

    def _loop(self) -> None:
        while True:
            with self._wake:
                if self._stopping:
                    return
                self._wake.wait(self._wait_seconds())
                if self._stopping:
                    return
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the scheduler thread never dies on one bad item
                debug_log(f"scheduler tick failed ({type(exc).__name__})", "scheduler")

    def _wait_seconds(self) -> float:
        pending = self.store.pending()
        if not pending:
            return MAX_WAIT_SEC
        return max(0.05, min(MAX_WAIT_SEC, pending[0].due - self._clock()))

    def _notify(self) -> None:
        with self._wake:
            self._wake.notify_all()

    # --- items ------------------------------------------------------------------------------

    def add_timer(self, seconds: float, label: str, language: str) -> Item:
        now = self._clock()
        item = self.store.add("timer", label, now + float(seconds), now, language)
        debug_log(f"timer added ({int(seconds)} s, {'named' if label else 'unnamed'})", "scheduler")
        self._notify()
        return item

    def add_reminder(self, due: float, label: str, language: str) -> Item:
        kind = "reminder" if label else "alarm"
        item = self.store.add(kind, label, float(due), self._clock(), language)
        debug_log(f"{kind} added (due in {int(due - self._clock())} s)", "scheduler")
        self._notify()
        return item

    def pending(self, kind: Optional[str] = None) -> List[Item]:
        return self.store.pending(kind)

    def cancel(self, item_id: int) -> bool:
        cancelled = self.store.close_item(item_id, CANCELLED, self._clock())
        debug_log(f"scheduled item cancel: {'ok' if cancelled else 'not pending'}", "scheduler")
        self._notify()
        return cancelled

    # --- firing -----------------------------------------------------------------------------

    def tick(self) -> None:
        """Fire every item that is due and start every scheduled routine whose time has come."""
        with self._tick_lock:
            now = self._clock()
            for item in self.store.pending():
                if item.due > now:
                    break
                if not self.store.close_item(item.id, DONE, now):
                    continue
                debug_log(f"{item.kind} fired", "scheduler")
                self._say(due_text(item))
            self._check_routines(now)

    def _say(self, text: str) -> None:
        try:
            self._announce(text)
        except Exception as exc:  # noqa: BLE001 - one failed announcement never stops the others
            debug_log(f"scheduler announcement failed ({type(exc).__name__})", "scheduler")

    def _announce_missed(self, now: float) -> None:
        missed = [item for item in self.store.pending() if item.due < now - MISSED_GRACE_SEC]
        closed = [item for item in missed if self.store.close_item(item.id, MISSED, now)]
        if not closed:
            return
        debug_log(f"scheduler: {len(closed)} items missed while off", "scheduler")
        language = closed[-1].language
        self._say(phrases.text("missed_intro", language,
                               items=phrases.join([missed_text(item) for item in closed], language)))

    # --- scheduled routines -----------------------------------------------------------------

    def _check_routines(self, now: float) -> None:
        # Never step back: a clock moved backwards must not bring a run that already happened round again.
        start = self._routines_checked
        self._routines_checked = max(start, now)
        if self._routines is None or self._run_routine is None:
            return
        try:
            routines = list(self._routines())
        except Exception as exc:  # noqa: BLE001
            debug_log(f"scheduled routines unavailable ({type(exc).__name__})", "scheduler")
            return
        for routine in routines:
            runs = routine_runs_between(getattr(routine, "schedule", None), start, now)
            if not runs:
                continue
            if now - runs[-1] > ROUTINE_GRACE_SEC:
                debug_log("scheduled routine skipped: its time passed while Jarvis was not running", "scheduler")
                continue
            debug_log("scheduled routine due", "scheduler")
            self._submit(routine)

    def _submit(self, routine) -> None:
        def job():
            try:
                self._run_routine(routine)
            except Exception as exc:  # noqa: BLE001
                debug_log(f"scheduled routine failed ({type(exc).__name__})", "scheduler")
        if self._inline:
            job()
            return
        if self._jobs is None:
            self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-scheduled-routine")
        self._last_job = self._jobs.submit(job)


def due_text(item: Item) -> str:
    """What Jarvis says when an item comes due, in the language it was set in."""
    if item.kind == "timer":
        return (phrases.text("timer_done", item.language, label=item.label) if item.label
                else phrases.text("timer_done_unnamed", item.language))
    if item.kind == "reminder":
        return phrases.text("reminder_due", item.language, message=item.label)
    return phrases.text("alarm_due", item.language, time=phrases.clock_time(item.due, item.language))


def missed_text(item: Item) -> str:
    time_text = phrases.clock_time(item.due, item.language)
    if item.kind == "timer":
        return (phrases.fragment("missed_timer", item.language, label=item.label) if item.label
                else phrases.fragment("missed_timer_unnamed", item.language))
    if item.kind == "reminder":
        return phrases.fragment("missed_reminder", item.language, time=time_text, message=item.label)
    return phrases.fragment("missed_alarm", item.language, time=time_text)

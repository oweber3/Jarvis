"""The scheduler inside the daemon: started at boot with the way Jarvis announces things, stopped at exit.

``scheduleControl`` reaches the running service through ``get_service``; with none running it fails
honestly. See ``scheduler.spec.md``, Daemon.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable, ContextManager, Optional

from ..debug import debug_log
from .service import SchedulerService
from .store import ScheduleStore

_lock = threading.Lock()
_service: Optional[SchedulerService] = None


def get_service() -> Optional[SchedulerService]:
    with _lock:
        return _service


def set_service(service: Optional[SchedulerService]) -> None:
    global _service
    with _lock:
        _service = service


def start(cfg: Any, announce: Callable[[str], None], *, db=None,
          exclusive: Callable[[], ContextManager] = contextlib.nullcontext) -> Optional[SchedulerService]:
    """Open the store in the Jarvis database and start the scheduler thread.

    ``announce`` speaks and shows a sentence; ``exclusive`` is the shared query lock a scheduled routine
    holds while it runs, so it never overlaps a reply."""
    from ..routines import store as routine_store
    from .routine_runs import run_scheduled_routine
    stop()
    try:
        store = ScheduleStore(cfg.db_path)
    except Exception as exc:  # noqa: BLE001 - timers are unavailable, the rest of Jarvis is not
        debug_log(f"scheduler store unavailable ({type(exc).__name__})", "scheduler")
        print("  ⚠️ Timers and reminders are unavailable: the database could not be opened.", flush=True)
        return None

    def run(routine) -> None:
        run_scheduled_routine(routine, cfg, announce=announce, db=db, exclusive=exclusive)

    service = SchedulerService(store, announce, routines=lambda: routine_store.current(cfg).values(),
                               run_routine=run)
    service.start()
    set_service(service)
    pending = len(service.pending())
    scheduled = sum(1 for routine in routine_store.current(cfg).values()
                    if routine.schedule is not None and routine.schedule.enabled)
    print(f"⏰ Scheduler ready", flush=True)
    print(f"   ⏳ {pending} timer(s), alarm(s) and reminder(s) pending", flush=True)
    print(f"   🔁 {scheduled} scheduled routine(s)", flush=True)
    return service


def stop() -> None:
    service = get_service()
    set_service(None)
    if service is not None:
        service.stop()

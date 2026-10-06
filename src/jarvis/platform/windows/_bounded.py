"""Run a blocking OS call on a worker thread with a hard time limit.

Tools execute on the listener thread, so a stuck COM or WinRT call must never
be able to freeze the microphone loop. The worker is a daemon thread: if the
call hangs it is abandoned and the caller gets a ``TimeoutError``.
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


def run_bounded(fn: Callable[[], T], timeout_sec: float) -> T:
    """Return ``fn()``, re-raising its exception, or raise ``TimeoutError``."""
    box: dict = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # propagate to the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_worker, name="windows-os-call", daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        raise TimeoutError(f"OS call did not finish within {timeout_sec:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]

"""Runs the opt-in activity log inside the daemon: watcher thread, recorder, daily pruning.

Nothing starts unless ``activity_log_enabled`` is true. The pause switch and the delete-all action
live here so the tray (bundled or subprocess) and the daemon share one implementation. See
``activity_log.spec.md``.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Optional

from ..debug import debug_log
from .activity_log import ActivityRecorder, ActivityStore

PRUNE_INTERVAL_SEC = 24 * 3600.0
_FOREGROUND_READ_SEC = 3.0


class ActivityService:
    """Feeds watcher events into the recorder and prunes old sessions once a day."""

    def __init__(self, cfg: Any, store: ActivityStore, recorder: ActivityRecorder, *,
                 foreground: Callable[[], Any], idle: Callable[[], float],
                 watcher_factory: Callable[[Callable[[str], None]], Any],
                 clock: Callable[[], float] = time.time) -> None:
        self._retention_sec = float(cfg.activity_log_retention_days) * 86400.0
        self.store = store
        self.recorder = recorder
        self._foreground = foreground
        self._idle = idle
        self._clock = clock
        self._lock = threading.RLock()
        self._last_prune = 0.0
        self._watcher = watcher_factory(self.on_event)
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        self._prune(self._clock())
        # The watcher reports its first observation from its own thread, possibly before start() returns.
        self._running = True
        if not self._watcher.start():
            self._running = False
            debug_log("activity watcher did not start", "activity")
        return self._running

    def stop(self) -> None:
        self._running = False
        try:
            self._watcher.stop()
        finally:
            with self._lock:
                self.recorder.close(self._clock())
            self.store.close()

    def on_event(self, kind: str) -> None:
        with self._lock:
            if not self._running:
                return
            now = self._clock()
            if not self.recorder.paused:  # paused means not observed: no title or idle read at all
                try:
                    foreground = self._foreground()
                    idle = self._idle()
                except Exception as exc:
                    debug_log(f"activity read failed: {type(exc).__name__}", "activity")
                    return
                now = self._clock()
                self.recorder.observe(foreground, idle, now)
            if now - self._last_prune >= PRUNE_INTERVAL_SEC:
                self._prune(now)

    def _prune(self, now: float) -> None:
        self._last_prune = now
        self.store.prune(now - self._retention_sec)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if paused:
                self.recorder.pause(self._clock())
            else:
                self.recorder.resume()
        if not paused:
            self.on_event("resume")

    def delete_history(self) -> int:
        with self._lock:
            return self.recorder.delete_history()


_service: Optional[ActivityService] = None
_service_lock = threading.Lock()


def get_service() -> Optional[ActivityService]:
    return _service


def _platform_readers():
    from ..platform.windows import activity as windows_activity
    from ..platform.windows._bounded import run_bounded
    return (lambda: run_bounded(windows_activity.foreground_snapshot, _FOREGROUND_READ_SEC),
            windows_activity.idle_seconds,
            windows_activity.ForegroundWatcher)


def start(cfg: Any, *, foreground: Optional[Callable[[], Any]] = None, idle: Optional[Callable[[], float]] = None,
          watcher_factory: Optional[Callable[[Callable[[str], None]], Any]] = None,
          clock: Callable[[], float] = time.time) -> Optional[ActivityService]:
    """Start recording when the user has opted in. Returns the running service, or None."""
    global _service
    if getattr(cfg, "activity_log_enabled", False) is not True:
        return None
    if watcher_factory is None and sys.platform != "win32":
        debug_log("activity log needs Windows; not started", "activity")
        return None
    stop()
    if watcher_factory is None or foreground is None or idle is None:
        default_foreground, default_idle, watcher_class = _platform_readers()
        foreground = foreground or default_foreground
        idle = idle or default_idle
        watcher_factory = watcher_factory or watcher_class
    store = ActivityStore(cfg.db_path)
    recorder = ActivityRecorder(
        store, excluded_processes=cfg.activity_log_excluded_processes,
        private_markers=cfg.activity_log_private_title_markers, idle_after_sec=cfg.activity_log_idle_after_sec)
    service = ActivityService(cfg, store, recorder, foreground=foreground, idle=idle,
                              watcher_factory=watcher_factory, clock=clock)
    if getattr(cfg, "activity_log_paused", False) is True:
        recorder.pause(clock())
    with _service_lock:
        _service = service
    if not service.start():
        stop()
        return None
    print("🗂️ Activity log on: recording the foreground app and a redacted window title", flush=True)
    if recorder.paused:
        print("  ⏸️ Paused", flush=True)
    debug_log("activity log started", "activity")
    return service


def stop() -> None:
    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        service.stop()
        debug_log("activity log stopped", "activity")


def set_paused(paused: bool, *, persist: bool = True) -> bool:
    """Pause or resume recording and remember the choice. Returns the resulting paused state."""
    service = _service
    if service is not None:
        service.set_paused(paused)
    if persist:
        from ..config import update_config_values
        update_config_values({"activity_log_paused": bool(paused)})
    print("⏸️ Activity log paused" if paused else "▶️ Activity log resumed", flush=True)
    return bool(paused)


# Set when the user switches cloud sharing off while Jarvis runs; switching it on waits for a restart.
_sharing_stopped = False


def stop_cloud_sharing() -> None:
    """Withhold the activity log from Codex and Claude from now on, before the saved setting is reloaded."""
    global _sharing_stopped
    _sharing_stopped = True
    print("🔒 Activity log no longer shared with Codex or Claude", flush=True)
    debug_log("activity log cloud sharing stopped", "activity")


def shared_with_cloud(cfg: Any) -> bool:
    """Whether Codex and Claude may read the activity log: only a real ``True``, and not stopped since."""
    return getattr(cfg, "activity_log_share_with_cloud", False) is True and not _sharing_stopped


def delete_history(cfg: Any = None) -> int:
    """Delete every recorded session. Works whether or not recording is running."""
    service = _service
    if service is not None:
        removed = service.delete_history()
    else:
        if cfg is None:
            from ..config import load_settings
            cfg = load_settings()
        store = ActivityStore(cfg.db_path)
        try:
            removed = store.delete_all()
        finally:
            store.close()
    print(f"🗑️ Activity history deleted ({removed} sessions)", flush=True)
    return removed

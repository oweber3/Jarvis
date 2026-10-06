"""Opt-in activity log: which application (and redacted window title) was in the foreground, and when.

Three parts, none of which know about the OS: ``ActivityStore`` (its own SQLite table in the Jarvis
database), ``ActivityRecorder`` (turns foreground observations into sessions, applying idle, pause,
exclusion and redaction rules) and the read side (``summarise`` and ``timeline``) behind the
``activityLog`` tool. See ``activity_log.spec.md``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

from ..debug import debug_log
from ..utils.redact import redact

TITLE_MAX_CHARS = 160
MIN_SESSION_SEC = 2.0
SUMMARY_MAX_APPS = 12
SUMMARY_MAX_TITLES = 5
TIMELINE_DEFAULT_LIMIT = 100
TIMELINE_MAX_LIMIT = 300

_DEFAULTS_FILE = Path(__file__).with_name("activity_exclusions.json")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity_sessions (
  id       INTEGER PRIMARY KEY,
  start_ts REAL NOT NULL,
  end_ts   REAL NOT NULL,
  idle     INTEGER NOT NULL DEFAULT 0,
  process  TEXT NOT NULL,
  app      TEXT NOT NULL,
  title    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_sessions_end ON activity_sessions(end_ts);
"""


# ── Defaults ───────────────────────────────────────────────────────────────

# Used only when the data file cannot be read, so a damaged install fails closed instead of
# recording password managers and private windows.
_FALLBACK_DEFAULTS: Dict[str, Any] = {
    "processes": ["1Password", "Bitwarden", "KeePass", "KeePassXC"],
    "private_title_markers": {"en": ["Incognito", "InPrivate", "Private Browsing"]},
}


@lru_cache(maxsize=1)
def _defaults() -> Dict[str, Any]:
    try:
        return json.loads(_DEFAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        debug_log(f"activity exclusion defaults unreadable, using the built-in minimum: {type(exc).__name__}",
                  "activity")
        return _FALLBACK_DEFAULTS


def default_excluded_processes() -> List[str]:
    """Password managers, from the data file. Replaced wholesale by ``activity_log_excluded_processes``."""
    return [str(p) for p in _defaults().get("processes", []) if isinstance(p, str)]


def default_private_title_markers() -> List[str]:
    """Private-browsing title markers in every supported language, from the locale data file."""
    markers: List[str] = []
    for words in (_defaults().get("private_title_markers") or {}).values():
        for word in words if isinstance(words, list) else []:
            if isinstance(word, str) and word not in markers:
                markers.append(word)
    return markers


def _process_key(name: str) -> str:
    folded = name.casefold().strip()
    if folded.endswith(".exe"):
        folded = folded[:-4]
    return re.sub(r"[^0-9a-z\u0080-￿]", "", folded)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


# ── Time ───────────────────────────────────────────────────────────────────

def parse_time(text: str) -> float:
    """Epoch seconds from an ISO 8601 date or date-time. A time without an offset is local time."""
    try:
        moment = datetime.fromisoformat(str(text).strip())
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 date or time: {str(text)[:40]!r}") from exc
    return moment.astimezone().timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


# ── Per-turn privacy flag ──────────────────────────────────────────────────
#
# A reply that quotes activity data must not reach the diary. The tool marks the turn on the thread
# that owns the request; delivery consumes the mark on the same thread.

_turn = threading.local()


def mark_turn_private() -> None:
    _turn.private = True


def is_turn_private() -> bool:
    """Whether this thread's current turn is private, without consuming the mark."""
    return bool(getattr(_turn, "private", False))


def consume_turn_private() -> bool:
    private = bool(getattr(_turn, "private", False))
    _turn.private = False
    return private


# ── Storage ────────────────────────────────────────────────────────────────

class Observation(NamedTuple):
    """What the platform layer saw in the foreground."""
    process: str
    app: str
    title: str


@dataclass(frozen=True)
class Session:
    id: int
    start: float
    end: float
    process: str
    app: str
    title: str
    idle: bool


class ActivityStore:
    """Sessions in their own table of the Jarvis SQLite database."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        # Deleted history is overwritten rather than left readable in free pages.
        self.conn.execute("PRAGMA secure_delete = ON")
        with self._lock:
            self.conn.executescript(_SCHEMA_SQL)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    def open_session(self, start: float, process: str, app: str, title: str, *, idle: bool = False,
                     end: Optional[float] = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO activity_sessions(start_ts, end_ts, idle, process, app, title) VALUES (?,?,?,?,?,?)",
                (start, start if end is None else end, 1 if idle else 0, process, app, title))
            self.conn.commit()
            return int(cur.lastrowid)

    def update_end(self, session_id: int, end: float) -> bool:
        """Move a session's end. False when the row no longer exists (deleted meanwhile)."""
        with self._lock:
            cur = self.conn.execute("UPDATE activity_sessions SET end_ts = ? WHERE id = ?", (end, session_id))
            self.conn.commit()
            return cur.rowcount > 0

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM activity_sessions WHERE id = ?", (session_id,))
            self.conn.commit()

    def sessions(self, start: float, end: float) -> List[Session]:
        """Sessions overlapping ``[start, end]``, oldest first."""
        with self._lock:
            found = self.conn.execute(
                "SELECT id, start_ts, end_ts, process, app, title, idle FROM activity_sessions "
                "WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts, id", (start, end)).fetchall()
        return [Session(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6])) for r in found]

    def earliest_start(self) -> Optional[float]:
        with self._lock:
            row = self.conn.execute("SELECT MIN(start_ts) FROM activity_sessions").fetchone()
        return row[0] if row and row[0] is not None else None

    def prune(self, cutoff: float) -> int:
        """Remove sessions that ended before ``cutoff``. Returns how many."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM activity_sessions WHERE end_ts < ?", (cutoff,))
            self.conn.commit()
            removed = cur.rowcount
        if removed:
            debug_log(f"activity log pruned {removed} sessions", "activity")
        return removed

    def delete_all(self) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM activity_sessions")
            self.conn.commit()
            removed = cur.rowcount
            try:
                # Fold the write-ahead log back so deleted rows do not linger in it.
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass
        debug_log(f"activity log deleted {removed} sessions", "activity")
        return removed


# ── Recording ──────────────────────────────────────────────────────────────

@dataclass
class _Open:
    session_id: int
    start: float
    key: Any
    idle: bool


class ActivityRecorder:
    """Turns ``observe(foreground, idle_sec, now)`` calls into stored sessions.

    One open session at a time. It grows with every observation (so a crash loses little), ends when
    the application or title changes, when the user goes idle (at the moment of the last input), when
    nothing recordable is in the foreground, or on pause and close. Excluded applications and private
    browsing windows leave a gap and no trace.
    """

    def __init__(self, store: ActivityStore, *, excluded_processes: Iterable[str] = (),
                 private_markers: Iterable[str] = (), idle_after_sec: float = 300.0,
                 min_session_sec: float = MIN_SESSION_SEC) -> None:
        self._store = store
        self._excluded = {_process_key(p) for p in excluded_processes if isinstance(p, str) and p.strip()}
        self._markers = [_fold(m) for m in private_markers if isinstance(m, str) and m.strip()]
        self._idle_after = float(idle_after_sec)
        self._min = float(min_session_sec)
        self._lock = threading.RLock()
        self._open: Optional[_Open] = None
        self._paused = False
        self._floor: Optional[float] = None  # nothing is recorded before this instant
        # Observations arrive every few seconds; a gap as long as the idle threshold means the PC
        # slept or recording stalled, and nothing can be claimed for that time.
        self._max_gap = self._idle_after
        self._last_seen: Optional[float] = None

    @property
    def paused(self) -> bool:
        return self._paused

    def _recordable(self, foreground: Optional[Observation]) -> bool:
        if foreground is None or _process_key(foreground.process) in self._excluded:
            return False
        folded = _fold(foreground.title)
        return not any(marker in folded for marker in self._markers)

    def _end(self, end: float) -> None:
        current, self._open = self._open, None
        if current is None:
            return
        end = max(end, current.start)
        self._floor = end
        duration = end - current.start
        if duration <= 0 if current.idle else duration < self._min:
            self._store.delete_session(current.session_id)
        else:
            self._store.update_end(current.session_id, end)

    def _grow(self, now: float) -> bool:
        """Extend the open session to ``now``. False when its row has vanished."""
        assert self._open is not None
        if self._store.update_end(self._open.session_id, max(now, self._open.start)):
            return True
        self._open = None
        return False

    def observe(self, foreground: Optional[Observation], idle_sec: float, now: float) -> None:
        with self._lock:
            if self._paused:
                return
            if self._floor is None:
                self._floor = now
            last_seen, self._last_seen = self._last_seen, now
            if (last_seen is not None and now - last_seen >= self._max_gap
                    and self._open is not None and not self._open.idle):
                debug_log("activity gap (sleep or stall); session ended at the last observation", "activity")
                self._end(last_seen)
            idle_sec = max(0.0, float(idle_sec))
            if idle_sec >= self._idle_after:
                self._observe_idle(now, idle_sec)
                return
            boundary = now
            if self._open is not None and self._open.idle:
                boundary = min(now, max(self._open.start, now - idle_sec))
                self._end(boundary)
            if not self._recordable(foreground):
                if self._open is not None:
                    self._end(now)
                return
            title = redact(foreground.title, max_len=TITLE_MAX_CHARS)
            key = (_process_key(foreground.process), title)
            if self._open is not None and self._open.key == key and self._grow(now):
                return
            if self._open is not None:
                self._end(now)
            session_id = self._store.open_session(boundary, foreground.process, foreground.app, title)
            self._open = _Open(session_id, boundary, key, False)
            self._grow(now)

    def _observe_idle(self, now: float, idle_sec: float) -> None:
        if self._open is not None and self._open.idle:
            self._grow(now)
            return
        started = max(now - idle_sec, self._floor if self._floor is not None else now)
        if self._open is not None:
            started = max(started, self._open.start)
            self._end(started)
        session_id = self._store.open_session(started, "", "", "", idle=True)
        self._open = _Open(session_id, started, "idle", True)
        self._grow(now)

    def pause(self, now: float) -> None:
        with self._lock:
            self._end(now)
            self._paused = True
            self._last_seen = None
        debug_log("activity log paused", "activity")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._floor = None
        debug_log("activity log resumed", "activity")

    def close(self, now: float) -> None:
        with self._lock:
            self._end(now)

    def delete_history(self) -> int:
        """Delete every stored session, including the one being recorded, and start clean."""
        with self._lock:
            self._open = None
            return self._store.delete_all()


# ── Read side ──────────────────────────────────────────────────────────────

def _clip(sessions: List[Session], start: float, end: float):
    for s in sessions:
        lo, hi = max(s.start, start), min(s.end, end)
        if hi > lo:
            yield s, lo, hi


def _range(start: float, end: float) -> Dict[str, Any]:
    return {"start": _iso(start), "end": _iso(end), "timezone": _iso(start)[-6:]}


def summarise(store: ActivityStore, start: float, end: float) -> Dict[str, Any]:
    """Time per application and window title in ``[start, end]``, idle time counted separately."""
    apps: Dict[str, Dict[str, Any]] = {}
    idle_seconds = 0.0
    for s, lo, hi in _clip(store.sessions(start, end), start, end):
        seconds = hi - lo
        if s.idle:
            idle_seconds += seconds
            continue
        entry = apps.setdefault(s.app or s.process, {"seconds": 0.0, "titles": {}})
        entry["seconds"] += seconds
        title = redact(s.title, max_len=TITLE_MAX_CHARS)
        entry["titles"][title] = entry["titles"].get(title, 0.0) + seconds
    ranked = sorted(apps.items(), key=lambda kv: (-kv[1]["seconds"], kv[0]))
    earliest = store.earliest_start()
    result: Dict[str, Any] = {
        "range": _range(start, end),
        "active_seconds": round(sum(a["seconds"] for a in apps.values())),
        "idle_seconds": round(idle_seconds),
        "apps": [{
            "app": name,
            "seconds": round(entry["seconds"]),
            "titles": [{"title": t, "seconds": round(sec)} for t, sec in sorted(
                entry["titles"].items(), key=lambda kv: (-kv[1], kv[0]))[:SUMMARY_MAX_TITLES]],
        } for name, entry in ranked[:SUMMARY_MAX_APPS]],
        "recording_since": _iso(earliest) if earliest is not None else None,
    }
    if len(ranked) > SUMMARY_MAX_APPS:
        result["apps_omitted"] = len(ranked) - SUMMARY_MAX_APPS
    return result


def timeline(store: ActivityStore, start: float, end: float, limit: int = TIMELINE_DEFAULT_LIMIT
             ) -> Dict[str, Any]:
    """Sessions in ``[start, end]`` in order, clipped to the range, at most ``limit`` of them."""
    limit = max(1, min(int(limit), TIMELINE_MAX_LIMIT))
    entries = [{
        "start": _iso(lo), "end": _iso(hi), "seconds": round(hi - lo),
        "app": s.app or s.process, "title": redact(s.title, max_len=TITLE_MAX_CHARS), "idle": s.idle,
    } for s, lo, hi in _clip(store.sessions(start, end), start, end)]
    earliest = store.earliest_start()
    return {
        "range": _range(start, end),
        "sessions": entries[:limit],
        "truncated": len(entries) > limit,
        "total_sessions": len(entries),
        "recording_since": _iso(earliest) if earliest is not None else None,
    }

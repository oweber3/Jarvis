"""Timers, alarms and reminders kept in the Jarvis database, so they survive a restart.

``ScheduleStore`` owns the ``scheduled_items`` table in the same SQLite file as the diary, with its own
connection, as ``ActivityStore`` and ``ChatStore`` do. The table is never read by retrieval, the diary or
the graph. See ``scheduler.spec.md``, Storage.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
from typing import List, Optional

from ..debug import debug_log

KINDS = ("timer", "reminder", "alarm")
PENDING, DONE, MISSED, CANCELLED = "pending", "done", "missed", "cancelled"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_items (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,
  label       TEXT NOT NULL DEFAULT '',
  due_utc     REAL NOT NULL,
  created_utc REAL NOT NULL,
  language    TEXT NOT NULL DEFAULT 'en',
  status      TEXT NOT NULL DEFAULT 'pending',
  closed_utc  REAL
);
CREATE INDEX IF NOT EXISTS scheduled_items_pending ON scheduled_items(status, due_utc);
"""


@dataclass(frozen=True)
class Item:
    id: int
    kind: str
    label: str
    due: float
    created: float
    language: str
    status: str = PENDING


class ScheduleStore:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _item(row) -> Item:
        return Item(row["id"], row["kind"], row["label"], row["due_utc"], row["created_utc"], row["language"],
                    row["status"])

    def add(self, kind: str, label: str, due: float, created: float, language: str) -> Item:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO scheduled_items(kind, label, due_utc, created_utc, language) VALUES (?, ?, ?, ?, ?)",
                (kind, label, float(due), float(created), language))
            self.conn.commit()
            return Item(int(cursor.lastrowid), kind, label, float(due), float(created), language)

    def pending(self, kind: Optional[str] = None) -> List[Item]:
        """Pending items, soonest first."""
        query = "SELECT * FROM scheduled_items WHERE status = ?"
        params: list = [PENDING]
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        with self._lock:
            rows = self.conn.execute(query + " ORDER BY due_utc, id", params).fetchall()
        return [self._item(row) for row in rows]

    def close_item(self, item_id: int, status: str, when: float) -> bool:
        """Move a pending item to ``status``. False when it is no longer pending (it fires or cancels once)."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE scheduled_items SET status = ?, closed_utc = ? WHERE id = ? AND status = ?",
                (status, float(when), int(item_id), PENDING))
            self.conn.commit()
            return cursor.rowcount == 1

    def prune(self, before: float) -> int:
        """Delete finished items closed before ``before``."""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM scheduled_items WHERE status != ? AND closed_utc IS NOT NULL AND closed_utc < ?",
                (PENDING, float(before)))
            self.conn.commit()
        if cursor.rowcount:
            debug_log(f"scheduler pruned {cursor.rowcount} finished items", "scheduler")
        return cursor.rowcount

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM scheduled_items").fetchone()[0])

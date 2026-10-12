"""Named lists and quick notes, in their own tables of the Jarvis database.

``ListsNotesStore`` follows ``ActivityStore`` and ``ChatStore``: it owns its schema and its
connection. Names and items are matched by their words, case folded, so no word of any language is
special. Nothing here logs list or note content. See ``lists_notes.spec.md``.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..debug import debug_log

NAME_MAX_CHARS = 80
ITEM_MAX_CHARS = 200
NOTE_MAX_CHARS = 2000
MAX_ITEMS_PER_CALL = 50
SEARCH_DEFAULT_LIMIT = 5
SEARCH_MAX_LIMIT = 20

_WORD = re.compile(r"\w+", re.UNICODE)
_LONG_WORD = re.compile(r"\w{3,}", re.UNICODE)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_lists (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  key        TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_list_items (
  id         INTEGER PRIMARY KEY,
  list_id    INTEGER NOT NULL REFERENCES user_lists(id) ON DELETE CASCADE,
  text       TEXT NOT NULL,
  key        TEXT NOT NULL,
  done       INTEGER NOT NULL DEFAULT 0,
  position   INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(list_id, key)
);
CREATE INDEX IF NOT EXISTS idx_user_list_items_list ON user_list_items(list_id, position);
CREATE TABLE IF NOT EXISTS user_notes (
  id         INTEGER PRIMARY KEY,
  text       TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_notes_created ON user_notes(created_at);
"""


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _words(text: str) -> List[str]:
    return _WORD.findall(_fold(text))


def _key(text: str) -> str:
    return "".join(_words(text))


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text if text is not None else "").split())[:limit]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_items(items: Iterable[Any]) -> List[str]:
    cleaned: List[str] = []
    for item in list(items)[:MAX_ITEMS_PER_CALL]:
        text = _clean(item, ITEM_MAX_CHARS)
        if _key(text):
            cleaned.append(text)
    if not cleaned:
        raise ValueError("at least one item is required")
    return cleaned


class ListsNotesStore:
    """Lists, their items and notes. Thread-safe: one connection guarded by a re-entrant lock."""

    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        # Removed items and notes are overwritten rather than left readable in free pages.
        self.conn.execute("PRAGMA secure_delete = ON")
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self.conn.executescript(_SCHEMA_SQL)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # -- lists -------------------------------------------------------------------------------

    def _resolve(self, name: str) -> Optional[tuple]:
        """``(id, name)`` of the list this name means, or None (see the spec, Finding a list by name)."""
        key = _key(name)
        if not key:
            return None
        rows = self.conn.execute("SELECT id, name, key FROM user_lists ORDER BY id").fetchall()
        for row in rows:
            if row[2] == key:
                return row[0], row[1]
        wanted = _words(name)
        candidates = []
        for row in rows:
            have = _words(row[1])
            shorter, longer = (wanted, have) if len(wanted) < len(have) else (have, wanted)
            if shorter and len(shorter) != len(longer) and longer[:len(shorter)] == shorter:
                candidates.append((row[0], row[1]))
        return candidates[0] if len(candidates) == 1 else None

    def resolve_name(self, name: str) -> Optional[str]:
        """The display name of the list this name means, or None."""
        with self._lock:
            found = self._resolve(name)
        return found[1] if found else None

    def _items(self, list_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT text, done FROM user_list_items WHERE list_id = ? ORDER BY position, id", (list_id,)).fetchall()
        return [{"text": r[0], "done": bool(r[1])} for r in rows]

    def _touch(self, list_id: int) -> None:
        self.conn.execute("UPDATE user_lists SET updated_at = ? WHERE id = ?", (time.time(), list_id))

    def add_items(self, name: str, items: Iterable[Any]) -> Dict[str, Any]:
        """Add items, creating the list when the name does not resolve."""
        display = _clean(name, NAME_MAX_CHARS)
        if not _key(display):
            raise ValueError("a list name is required")
        cleaned = _clean_items(items)
        added: List[str] = []
        already: List[str] = []
        reopened: List[str] = []
        with self._lock:
            found = self._resolve(display)
            now = time.time()
            if found is None:
                cur = self.conn.execute(
                    "INSERT INTO user_lists(name, key, created_at, updated_at) VALUES (?,?,?,?)",
                    (display, _key(display), now, now))
                found = (int(cur.lastrowid), display)
                debug_log("list created", "lists")
            list_id = found[0]
            for text in cleaned:
                row = self.conn.execute("SELECT id, done FROM user_list_items WHERE list_id = ? AND key = ?",
                                        (list_id, _key(text))).fetchone()
                if row is None:
                    position = self.conn.execute(
                        "SELECT COALESCE(MAX(position), -1) + 1 FROM user_list_items WHERE list_id = ?",
                        (list_id,)).fetchone()[0]
                    self.conn.execute(
                        "INSERT INTO user_list_items(list_id, text, key, done, position, created_at) "
                        "VALUES (?,?,?,0,?,?)", (list_id, text, _key(text), position, now))
                    added.append(text)
                elif row[1]:
                    self.conn.execute("UPDATE user_list_items SET done = 0 WHERE id = ?", (row[0],))
                    reopened.append(text)
                else:
                    already.append(text)
            self._touch(list_id)
            self.conn.commit()
            items_after = self._items(list_id)
        debug_log(f"list items added: {len(added)} new, {len(reopened)} reopened, {len(already)} present", "lists")
        return {"list": found[1], "added": added, "already_there": already, "reopened": reopened,
                "items": items_after}

    def _change_items(self, name: str, items: Iterable[Any], sql: str, field: str) -> Optional[Dict[str, Any]]:
        cleaned = _clean_items(items)
        changed: List[str] = []
        missing: List[str] = []
        with self._lock:
            found = self._resolve(name)
            if found is None:
                return None
            for text in cleaned:
                cur = self.conn.execute(sql, (found[0], _key(text)))
                (changed if cur.rowcount > 0 else missing).append(text)
            self._touch(found[0])
            self.conn.commit()
            items_after = self._items(found[0])
        debug_log(f"list items {field}: {len(changed)}, not found: {len(missing)}", "lists")
        return {"list": found[1], field: changed, "not_found": missing, "items": items_after}

    def remove_items(self, name: str, items: Iterable[Any]) -> Optional[Dict[str, Any]]:
        return self._change_items(name, items, "DELETE FROM user_list_items WHERE list_id = ? AND key = ?",
                                  "removed")

    def check_items(self, name: str, items: Iterable[Any]) -> Optional[Dict[str, Any]]:
        return self._change_items(name, items, "UPDATE user_list_items SET done = 1 WHERE list_id = ? AND key = ?",
                                  "checked")

    def show(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._resolve(name)
            if found is None:
                return None
            return {"list": found[1], "items": self._items(found[0])}

    def lists(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT l.name, "
                "COALESCE(SUM(CASE WHEN i.done = 0 THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN i.done = 1 THEN 1 ELSE 0 END), 0) "
                "FROM user_lists l LEFT JOIN user_list_items i ON i.list_id = l.id "
                "GROUP BY l.id ORDER BY l.id").fetchall()
        return [{"name": r[0], "open": int(r[1]), "done": int(r[2])} for r in rows]

    def clear(self, name: str) -> Optional[Dict[str, Any]]:
        """Remove every item and keep the list."""
        with self._lock:
            found = self._resolve(name)
            if found is None:
                return None
            cur = self.conn.execute("DELETE FROM user_list_items WHERE list_id = ?", (found[0],))
            self._touch(found[0])
            self.conn.commit()
        debug_log(f"list cleared: {cur.rowcount} items", "lists")
        return {"list": found[1], "removed_count": cur.rowcount}

    def delete_list(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._resolve(name)
            if found is None:
                return None
            self.conn.execute("DELETE FROM user_lists WHERE id = ?", (found[0],))
            self.conn.commit()
        debug_log("list deleted", "lists")
        return {"list": found[1]}

    # -- notes -------------------------------------------------------------------------------

    @staticmethod
    def _note(row) -> Dict[str, Any]:
        return {"id": int(row[0]), "text": row[1], "created": _iso(row[2])}

    def add_note(self, text: Any) -> Dict[str, Any]:
        cleaned = str(text if text is not None else "").strip()[:NOTE_MAX_CHARS]
        if not cleaned:
            raise ValueError("a note needs text")
        now = time.time()
        with self._lock:
            cur = self.conn.execute("INSERT INTO user_notes(text, created_at) VALUES (?, ?)", (cleaned, now))
            self.conn.commit()
        debug_log("note saved", "notes")
        return {"id": int(cur.lastrowid), "text": cleaned, "created": _iso(now)}

    def recent_notes(self, limit: int = 1, *, cap: int = SEARCH_MAX_LIMIT) -> List[Dict[str, Any]]:
        """The newest notes, newest first; ``cap`` bounds ``limit`` (the memory viewer lists more)."""
        limit = max(1, min(int(limit), cap))
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, text, created_at FROM user_notes ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._note(r) for r in rows]

    def search_notes(self, query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """Notes holding any of the query's words, those holding more of them first, then newest."""
        folded = _fold(query or "")
        words = list(dict.fromkeys(_LONG_WORD.findall(folded) or _WORD.findall(folded)))
        if not words:
            return []
        limit = max(1, min(int(limit), SEARCH_MAX_LIMIT))
        with self._lock:
            rows = self.conn.execute("SELECT id, text, created_at FROM user_notes").fetchall()
        scored = []
        for row in rows:
            text = _fold(row[1])
            score = sum(1 for word in words if word in text)
            if score:
                scored.append((score, row[2], row[0], row))
        scored.sort(key=lambda entry: (entry[0], entry[1], entry[2]), reverse=True)
        debug_log(f"notes searched: {len(scored)} matches", "notes")
        return [self._note(entry[3]) for entry in scored[:limit]]

    def get_note(self, note_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """The note with this ID, or the newest note when ``note_id`` is None."""
        with self._lock:
            if note_id is None:
                row = self.conn.execute(
                    "SELECT id, text, created_at FROM user_notes ORDER BY created_at DESC, id DESC LIMIT 1"
                ).fetchone()
            else:
                row = self.conn.execute("SELECT id, text, created_at FROM user_notes WHERE id = ?",
                                        (int(note_id),)).fetchone()
        return self._note(row) if row else None

    def delete_note(self, note_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """Delete the note with this ID, or the newest note; None when there is no such note."""
        with self._lock:
            note = self.get_note(note_id)
            if note is None:
                return None
            self.conn.execute("DELETE FROM user_notes WHERE id = ?", (note["id"],))
            self.conn.commit()
        debug_log("note deleted", "notes")
        return note

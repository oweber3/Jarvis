"""Web chat storage: projects, chats and their messages, in their own tables of the Jarvis database.

``ChatStore`` follows ``ActivityStore``: it owns its schema and its connection. Messages hold the
redacted text the dialogue memory holds, never what was typed. See ``webchat/webchat.spec.md``.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..debug import debug_log

NAME_MAX_CHARS = 80
TITLE_MAX_CHARS = 60
ROLES = ("user", "assistant")
SOURCES = ("typed", "voice", "confirmed")

# ``list_chats`` filters: every chat, or the chats that belong to no project.
ANY_PROJECT = object()
UNFILED = object()

_ACTIVE_KEY = "active_chat_id"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_projects (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  position   INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
  id         TEXT PRIMARY KEY,
  project_id TEXT REFERENCES chat_projects(id) ON DELETE SET NULL,
  title      TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_mode  TEXT NOT NULL DEFAULT '',
  last_model TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at);
CREATE TABLE IF NOT EXISTS chat_messages (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role    TEXT NOT NULL,
  content TEXT NOT NULL,
  ts      REAL NOT NULL,
  source  TEXT NOT NULL DEFAULT 'typed',
  private INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, id);
CREATE TABLE IF NOT EXISTS chat_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    position: int
    created_at: float


@dataclass(frozen=True)
class Chat:
    id: str
    project_id: Optional[str]
    title: str
    created_at: float
    updated_at: float
    last_mode: str
    last_model: str


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: str
    role: str
    content: str
    ts: float
    source: str
    private: bool = False


def _new_id() -> str:
    return secrets.token_hex(8)


def _clean_name(name: str) -> str:
    cleaned = " ".join(str(name).split())
    if not cleaned:
        raise ValueError("a name is required")
    return cleaned[:NAME_MAX_CHARS]


def _title_from(text: str) -> str:
    return " ".join(text.split())[:TITLE_MAX_CHARS]


class ChatStore:
    """Projects, chats and messages. Thread-safe: one connection guarded by a re-entrant lock."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        # Deleted chats are overwritten rather than left readable in free pages.
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

    # -- projects ----------------------------------------------------------------------------

    def create_project(self, name: str) -> Project:
        cleaned = _clean_name(name)
        now = time.time()
        with self._lock:
            row = self.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM chat_projects").fetchone()
            project = Project(_new_id(), cleaned, int(row[0]), now)
            self.conn.execute("INSERT INTO chat_projects(id, name, position, created_at) VALUES (?,?,?,?)",
                              (project.id, project.name, project.position, project.created_at))
            self.conn.commit()
        debug_log("web chat project created", "webchat")
        return project

    def list_projects(self) -> List[Project]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, position, created_at FROM chat_projects ORDER BY position, created_at").fetchall()
        return [Project(*r) for r in rows]

    def rename_project(self, project_id: str, name: str) -> bool:
        cleaned = _clean_name(name)
        with self._lock:
            cur = self.conn.execute("UPDATE chat_projects SET name = ? WHERE id = ?", (cleaned, project_id))
            self.conn.commit()
            return cur.rowcount > 0

    def delete_project(self, project_id: str) -> bool:
        """Delete a project; its chats stay and become unfiled."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM chat_projects WHERE id = ?", (project_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # -- chats -------------------------------------------------------------------------------

    def _project_exists(self, project_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM chat_projects WHERE id = ?", (project_id,)).fetchone()
        return row is not None

    def create_chat(self, project_id: Optional[str] = None, title: str = "") -> Chat:
        now = time.time()
        with self._lock:
            if project_id is not None and not self._project_exists(project_id):
                raise ValueError("unknown project")
            chat = Chat(_new_id(), project_id, _clean_name(title) if title.strip() else "", now, now, "", "")
            self.conn.execute(
                "INSERT INTO chats(id, project_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (chat.id, chat.project_id, chat.title, chat.created_at, chat.updated_at))
            self.conn.commit()
        debug_log("web chat created", "webchat")
        return chat

    _CHAT_COLUMNS = "id, project_id, title, created_at, updated_at, last_mode, last_model"

    def get_chat(self, chat_id: str) -> Optional[Chat]:
        with self._lock:
            row = self.conn.execute(f"SELECT {self._CHAT_COLUMNS} FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return Chat(*row) if row else None

    def list_chats(self, project=ANY_PROJECT) -> List[Chat]:
        """Chats, most recently active first: all of them, only the unfiled ones, or one project's."""
        query = f"SELECT {self._CHAT_COLUMNS} FROM chats"
        params: tuple = ()
        if project is UNFILED:
            query += " WHERE project_id IS NULL"
        elif project is not ANY_PROJECT:
            query += " WHERE project_id = ?"
            params = (project,)
        with self._lock:
            rows = self.conn.execute(query + " ORDER BY updated_at DESC, rowid DESC", params).fetchall()
        return [Chat(*r) for r in rows]

    def rename_chat(self, chat_id: str, title: str) -> bool:
        cleaned = _clean_name(title)
        with self._lock:
            cur = self.conn.execute("UPDATE chats SET title = ? WHERE id = ?", (cleaned, chat_id))
            self.conn.commit()
            return cur.rowcount > 0

    def move_chat(self, chat_id: str, project_id: Optional[str]) -> bool:
        """Put a chat in a project, or unfile it with ``None``. False for an unknown chat or project."""
        with self._lock:
            if project_id is not None and not self._project_exists(project_id):
                return False
            cur = self.conn.execute("UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id))
            self.conn.commit()
            return cur.rowcount > 0

    def set_last_model(self, chat_id: str, mode: str, model: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE chats SET last_mode = ?, last_model = ? WHERE id = ?", (mode, model, chat_id))
            self.conn.commit()

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            if self.get_active_chat_id() == chat_id:
                self.conn.execute("DELETE FROM chat_state WHERE key = ?", (_ACTIVE_KEY,))
            self.conn.commit()
            removed = cur.rowcount > 0
        if removed:
            debug_log("web chat deleted", "webchat")
        return removed

    # -- messages ----------------------------------------------------------------------------

    def append_message(self, chat_id: str, role: str, content: str, *, ts: float, source: str = "typed",
                       private: bool = False) -> Message:
        """Add a message. The first user message titles a chat that has no title yet.

        ``private`` marks a turn that quotes the activity log: it stays out of the diary and of any
        context that leaves the PC, also when the chat is opened again.
        """
        if role not in ROLES:
            raise ValueError("unknown role")
        if source not in SOURCES:
            raise ValueError("unknown source")
        with self._lock:
            chat = self.get_chat(chat_id)
            if chat is None:
                raise ValueError("unknown chat")
            cur = self.conn.execute(
                "INSERT INTO chat_messages(chat_id, role, content, ts, source, private) VALUES (?,?,?,?,?,?)",
                (chat_id, role, content, ts, source, 1 if private else 0))
            title = chat.title or (_title_from(content) if role == "user" else "")
            self.conn.execute("UPDATE chats SET updated_at = ?, title = ? WHERE id = ?",
                              (max(ts, time.time()), title, chat_id))
            self.conn.commit()
            return Message(int(cur.lastrowid), chat_id, role, content, ts, source, private)

    _MESSAGE_COLUMNS = "id, chat_id, role, content, ts, source, private"

    @staticmethod
    def _message(row) -> Message:
        return Message(row[0], row[1], row[2], row[3], row[4], row[5], bool(row[6]))

    def messages(self, chat_id: str, after_id: int = 0) -> List[Message]:
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {self._MESSAGE_COLUMNS} FROM chat_messages WHERE chat_id = ? AND id > ? ORDER BY id",
                (chat_id, after_id)).fetchall()
        return [self._message(r) for r in rows]

    def last_messages(self, chat_id: str, limit: int) -> List[Message]:
        """The newest ``limit`` messages, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {self._MESSAGE_COLUMNS} FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, max(0, limit))).fetchall()
        return [self._message(r) for r in reversed(rows)]

    # -- state -------------------------------------------------------------------------------

    def get_active_chat_id(self) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT value FROM chat_state WHERE key = ?", (_ACTIVE_KEY,)).fetchone()
        return row[0] if row else None

    def set_active_chat_id(self, chat_id: Optional[str]) -> None:
        with self._lock:
            if chat_id is None:
                self.conn.execute("DELETE FROM chat_state WHERE key = ?", (_ACTIVE_KEY,))
            else:
                self.conn.execute("INSERT OR REPLACE INTO chat_state(key, value) VALUES (?, ?)", (_ACTIVE_KEY, chat_id))
            self.conn.commit()

    def delete_all(self) -> None:
        """Remove every project, chat and message."""
        with self._lock:
            self.conn.execute("DELETE FROM chat_messages")
            self.conn.execute("DELETE FROM chats")
            self.conn.execute("DELETE FROM chat_projects")
            self.conn.execute("DELETE FROM chat_state")
            self.conn.commit()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass
        debug_log("web chat history deleted", "webchat")

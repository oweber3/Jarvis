"""The document index: its own tables in the Jarvis SQLite database, and vector search over them.

See ``documents.spec.md`` (Storage).
"""
from __future__ import annotations

import heapq
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

STATUS_INDEXED = "indexed"
STATUS_SKIPPED = "skipped"
PER_FILE_HITS = 2
_BATCH = 2000

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS document_files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    chunks INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS document_chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    page INTEGER,
    text TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS document_chunks_file ON document_chunks(file_id);
CREATE TABLE IF NOT EXISTS document_index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class FileRecord(NamedTuple):
    mtime: float
    size: int
    status: str


class Hit(NamedTuple):
    path: str
    page: Optional[int]
    text: str
    score: float


def index_exists(db_path: str) -> bool:
    """Whether the database file holds the index tables (without creating anything)."""
    if db_path != ":memory:" and not Path(db_path).exists():
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_files'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


class DocumentStore:
    """Indexed files and their passages, with one embedding vector per passage."""

    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        # Deleted passages are overwritten rather than left readable in free pages.
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

    # ── Meta ──────────────────────────────────────────────────────────────

    def model(self) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT value FROM document_index_meta WHERE key = 'model'").fetchone()
        return row[0] if row else None

    def set_model(self, model: str) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO document_index_meta(key, value) VALUES ('model', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (model,))
            self.conn.commit()

    # ── Files ─────────────────────────────────────────────────────────────

    def files(self) -> Dict[str, FileRecord]:
        with self._lock:
            rows = self.conn.execute("SELECT path, mtime, size, status FROM document_files").fetchall()
        return {path: FileRecord(mtime, size, status) for path, mtime, size, status in rows}

    def replace_file(self, path: str, mtime: float, size: int,
                     chunks: Sequence[Tuple[Optional[int], str, Sequence[float]]],
                     status: str = STATUS_INDEXED) -> None:
        """Store ``path`` with ``chunks`` (page, text, vector), replacing what was stored for it, in one go."""
        with self._lock:
            try:
                file_id = self._remove(path)
                cur = self.conn.execute(
                    "INSERT INTO document_files(path, mtime, size, status, chunks, indexed_at) VALUES (?,?,?,?,?,?)",
                    (path, float(mtime), int(size), status, len(chunks), time.time()))
                file_id = cur.lastrowid
                self.conn.executemany(
                    "INSERT INTO document_chunks(file_id, seq, page, text, vector) VALUES (?,?,?,?,?)",
                    [(file_id, seq, page, text, np.asarray(vector, dtype=np.float32).tobytes())
                     for seq, (page, text, vector) in enumerate(chunks)])
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def chunk_count(self, path: str) -> int:
        with self._lock:
            row = self.conn.execute("SELECT chunks FROM document_files WHERE path = ?", (path,)).fetchone()
        return int(row[0]) if row else 0

    def mark_skipped(self, path: str, mtime: float, size: int) -> None:
        self.replace_file(path, mtime, size, [], status=STATUS_SKIPPED)

    def remove_file(self, path: str) -> None:
        with self._lock:
            self._remove(path)
            self.conn.commit()

    def _remove(self, path: str) -> Optional[int]:
        row = self.conn.execute("SELECT id FROM document_files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return None
        self.conn.execute("DELETE FROM document_chunks WHERE file_id = ?", (row[0],))
        self.conn.execute("DELETE FROM document_files WHERE id = ?", (row[0],))
        return row[0]

    def clear_passages(self) -> None:
        """Forget every indexed file and passage (the vectors no longer fit the embedding model)."""
        with self._lock:
            self.conn.execute("DELETE FROM document_chunks")
            self.conn.execute("DELETE FROM document_files")
            self.conn.commit()

    def delete_all(self) -> int:
        """Delete every row of the index; returns the number of passages removed."""
        with self._lock:
            removed = self.conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            self.conn.execute("DELETE FROM document_chunks")
            self.conn.execute("DELETE FROM document_files")
            self.conn.execute("DELETE FROM document_index_meta")
            self.conn.commit()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        return int(removed)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            files = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(status = ?), 0) FROM document_files", (STATUS_INDEXED,)).fetchone()
            chunks = self.conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        return {"files": int(files[0]), "indexed_files": int(files[1]), "chunks": int(chunks)}

    # ── Search ────────────────────────────────────────────────────────────

    def search(self, query_vector: Sequence[float], limit: int = 5) -> List[Hit]:
        """The best passages by cosine similarity, at most ``PER_FILE_HITS`` per file, best first."""
        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if limit <= 0 or norm == 0.0:
            return []
        query = query / norm
        width = query.shape[0] * 4
        pool: List[Tuple[float, int, int]] = []  # min-heap of (score, chunk id, file id)
        keep = max(limit * 4, 20)
        with self._lock:
            cursor = self.conn.execute("SELECT id, file_id, vector FROM document_chunks")
            while True:
                rows = cursor.fetchmany(_BATCH)
                if not rows:
                    break
                kept = [(cid, fid, blob) for cid, fid, blob in rows if len(blob) == width]
                if not kept:
                    continue
                matrix = np.frombuffer(b"".join(blob for _, _, blob in kept), dtype=np.float32)
                matrix = matrix.reshape(len(kept), query.shape[0])
                norms = np.linalg.norm(matrix, axis=1)
                norms[norms == 0.0] = 1.0
                scores = (matrix @ query) / norms
                for (cid, fid, _), score in zip(kept, scores.tolist()):
                    item = (score, cid, fid)
                    if len(pool) < keep:
                        heapq.heappush(pool, item)
                    elif item > pool[0]:
                        heapq.heapreplace(pool, item)
            ranked = sorted(pool, reverse=True)
            chosen: List[Tuple[float, int]] = []
            per_file: Dict[int, int] = {}
            for score, cid, fid in ranked:
                if per_file.get(fid, 0) >= PER_FILE_HITS:
                    continue
                per_file[fid] = per_file.get(fid, 0) + 1
                chosen.append((score, cid))
                if len(chosen) >= limit:
                    break
            return [self._hit(cid, score) for score, cid in chosen]

    def _hit(self, chunk_id: int, score: float) -> Hit:
        path, page, text = self.conn.execute(
            "SELECT f.path, c.page, c.text FROM document_chunks c JOIN document_files f ON f.id = c.file_id "
            "WHERE c.id = ?", (chunk_id,)).fetchone()
        return Hit(path, page, text, round(float(score), 4))


def remove_missing(store: DocumentStore, keep: Iterable[str]) -> int:
    """Remove stored files whose path is not in ``keep``; returns how many were removed."""
    wanted = set(keep)
    gone = [path for path in store.files() if path not in wanted]
    for path in gone:
        store.remove_file(path)
    return len(gone)

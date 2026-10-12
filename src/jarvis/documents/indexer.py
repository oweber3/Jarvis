"""Which files document search may read, and the incremental indexer that embeds them.

``scan_folders`` lists eligible files; ``DocumentIndexer.run_pass`` brings the store up to date with them,
and ``DocumentIndexer.start`` repeats that on a low-priority background thread. See ``documents.spec.md``.
"""
from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..debug import debug_log
from . import extract
from .store import DocumentStore, remove_missing

MAX_FILES = 20_000
MAX_TOTAL_CHUNKS = 50_000
RETRY_SEC = 300.0
BUSY_WAIT_SEC = 2.0
DEFAULT_PAUSE_SEC = 0.2

FILE_ATTRIBUTE_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
FILE_ATTRIBUTE_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
FILE_ATTRIBUTE_OFFLINE = getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0x1000)
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
_HIDDEN_OR_SYSTEM = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
# Build, package and system folders: never the user's own documents, and often huge.
SKIPPED_FOLDERS = frozenset({"node_modules", "__pycache__", "$recycle.bin", "system volume information"})


# ── Eligible files ────────────────────────────────────────────────────────

def resolve_folders(entries: Iterable[str]) -> Tuple[List[Path], int]:
    """The configured folders that exist inside the home folder, and how many entries were rejected.

    Entries are read like ``localFiles`` paths (known folder names, ``~``, home-relative or full paths)."""
    from ..tools.builtin.local_files import inside_home, resolve_path

    folders: List[Path] = []
    rejected = 0
    for entry in entries:
        try:
            folder = resolve_path(entry)
        except (PermissionError, OSError, ValueError):
            rejected += 1
            continue
        if not inside_home(folder) or not folder.is_dir():
            rejected += 1
            continue
        if folder not in folders:
            folders.append(folder)
    return folders, rejected


def _attributes(entry: os.DirEntry) -> int:
    """Windows file attributes of ``entry`` (0 elsewhere or when unreadable)."""
    try:
        return int(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return 0


@dataclass
class ScanResult:
    files: Dict[Path, Tuple[float, int]] = field(default_factory=dict)  # path -> (mtime, size)
    truncated: bool = False
    cloud_only: int = 0
    stopped: bool = False


def scan_folders(folders: Sequence[Path], should_stop: Callable[[], bool] = lambda: False) -> ScanResult:
    """Eligible files under ``folders``: supported, visible, local (not cloud-only), links not followed.

    ``should_stop`` is checked per folder; when it turns true the scan ends early, marked ``stopped``."""
    result = ScanResult()
    pending = [Path(f) for f in folders]
    seen_dirs = set()
    while pending:
        if should_stop():
            result.stopped = True
            return result
        folder = pending.pop()
        key = os.path.normcase(str(folder))
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith((".", "~$")):
                continue
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = not is_dir and entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            attributes = _attributes(entry)
            if attributes & _HIDDEN_OR_SYSTEM:
                continue
            if is_dir:
                if name.casefold() not in SKIPPED_FOLDERS and not attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    pending.append(Path(entry.path))
                continue
            if not is_file or Path(name).suffix.lower() not in extract.SUPPORTED_SUFFIXES:
                continue
            if attributes & _CLOUD_ONLY:
                result.cloud_only += 1
                continue
            if len(result.files) >= MAX_FILES:
                result.truncated = True
                return result
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            result.files[Path(entry.path)] = (info.st_mtime, info.st_size)
    return result


# ── Indexing ──────────────────────────────────────────────────────────────

@dataclass
class PassReport:
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    passages: int = 0
    truncated: bool = False
    # None (finished), "stopped", "embedding_unavailable" or "index_full"
    stopped: Optional[str] = None


class _Halt(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DocumentIndexer:
    """Brings a ``DocumentStore`` up to date with the eligible files under ``folders``."""

    def __init__(self, store: DocumentStore, folders: Sequence[Path], embed: Callable[[str], Optional[List[float]]],
                 *, model: str, max_file_bytes: int = 20 * 1024 * 1024, busy: Callable[[], bool] = lambda: False,
                 rescan_sec: float = 3600.0, pause_sec: float = DEFAULT_PAUSE_SEC,
                 sleep: Optional[Callable[[float], object]] = None) -> None:
        self.store = store
        self.folders = [Path(f) for f in folders]
        self._embed = embed
        self._model = model
        self._max_file_bytes = int(max_file_bytes)
        self._busy = busy
        self._rescan_sec = float(rescan_sec)
        self._pause_sec = float(pause_sec)
        self._halt = threading.Event()        # set to end the current pass early (stop or interrupt)
        self._stopping = False
        self._sleep = sleep or self._halt.wait
        self._wake = threading.Event()
        self._pass_lock = threading.Lock()
        self._cond = threading.Condition()
        self._requested = 1                   # passes asked for (the first one at start)
        self._completed = 0                   # passes finished, interrupted ones included
        self._in_pass = False
        self._halt_requests = 0
        self._thread: Optional[threading.Thread] = None
        self.last_report: Optional[PassReport] = None

    # ── One pass ──────────────────────────────────────────────────────────

    @property
    def indexing(self) -> bool:
        return self._in_pass

    def request_stop(self) -> None:
        self._stopping = True
        self._halt.set()
        self._wake.set()

    def _check(self) -> None:
        if self._halt.is_set():
            raise _Halt("stopped")

    def _wait_until_quiet(self) -> None:
        while self._busy():
            self._check()
            self._sleep(BUSY_WAIT_SEC)
        self._check()

    def run_pass(self) -> PassReport:
        report = PassReport()
        with self._pass_lock:
            self._in_pass = True
            try:
                self._run(report)
            except _Halt as halt:
                report.stopped = halt.reason
            finally:
                self._in_pass = False
        self.last_report = report
        debug_log(f"document pass: {report.scanned} files, {report.indexed} indexed, {report.unchanged} unchanged, "
                  f"{report.removed} removed, {report.skipped} skipped, {report.passages} passages, "
                  f"stopped={report.stopped or 'no'}", "documents")
        return report

    def _run(self, report: PassReport) -> None:
        self._check()
        stored_model = self.store.model()
        if stored_model != self._model:
            if stored_model is not None:
                debug_log("embedding model changed; document index cleared", "documents")
                self.store.clear_passages()
            self.store.set_model(self._model)
        scan = scan_folders(self.folders, self._halt.is_set)
        self._check()  # an interrupted scan is incomplete: never remove files on its account
        report.scanned = len(scan.files)
        report.truncated = scan.truncated
        eligible = {str(path): info for path, info in scan.files.items()}
        report.removed = remove_missing(self.store, eligible)
        stored = self.store.files()
        total = self.store.stats()["chunks"]
        for path in sorted(eligible):
            mtime, size = eligible[path]
            record = stored.get(path)
            if record is not None and record.mtime == mtime and record.size == size:
                report.unchanged += 1
                continue
            self._wait_until_quiet()
            previous = self.store.chunk_count(path) if record is not None else 0
            if size > self._max_file_bytes:
                self.store.mark_skipped(path, mtime, size)
                total -= previous
                report.skipped += 1
                continue
            try:
                chunks = extract.chunk_segments(extract.extract_segments(Path(path)))
            except extract.ExtractError as exc:
                debug_log(f"document skipped: {exc.args[0] if exc.args else 'unreadable'}", "documents")
                chunks = []
            if not chunks:
                self.store.mark_skipped(path, mtime, size)
                total -= previous
                report.skipped += 1
                continue
            if total - previous + len(chunks) > MAX_TOTAL_CHUNKS:
                raise _Halt("index_full")
            stem = Path(path).stem
            embedded = []
            for chunk in chunks:
                self._wait_until_quiet()
                vector = self._embed(f"{stem}\n{chunk.text}")
                if not vector:
                    raise _Halt("embedding_unavailable")
                embedded.append((chunk.page, chunk.text, vector))
            self.store.replace_file(path, mtime, size, embedded)
            total += len(embedded) - previous
            report.indexed += 1
            report.passages += len(embedded)
            if self._pause_sec > 0:
                self._sleep(self._pause_sec)

    # ── Index changes while running ───────────────────────────────────────

    def _with_pass_halted(self, action: Callable[[], object]) -> object:
        """Run ``action`` with no pass in progress (an ongoing pass ends at its next passage)."""
        with self._cond:
            self._halt_requests += 1
            self._halt.set()
        try:
            with self._pass_lock:
                return action()
        finally:
            with self._cond:
                self._halt_requests -= 1
                # Overlapping requests keep the halt until the last one has run.
                if self._halt_requests == 0 and not self._stopping:
                    self._halt.clear()

    def delete(self) -> int:
        """Delete the whole index now. The next scheduled pass builds it again."""
        return int(self._with_pass_halted(self.store.delete_all))

    def rebuild(self) -> None:
        """Delete the whole index and start a fresh pass."""
        self._with_pass_halted(self.store.delete_all)
        with self._cond:
            self._requested += 1
        self._wake.set()

    def wait_idle(self, timeout: float) -> bool:
        """Wait until every pass asked for so far has finished."""
        with self._cond:
            target = self._requested
            return self._cond.wait_for(lambda: self._completed >= target, timeout)

    # ── Background thread ─────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._stopping = False
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="document-indexer")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop the thread; returns whether it has ended."""
        self.request_stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return not self.running

    def _loop(self) -> None:
        _lower_thread_priority()
        while not self._stopping:
            with self._cond:
                target = self._requested
            self._wake.clear()
            try:
                report = self.run_pass()
            except Exception as exc:  # a broken database must not kill the daemon
                debug_log(f"document pass failed: {type(exc).__name__}", "documents")
                report = PassReport(stopped="error")
            with self._cond:
                self._completed = max(self._completed, target)
                self._cond.notify_all()
            if self._stopping:
                break
            wait = RETRY_SEC if report.stopped in ("embedding_unavailable", "error") else self._rescan_sec
            with self._cond:
                pending = self._requested > self._completed
            if not pending:
                self._wake.wait(wait)


def _lower_thread_priority() -> None:
    """Below-normal scheduling priority for the indexer thread on Windows; nothing elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -2)  # THREAD_PRIORITY_LOWEST
    except Exception as exc:
        debug_log(f"document indexer priority unchanged: {type(exc).__name__}", "documents")

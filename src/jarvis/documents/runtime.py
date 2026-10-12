"""Runs opt-in document search inside the daemon: the background indexer, search, rebuild and delete.

Nothing starts unless ``document_search_enabled`` is true. The tray (bundled or subprocess), the tool and
the CLI share these entry points. See ``documents.spec.md``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from ..debug import debug_log
from .indexer import DocumentIndexer, resolve_folders
from .store import DocumentStore, Hit, index_exists

EMBED_TIMEOUT_SEC = 15.0

_service: Optional[DocumentIndexer] = None
_service_lock = threading.Lock()


def get_service() -> Optional[DocumentIndexer]:
    return _service


def enabled(cfg: Any) -> bool:
    return getattr(cfg, "document_search_enabled", False) is True


def _embedder(cfg: Any) -> Callable[[str], Optional[List[float]]]:
    """Embeds one text with the configured embedding backend; None on any failure."""
    from ..llm import get_embedding_backend
    backend = get_embedding_backend(cfg)
    model = str(getattr(cfg, "embedding_model", "") or "")

    def embed(text: str) -> Optional[List[float]]:
        if not model:
            return None
        try:
            return backend.embed(text, model, timeout_sec=EMBED_TIMEOUT_SEC)
        except Exception:
            return None

    return embed


def _embed_query(cfg: Any, text: str) -> Optional[List[float]]:
    return _embedder(cfg)(text)


def _busy() -> bool:
    """Jarvis is listening, thinking, speaking or dictating: indexing waits."""
    from ..assistant_state import AssistantState, current
    return current() not in (AssistantState.IDLE, AssistantState.ASLEEP)


def start(cfg: Any) -> Optional[DocumentIndexer]:
    """Start background indexing when the user has opted in. Returns the running indexer, or None."""
    global _service
    if not enabled(cfg):
        return None
    stop()
    folders, rejected = resolve_folders(getattr(cfg, "document_search_folders", None) or [])
    if rejected:
        print(f"⚠️ Document search: {rejected} folder(s) skipped (missing, or outside your home folder)",
              flush=True)
    if not folders:
        print("📚 Document search is on, but no folder can be indexed", flush=True)
        debug_log("document search: no usable folders", "documents")
        return None
    indexer = DocumentIndexer(
        DocumentStore(cfg.db_path), folders, _embedder(cfg),
        model=str(getattr(cfg, "embedding_model", "") or ""),
        max_file_bytes=int(getattr(cfg, "document_search_max_file_mb", 20)) * 1024 * 1024,
        rescan_sec=float(getattr(cfg, "document_search_rescan_minutes", 60)) * 60.0,
        busy=_busy)
    with _service_lock:
        _service = indexer
    indexer.start()
    print(f"📚 Document search on: indexing {len(folders)} folder(s) in the background", flush=True)
    debug_log(f"document search started: {len(folders)} folders", "documents")
    return indexer


def stop() -> None:
    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        if service.stop():
            service.store.close()  # a thread still finishing a passage keeps its connection until it exits
        debug_log("document search stopped", "documents")


def _load_cfg(cfg: Any) -> Any:
    if cfg is None:
        from ..config import load_settings
        cfg = load_settings()
    return cfg


def rebuild(cfg: Any = None) -> None:
    """Delete the index and index again (at once when running, otherwise at the next start)."""
    service = _service
    if service is not None:
        service.rebuild()
        print("🔄 Document index rebuilding in the background", flush=True)
    else:
        delete_index(cfg)
    debug_log("document index rebuild requested", "documents")


def delete_index(cfg: Any = None) -> int:
    """Delete every stored passage. Works whether or not indexing is running."""
    service = _service
    if service is not None:
        removed = service.delete()
    else:
        db_path = _load_cfg(cfg).db_path
        removed = 0
        if index_exists(db_path):  # never creates the tables just to empty them
            store = DocumentStore(db_path)
            try:
                removed = store.delete_all()
            finally:
                store.close()
    print(f"🗑️ Document index deleted ({removed} passages)", flush=True)
    debug_log(f"document index deleted: {removed} passages", "documents")
    return removed


# Set when the user switches cloud sharing off while Jarvis runs; switching it on waits for a restart.
_sharing_stopped = False


def stop_cloud_sharing() -> None:
    """Withhold document search from Codex and Claude from now on, before the saved setting is reloaded."""
    global _sharing_stopped
    _sharing_stopped = True
    print("🔒 Document search no longer shared with Codex or Claude", flush=True)
    debug_log("document search cloud sharing stopped", "documents")


def shared_with_cloud(cfg: Any) -> bool:
    """Whether Codex and Claude may search the documents: only a real ``True``, and not stopped since."""
    return getattr(cfg, "document_search_share_with_cloud", False) is True and not _sharing_stopped


@dataclass
class SearchResult:
    available: bool                      # False when the query could not be embedded
    hits: List[Hit] = field(default_factory=list)
    indexed_files: int = 0
    passages: int = 0
    indexing: bool = False


def search(cfg: Any, query: str, limit: int) -> SearchResult:
    """The passages that best match ``query``."""
    service = _service
    indexing = bool(service is not None and service.indexing)
    if service is None and not index_exists(cfg.db_path):
        return SearchResult(True, [], 0, 0, False)
    store = service.store if service is not None else DocumentStore(cfg.db_path)
    try:
        stats = store.stats()
        if stats["chunks"] == 0:
            return SearchResult(True, [], stats["indexed_files"], 0, indexing)
        vector = _embed_query(cfg, query)
        if not vector:
            debug_log("document search: query embedding unavailable", "documents")
            return SearchResult(False, [], stats["indexed_files"], stats["chunks"], indexing)
        hits = store.search(vector, limit)
    finally:
        if service is None:
            store.close()
    debug_log(f"document search: {len(hits)} passages from {stats['chunks']}", "documents")
    return SearchResult(True, hits, stats["indexed_files"], stats["chunks"], indexing)

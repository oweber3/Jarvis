"""Document search diagnostic: ``python -m jarvis.documents <status | index | search QUERY | delete>``.

``--folder`` (repeatable) and ``--db`` override the configured folders and database, so indexing can be
checked on a test folder. Works with document search off. See ``documents.spec.md`` (CLI).
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(prog="python -m jarvis.documents", description="Jarvis document search")
    parser.add_argument("command", choices=["status", "index", "search", "delete"])
    parser.add_argument("query", nargs="*", help="search: what to look for")
    parser.add_argument("--folder", action="append", help="folder to index instead of the configured ones")
    parser.add_argument("--db", help="database file instead of the configured one")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    from ..config import load_settings
    from . import runtime
    from .indexer import DocumentIndexer, resolve_folders
    from .store import DocumentStore

    cfg = load_settings()
    if args.db:
        cfg = dataclasses.replace(cfg, db_path=str(Path(args.db).resolve()))
    folders_wanted = args.folder or cfg.document_search_folders

    if args.command == "delete":
        runtime.delete_index(cfg)
        return 0

    store = DocumentStore(cfg.db_path)
    try:
        if args.command == "status":
            stats = store.stats()
            print("📚 Document index")
            print(f"  📄 Documents indexed: {stats['indexed_files']} (of {stats['files']} seen)")
            print(f"  🧩 Passages: {stats['chunks']}")
            print(f"  🧠 Embedding model: {store.model() or 'none yet'}")
            print(f"  🔘 Document search: {'on' if runtime.enabled(cfg) else 'off'}")
            return 0
        if args.command == "index":
            folders, rejected = resolve_folders(folders_wanted)
            if rejected:
                print(f"⚠️ {rejected} folder(s) skipped (missing, or outside your home folder)")
            if not folders:
                print("❌ No folder can be indexed")
                return 1
            indexer = DocumentIndexer(store, folders, runtime._embedder(cfg), model=cfg.embedding_model,
                                      max_file_bytes=cfg.document_search_max_file_mb * 1024 * 1024, pause_sec=0)
            started = time.monotonic()
            print(f"🔎 Indexing {len(folders)} folder(s)...")
            report = indexer.run_pass()
            print(f"✅ Pass finished in {time.monotonic() - started:.1f}s" if report.stopped is None
                  else f"⚠️ Pass stopped: {report.stopped}")
            print(f"  📄 Eligible files: {report.scanned}{' (limit reached)' if report.truncated else ''}")
            print(f"  ➕ Indexed: {report.indexed} ({report.passages} passages)")
            print(f"  💤 Unchanged: {report.unchanged}")
            print(f"  ⏭️ Skipped: {report.skipped}")
            print(f"  🗑️ Removed: {report.removed}")
            return 0 if report.stopped is None else 1
        query = " ".join(args.query).strip()
        if not query:
            print("❌ Give something to search for")
            return 2
    finally:
        store.close()

    result = runtime.search(cfg, query, max(1, min(8, args.limit)))
    if not result.available:
        print("❌ The embedding model is not available")
        return 1
    print(f"🔎 {len(result.hits)} passage(s) from {result.passages}")
    for number, hit in enumerate(result.hits, start=1):
        page = f", page {hit.page}" if hit.page else ""
        print(f"  [{number}] 📄 {Path(hit.path).name}{page}  (score {hit.score})")
        print(f"      {hit.text[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

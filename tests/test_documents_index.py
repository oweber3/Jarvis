"""Document search: eligible files, the index store, and incremental indexing with a fake embedder."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from jarvis.documents import indexer as idx
from jarvis.documents.store import DocumentStore
from pdf_fixtures import make_pdf

DIM = 64


def fake_embed(text: str):
    """Bag of words hashed into a small vector: texts sharing words are close."""
    vector = [0.0] * DIM
    for word in re.findall(r"\w+", text.casefold()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
    return vector


class CountingEmbed:
    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    def __call__(self, text):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            return None
        self.calls.append(text)
        return fake_embed(text)


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "home" / "Documents"
    root.mkdir(parents=True)
    (root / "lease.txt").write_text("The tenant may keep one cat. Dogs need the landlord's written consent.",
                                    encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "thermo.md").write_text("Thermodynamics exam: revise entropy and the Carnot cycle.",
                                              encoding="utf-8")
    make_pdf(root / "recipe.pdf", ["Pancakes need flour, eggs and milk.", "Fry them in butter."])
    return root


@pytest.fixture
def store(tmp_path):
    s = DocumentStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


def make_indexer(store, folders, embed=None, **kw):
    kw.setdefault("pause_sec", 0)
    return idx.DocumentIndexer(store, folders, embed or CountingEmbed(), model="fake-embed", **kw)


@pytest.mark.unit
class TestScan:
    def test_finds_supported_files_only(self, docs):
        (docs / "photo.jpg").write_bytes(b"\xff\xd8")
        names = sorted(p.name for p in idx.scan_folders([docs]).files)
        assert names == ["lease.txt", "recipe.pdf", "thermo.md"]

    def test_skips_hidden_build_and_lock_files(self, docs):
        for folder in (".git", "node_modules", "__pycache__"):
            (docs / folder).mkdir()
            (docs / folder / "secret.txt").write_text("x", encoding="utf-8")
        (docs / "~$lease.docx").write_bytes(b"lock")
        (docs / ".hidden.md").write_text("x", encoding="utf-8")
        names = sorted(p.name for p in idx.scan_folders([docs]).files)
        assert names == ["lease.txt", "recipe.pdf", "thermo.md"]

    def test_skips_cloud_only_hidden_and_system_by_attribute(self, docs, monkeypatch):
        flagged = {"lease.txt": idx.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, "thermo.md": idx.FILE_ATTRIBUTE_HIDDEN,
                   "notes": 0}

        def attributes(entry):
            return flagged.get(entry.name, 0)

        monkeypatch.setattr(idx, "_attributes", attributes)
        names = sorted(p.name for p in idx.scan_folders([docs]).files)
        assert names == ["recipe.pdf"]
        flagged["notes"] = idx.FILE_ATTRIBUTE_SYSTEM
        flagged["thermo.md"] = 0
        assert "thermo.md" not in [p.name for p in idx.scan_folders([docs]).files]

    def test_the_file_count_is_capped(self, docs, monkeypatch):
        monkeypatch.setattr(idx, "MAX_FILES", 2)
        result = idx.scan_folders([docs])
        assert len(result.files) == 2 and result.truncated is True

    def test_configured_folders_stay_inside_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "Documents").mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))
        folders, rejected = idx.resolve_folders([str(home / "Documents"), str(outside), "~/missing",
                                                 "\\\\server\\share"])
        assert folders == [(home / "Documents").resolve()]
        assert rejected == 3


@pytest.mark.unit
class TestStore:
    def test_tables_sit_beside_existing_ones(self, tmp_path):
        path = tmp_path / "jarvis.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE other(x)")
        conn.execute("INSERT INTO other VALUES (1)")
        conn.commit()
        conn.close()
        DocumentStore(str(path)).close()
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT x FROM other").fetchall() == [(1,)]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"document_files", "document_chunks", "document_index_meta"} <= tables
        conn.close()

    def test_search_ranks_by_meaning_and_limits_per_file(self, store):
        store.replace_file("/a.txt", 1.0, 10, [(None, f"cats and dogs {i}", fake_embed(f"cats and dogs {i}"))
                                               for i in range(4)])
        store.replace_file("/b.txt", 1.0, 10, [(None, "soup recipe", fake_embed("soup recipe"))])
        hits = store.search(fake_embed("cats dogs"), limit=5)
        assert [h.path for h in hits].count("/a.txt") == 2
        assert hits[0].path == "/a.txt" and hits[0].score >= hits[-1].score

    def test_vectors_of_another_length_are_ignored(self, store):
        store.replace_file("/a.txt", 1.0, 10, [(None, "short", [1.0, 0.0])])
        store.replace_file("/b.txt", 1.0, 10, [(None, "right", fake_embed("right"))])
        assert [h.path for h in store.search(fake_embed("right"), limit=5)] == ["/b.txt"]

    def test_replace_and_remove(self, store):
        store.replace_file("/a.txt", 1.0, 10, [(None, "old", fake_embed("old"))])
        store.replace_file("/a.txt", 2.0, 11, [(None, "new", fake_embed("new"))])
        assert [h.text for h in store.search(fake_embed("old new"), limit=5)] == ["new"]
        store.remove_file("/a.txt")
        assert store.search(fake_embed("new"), limit=5) == [] and store.stats()["files"] == 0

    def test_delete_all_leaves_no_readable_text(self, tmp_path):
        path = tmp_path / "jarvis.db"
        s = DocumentStore(str(path))
        s.replace_file("/lease.txt", 1.0, 10, [(None, "zebra-secret-clause", fake_embed("zebra"))])
        s.delete_all()
        s.close()
        assert b"zebra-secret-clause" not in path.read_bytes()
        assert not any(p.read_bytes().count(b"zebra-secret-clause") for p in tmp_path.glob("jarvis.db-*"))


@pytest.mark.unit
class TestIndexer:
    def test_first_pass_indexes_every_file(self, store, docs):
        report = make_indexer(store, [docs]).run_pass()
        assert report.indexed == 3 and report.stopped is None
        hit = store.search(fake_embed("cat dogs landlord consent"), limit=1)[0]
        assert Path(hit.path).name == "lease.txt"
        pdf_hit = store.search(fake_embed("fry butter"), limit=1)[0]
        assert Path(pdf_hit.path).name == "recipe.pdf" and pdf_hit.page == 2

    def test_unchanged_files_are_not_reread(self, store, docs, monkeypatch):
        make_indexer(store, [docs]).run_pass()
        opened = []
        real = idx.extract.extract_segments
        monkeypatch.setattr(idx.extract, "extract_segments", lambda p: opened.append(p) or real(p))
        embed = CountingEmbed()
        report = make_indexer(store, [docs], embed).run_pass()
        assert opened == [] and embed.calls == [] and report.unchanged == 3

    def test_changed_file_is_reembedded_and_deleted_file_removed(self, store, docs):
        make_indexer(store, [docs]).run_pass()
        lease = docs / "lease.txt"
        lease.write_text("No animals at all in the flat.", encoding="utf-8")
        os.utime(lease, (time.time() + 10, time.time() + 10))
        (docs / "notes" / "thermo.md").unlink()
        report = make_indexer(store, [docs]).run_pass()
        assert report.indexed == 1 and report.removed == 1
        assert store.search(fake_embed("animals flat"), limit=1)[0].text.startswith("No animals")
        assert store.stats()["files"] == 2

    def test_removed_folder_drops_its_files(self, store, docs):
        make_indexer(store, [docs]).run_pass()
        report = make_indexer(store, [docs / "notes"]).run_pass()
        assert report.removed == 2 and store.stats()["files"] == 1

    def test_large_and_empty_files_are_skipped(self, store, docs):
        (docs / "big.txt").write_text("x" * 50000, encoding="utf-8")
        (docs / "empty.md").write_text("   ", encoding="utf-8")
        (docs / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
        report = make_indexer(store, [docs], max_file_bytes=20000).run_pass()
        assert report.skipped == 3 and report.indexed == 3
        # Skipped files are not retried until they change.
        assert make_indexer(store, [docs], max_file_bytes=20000).run_pass().skipped == 0

    def test_embedding_failure_stops_and_the_next_pass_resumes(self, store, docs):
        report = make_indexer(store, [docs], CountingEmbed(fail_after=1)).run_pass()
        assert report.stopped == "embedding_unavailable"
        assert report.indexed <= 1
        report = make_indexer(store, [docs]).run_pass()
        assert report.stopped is None and store.stats()["files"] == 3

    def test_a_model_change_rebuilds(self, store, docs):
        make_indexer(store, [docs]).run_pass()
        embed = CountingEmbed()
        idx.DocumentIndexer(store, [docs], embed, model="other-model", pause_sec=0).run_pass()
        assert len(embed.calls) >= 3

    def test_waits_while_jarvis_is_busy(self, store, docs):
        states = iter([True, True, False] + [False] * 100)
        waits = []
        indexer = make_indexer(store, [docs], busy=lambda: next(states), sleep=waits.append)
        assert indexer.run_pass().indexed == 3
        assert len(waits) >= 2

    def test_total_passage_cap(self, store, docs, monkeypatch):
        monkeypatch.setattr(idx, "MAX_TOTAL_CHUNKS", 2)
        report = make_indexer(store, [docs]).run_pass()
        assert report.stopped == "index_full" and store.stats()["chunks"] <= 2

    def test_stop_ends_the_pass_between_passages(self, store, docs):
        indexer = make_indexer(store, [docs])
        indexer.request_stop()
        assert indexer.run_pass().stopped == "stopped"

    def test_an_interrupted_scan_removes_nothing(self, store, docs, monkeypatch):
        make_indexer(store, [docs]).run_pass()
        indexer = make_indexer(store, [docs])
        real = idx.scan_folders

        def halting_scan(folders, should_stop):
            indexer._halt.set()
            return real(folders, should_stop)

        monkeypatch.setattr(idx, "scan_folders", halting_scan)
        assert indexer.run_pass().stopped == "stopped"
        assert store.stats()["files"] == 3

    def test_overlapping_index_changes_keep_the_halt_until_the_last_one(self, store, docs):
        import threading
        indexer = make_indexer(store, [docs])
        inside = threading.Event()
        release = threading.Event()
        halts_seen = []

        def slow_delete():
            inside.set()
            release.wait(5)
            return store.delete_all()

        first = threading.Thread(target=lambda: indexer._with_pass_halted(slow_delete))
        first.start()
        assert inside.wait(5)
        second = threading.Thread(target=lambda: indexer._with_pass_halted(lambda: halts_seen.append(indexer._halt.is_set())))
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        assert halts_seen == [True]  # the first request's end did not lift the second one's halt
        assert not indexer._halt.is_set()

    def test_logs_never_carry_names_or_text(self, store, docs, monkeypatch):
        logged = []
        monkeypatch.setattr(idx, "debug_log", lambda message, *a, **k: logged.append(message))
        make_indexer(store, [docs]).run_pass()
        text = " ".join(logged)
        assert logged and "lease" not in text and "thermo" not in text and "cat" not in text.replace("indicat", "")
        assert str(docs) not in text

    def test_background_thread_runs_a_pass_and_stops(self, store, docs):
        indexer = make_indexer(store, [docs], rescan_sec=3600)
        indexer.start()
        deadline = time.time() + 10
        while store.stats()["files"] < 3 and time.time() < deadline:
            time.sleep(0.05)
        indexer.stop()
        assert store.stats()["files"] == 3 and not indexer.running

"""🗂️ Diary storage integrity: every pending turn reaches the diary, summaries are embedded for
semantic recall, and the FTS index and embeddings stay attached to the rows they describe, also in
databases written before rows were updated in place.

Everything runs on temporary databases with fake model backends.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from jarvis.memory import conversation as conv
from jarvis.memory.conversation import DialogueMemory
from jarvis.memory.db import SCHEMA_VERSION, Database

pytestmark = pytest.mark.unit

VECTOR = [1.0] + [0.0] * 767


def _cfg(embedding_model: str = ""):
    return SimpleNamespace(
        llm_provider="ollama", llm_base_url="http://localhost:11434", llm_chat_model="test",
        embedding_model=embedding_model, ollama_base_url="http://localhost:11434",
        ollama_chat_model="test", ollama_embed_model=embedding_model,
    )


@pytest.fixture
def fresh_vector_store(monkeypatch):
    """The vector stores are process-wide singletons bound to the first path; start clean."""
    from jarvis.utils import fast_vector_store, vector_store
    monkeypatch.setattr(fast_vector_store, "_faiss_vector_store", None)
    monkeypatch.setattr(vector_store, "_python_vector_store", None)


@pytest.fixture
def db_path(tmp_path, fresh_vector_store):
    return str(tmp_path / "jarvis.db")


@pytest.fixture
def diary_db(db_path):
    db = Database(db_path)  # default configuration: no sqlite-vss
    yield db
    db.close()


@pytest.fixture
def no_graph(monkeypatch):
    from jarvis.memory.graph_ops import GraphUpdateResult
    monkeypatch.setattr("jarvis.memory.graph_ops.update_graph_from_dialogue",
                        lambda **kw: GraphUpdateResult(stored=[], skipped=0))


def _fts_is_consistent(conn) -> bool:
    try:
        conn.execute("INSERT INTO summaries_fts(summaries_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False


def _long_conversation(memory: DialogueMemory, exchanges: int = 6) -> None:
    memory.add_message("user", "My sister Priya's birthday is on the third of March")
    memory.add_message("assistant", "Noted, the third of March.")
    for i in range(exchanges - 1):
        memory.add_message("user", f"what is {i} plus {i}")
        memory.add_message("assistant", f"{i + i}")


class TestEveryPendingTurnReachesTheDiary:
    def test_a_long_conversation_is_summarised_in_full(self, db, monkeypatch, no_graph):
        prompts: list[str] = []

        def fake_llm(cfg, system_prompt, user_content, **kwargs):
            prompts.append(user_content)
            return "SUMMARY: The user chatted about several things.\nTOPICS: chat, plans, family"

        monkeypatch.setattr(conv, "_direct_llm", fake_llm)
        memory = DialogueMemory(inactivity_timeout=300)
        _long_conversation(memory)

        assert conv.update_diary_from_dialogue_memory(db, memory, _cfg(), force=True) is not None
        sent = "\n".join(prompts)
        assert "Priya" in sent and "what is 4 plus 4" in sent
        assert memory.get_pending_chunks() == []

    def test_each_call_stays_within_the_batch_size(self, db, monkeypatch, no_graph):
        sizes: list[int] = []

        def fake_llm(cfg, system_prompt, user_content, **kwargs):
            sizes.append(sum(1 for line in user_content.splitlines() if line.startswith(("User:", "Assistant:"))))
            return "SUMMARY: The user chatted.\nTOPICS: chat, misc, things"

        monkeypatch.setattr(conv, "_direct_llm", fake_llm)
        memory = DialogueMemory(inactivity_timeout=300)
        _long_conversation(memory, exchanges=12)

        conv.update_diary_from_dialogue_memory(db, memory, _cfg(), force=True)
        assert sum(sizes) == 24
        assert max(sizes) <= conv.DIARY_BATCH_CHUNKS

    def test_a_later_batch_builds_on_the_summary_so_far(self, db, monkeypatch, no_graph):
        prompts: list[str] = []

        def fake_llm(cfg, system_prompt, user_content, **kwargs):
            prompts.append(user_content)
            return f"SUMMARY: Summary after call {len(prompts)}.\nTOPICS: chat, misc, things"

        monkeypatch.setattr(conv, "_direct_llm", fake_llm)
        memory = DialogueMemory(inactivity_timeout=300)
        _long_conversation(memory)

        conv.update_diary_from_dialogue_memory(db, memory, _cfg(), force=True)
        assert len(prompts) == 2 and "Summary after call 1." in prompts[1]

    def test_turns_of_a_failed_batch_stay_pending(self, db, monkeypatch, no_graph):
        calls = []

        def fake_llm(cfg, system_prompt, user_content, **kwargs):
            calls.append(user_content)
            if len(calls) == 1:
                return "SUMMARY: The user's sister Priya has a birthday on 3 March.\nTOPICS: family, birthday, dates"
            return None  # the model is unavailable for the second batch

        monkeypatch.setattr(conv, "_direct_llm", fake_llm)
        memory = DialogueMemory(inactivity_timeout=300)
        _long_conversation(memory)

        conv.update_diary_from_dialogue_memory(db, memory, _cfg(), force=True)
        pending = memory.get_pending_chunks()
        assert all("Priya" not in chunk for chunk in pending)  # its batch is in the diary
        assert pending == ["User: what is 4 plus 4", "Assistant: 8"]
        stored = db.get_conversation_summary(db.get_all_conversation_summaries()[0]["date_utc"])
        assert "Priya" in stored["summary"]


class TestSemanticRecall:
    def test_a_diary_summary_is_embedded_without_sqlite_vss(self, diary_db, monkeypatch):
        monkeypatch.setattr(conv, "_direct_llm", lambda *a, **k:
                            "SUMMARY: The user asked about quasars and pulsars.\nTOPICS: quasars, pulsars, space")
        monkeypatch.setattr(conv, "_embed_text", lambda text, cfg, **kw: VECTOR)

        summary_id = conv.update_daily_conversation_summary(
            diary_db, ["User: tell me about quasars", "Assistant: they are bright galactic nuclei"],
            _cfg(embedding_model="nomic-embed-text"))

        # Same meaning (same vector), no shared keyword.
        assert [r["id"] for r in diary_db.search_hybrid("astronomy", json.dumps(VECTOR))] == [summary_id]

    def test_no_embedding_model_means_no_embedding_call(self, diary_db, monkeypatch):
        monkeypatch.setattr(conv, "_direct_llm", lambda *a, **k: "SUMMARY: Notes.\nTOPICS: a, b, c")
        monkeypatch.setattr(conv, "_embed_text", lambda *a, **k: pytest.fail("embedded without a model"))
        assert conv.update_daily_conversation_summary(diary_db, ["User: hi"], _cfg()) is not None


class TestSummaryRowsStayConsistent:
    def test_updating_a_day_keeps_its_id_and_fts_in_step(self, diary_db):
        first = diary_db.upsert_conversation_summary("2026-10-12", "The user likes kayaking", "kayak")
        second = diary_db.upsert_conversation_summary("2026-10-12", "The user likes chess", "chess")

        assert first == second
        assert _fts_is_consistent(diary_db.conn)
        assert diary_db.search_hybrid("kayak", None) == []
        assert [r["id"] for r in diary_db.search_hybrid("chess", None)] == [second]

    def test_replaced_text_never_resurfaces_after_a_delete(self, diary_db):
        diary_db.upsert_conversation_summary("2026-10-11", "The user mentioned their kayaking club", "kayak")
        later = diary_db.upsert_conversation_summary("2026-10-11", "The user played chess", "chess")
        assert diary_db.delete_conversation_summary(later)
        diary_db.upsert_conversation_summary("2026-10-12", "The user bought groceries", "shopping")

        assert diary_db.search_hybrid("kayak", None) == []

    def test_a_summary_has_one_embedding_however_often_it_is_updated(self, diary_db):
        for text in ("kayaking", "chess", "groceries"):
            sid = diary_db.upsert_conversation_summary("2026-10-12", f"The user mentioned {text}", text)
            diary_db.upsert_summary_embedding(sid, VECTOR)

        hits = diary_db._python_vector_store.search(VECTOR, top_k=10)
        assert [summary_id for summary_id, _distance in hits] == [sid]

    def test_deleting_a_summary_removes_its_embedding(self, diary_db, db_path):
        sid = diary_db.upsert_conversation_summary("2026-10-12", "The user asked about quasars", "space")
        diary_db.upsert_summary_embedding(sid, VECTOR)

        assert diary_db.delete_conversation_summary(sid)
        assert diary_db._python_vector_store.search(VECTOR, top_k=10) == []
        assert diary_db.conn.execute("SELECT COUNT(*) FROM faiss_vector_store").fetchone()[0] == 0


class TestExistingDatabaseRepair:
    """A database written before rows were updated in place: every same-day flush replaced the row,
    so the FTS index kept the old text and the vector store kept the old row's embedding."""

    def _old_database(self, path: str) -> int:
        from jarvis.memory.db import _SCHEMA_SQL
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA_SQL)
        old_upsert = ("INSERT OR REPLACE INTO conversation_summaries(date_utc, ts_utc, summary, topics, source_app) "
                      "VALUES (?, ?, ?, ?, 'jarvis')")
        conn.execute(old_upsert, ("2026-10-12", "2026-10-12T09:00:00+00:00", "The user likes kayaking", "kayak"))
        conn.execute(old_upsert, ("2026-10-12", "2026-10-12T10:00:00+00:00", "The user likes chess", "chess"))
        conn.execute("CREATE TABLE python_vector_store (summary_id INTEGER PRIMARY KEY, vector_json TEXT NOT NULL)")
        conn.execute("CREATE TABLE faiss_vector_store (summary_id INTEGER PRIMARY KEY, vector_blob BLOB NOT NULL)")
        conn.executemany("INSERT INTO python_vector_store VALUES (?, ?)", [(1, json.dumps(VECTOR)), (2, json.dumps(VECTOR))])
        import numpy as np
        blob = np.array(VECTOR, dtype=np.float32).tobytes()
        conn.executemany("INSERT INTO faiss_vector_store VALUES (?, ?)", [(1, blob), (2, blob)])
        conn.commit()
        assert not _fts_is_consistent(conn)  # the state real databases are in
        live = conn.execute("SELECT id FROM conversation_summaries").fetchone()[0]
        conn.close()
        return live

    def test_opening_an_old_database_repairs_it(self, db_path):
        live = self._old_database(db_path)

        db = Database(db_path)
        try:
            assert _fts_is_consistent(db.conn)
            assert db.search_hybrid("kayak", None) == []
            assert [r["id"] for r in db.search_hybrid("chess", None)] == [live]
            assert db.get_conversation_summary("2026-10-12")["summary"] == "The user likes chess"
            for table in ("python_vector_store", "faiss_vector_store"):
                ids = [r[0] for r in db.conn.execute(f"SELECT summary_id FROM {table}")]
                assert ids == [live], table
            assert [sid for sid, _ in db._python_vector_store.search(VECTOR, top_k=10)] == [live]
            assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            db.close()

    def test_the_repair_runs_once(self, db_path, monkeypatch):
        self._old_database(db_path)

        from jarvis.memory import db as db_module
        rebuilds = []
        real_connect = sqlite3.connect

        class Spy(sqlite3.Connection):
            def execute(self, sql, *args):
                if "'rebuild'" in sql:
                    rebuilds.append(sql)
                return super().execute(sql, *args)

        monkeypatch.setattr(db_module.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Spy, **k))
        Database(db_path).close()
        Database(db_path).close()
        assert len(rebuilds) == 1

    def test_a_repair_that_cannot_run_is_tried_at_the_next_open(self, db_path, monkeypatch):
        live = self._old_database(db_path)

        def busy(self):
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as patched:
            patched.setattr(Database, "_repair_index_and_embeddings", busy)
            db = Database(db_path)  # still opens and works
            assert db.get_conversation_summary("2026-10-12")["id"] == live
            db.close()

        db = Database(db_path)
        try:
            assert _fts_is_consistent(db.conn)
            assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            db.close()

    def test_a_new_database_starts_at_the_current_version(self, diary_db):
        assert diary_db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
def test_memory_viewer_delete_removes_the_embedding(db_path, monkeypatch):
    from desktop_app import memory_viewer

    db = Database(db_path)
    sid = db.upsert_conversation_summary("2026-10-12", "The user asked about quasars", "space")
    db.upsert_summary_embedding(sid, VECTOR)
    db.close()
    monkeypatch.setattr(memory_viewer, "_get_db_path", lambda: db_path)
    monkeypatch.setattr(memory_viewer, "_db_conn", None)

    response = memory_viewer.app.test_client().delete(f"/api/memory/{sid}")

    assert response.status_code == 200
    conn = memory_viewer.get_db()
    for table in ("faiss_vector_store", "conversation_summaries"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert _fts_is_consistent(conn)
    conn.close()
    memory_viewer._db_conn = None

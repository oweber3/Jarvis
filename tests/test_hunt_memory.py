"""Repro tests for defects found in memory and stored data (bug hunt).

Each test describes the behaviour the specs or the code's own contract promise and fails on the
current code. Everything runs on temporary databases and fake model backends: no network, no real
config, no real Jarvis database.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jarvis.memory import conversation as conv
from jarvis.memory.conversation import DialogueMemory
from jarvis.memory.db import Database

pytestmark = pytest.mark.unit


# ── helpers ──────────────────────────────────────────────────────────────────

def _diary_cfg(embedding_model: str = ""):
    return SimpleNamespace(
        llm_provider="ollama", llm_base_url="http://localhost:11434", llm_chat_model="test",
        embedding_model=embedding_model, ollama_base_url="http://localhost:11434",
        ollama_chat_model="test", ollama_embed_model=embedding_model,
    )


@pytest.fixture
def fresh_vector_store(monkeypatch):
    """The vector stores are process-wide singletons keyed by the first path; start clean."""
    from jarvis.utils import fast_vector_store, vector_store
    monkeypatch.setattr(fast_vector_store, "_faiss_vector_store", None)
    monkeypatch.setattr(vector_store, "_python_vector_store", None)


@pytest.fixture
def diary_db(tmp_path, fresh_vector_store):
    db = Database(str(tmp_path / "jarvis.db"))  # default config: no sqlite-vss
    yield db
    db.close()


@pytest.fixture
def no_graph(monkeypatch):
    from jarvis.memory.graph_ops import GraphUpdateResult
    monkeypatch.setattr("jarvis.memory.graph_ops.update_graph_from_dialogue",
                        lambda **kw: GraphUpdateResult(stored=[], skipped=0))


# ── 1. Diary flush drops every pending turn but the last ten, yet marks them all saved ─────────

def test_diary_flush_never_marks_unsummarised_turns_as_saved(db, monkeypatch, no_graph):
    prompts: list[str] = []

    def fake_llm(cfg, system_prompt, user_content, **kwargs):
        prompts.append(user_content)
        return "SUMMARY: The user chatted about several things.\nTOPICS: chat, plans, family"

    monkeypatch.setattr(conv, "_direct_llm", fake_llm)
    memory = DialogueMemory(inactivity_timeout=300)
    memory.add_message("user", "My sister Priya's birthday is on the third of March")
    memory.add_message("assistant", "Noted, the third of March.")
    for i in range(5):  # five more ordinary exchanges inside the same five minutes
        memory.add_message("user", f"what is {i} plus {i}")
        memory.add_message("assistant", f"{i + i}")

    summary_id = conv.update_diary_from_dialogue_memory(db, memory, _diary_cfg(), force=True)

    assert summary_id is not None
    reached_summariser = any("Priya" in p for p in prompts)
    still_pending = any("Priya" in c for c in memory.get_pending_chunks())
    assert reached_summariser or still_pending, (
        "the first turns were neither summarised nor left pending: they are lost from the diary")


# ── 2. Default config (no sqlite-vss) never stores diary embeddings ───────────────────────────

def test_diary_summary_is_embedded_for_semantic_recall_without_sqlite_vss(diary_db, monkeypatch, no_graph):
    vector = [1.0] + [0.0] * 767
    monkeypatch.setattr(conv, "_direct_llm", lambda *a, **k:
                        "SUMMARY: The user asked about quasars and pulsars.\nTOPICS: quasars, pulsars, space")
    monkeypatch.setattr(conv, "_embed_text", lambda text, cfg, **kw: vector)

    summary_id = conv.update_daily_conversation_summary(
        diary_db, ["User: tell me about quasars", "Assistant: they are bright galactic nuclei"],
        _diary_cfg(embedding_model="nomic-embed-text"))
    assert summary_id is not None

    # A query that shares no keyword with the summary but has the same meaning (same vector).
    results = diary_db.search_hybrid("astronomy", json.dumps(vector))
    assert [r["id"] for r in results] == [summary_id], (
        "the diary summary has no stored embedding, so semantic recall finds nothing")


# ── 3. INSERT OR REPLACE re-keys the day's diary row and leaves the FTS index stale ───────────

def test_repeated_diary_flushes_keep_the_fts_index_consistent(diary_db):
    diary_db.upsert_conversation_summary("2026-10-12", "The user likes kayaking", "kayak")
    diary_db.upsert_conversation_summary("2026-10-12", "The user likes chess", "chess")

    try:
        diary_db.conn.execute("INSERT INTO summaries_fts(summaries_fts, rank) VALUES('integrity-check', 1)")
    except Exception as exc:  # sqlite3.DatabaseError: database disk image is malformed
        pytest.fail(f"FTS index no longer matches conversation_summaries: {exc}")


def test_replaced_diary_text_never_resurfaces_in_search(diary_db):
    diary_db.upsert_conversation_summary("2026-10-11", "The user mentioned their kayaking club", "kayak")
    later = diary_db.upsert_conversation_summary("2026-10-11", "The user played chess", "chess")
    # The user deletes that day's memory in the memory viewer (a plain DELETE).
    diary_db.conn.execute("DELETE FROM conversation_summaries WHERE id = ?", (later,))
    diary_db.conn.commit()
    new_day = diary_db.upsert_conversation_summary("2026-10-12", "The user bought groceries", "shopping")

    hits = [r["text"] for r in diary_db.search_hybrid("kayak", None)]
    assert hits == [], f"text that is no longer in any diary row is still matched: {hits} (row {new_day})"


# ── 4. logMeal inserts the same meal again on every retry after a post-insert failure ─────────

def test_log_meal_never_logs_one_meal_twice(diary_db, monkeypatch):
    from jarvis.tools.base import ToolContext
    from jarvis.tools.builtin.nutrition import log_meal

    def fake_llm(*, system_prompt, **kwargs):
        if system_prompt == log_meal.NUTRITION_SYS:
            return json.dumps({"description": "porridge with banana", "calories_kcal": 350,
                               "protein_g": 10, "carbs_g": 60, "fat_g": 6})
        raise TimeoutError("follow-up generation timed out")  # transient backend failure

    monkeypatch.setattr(log_meal, "call_llm_direct", fake_llm)
    cfg = SimpleNamespace(use_stdin=False, llm_chat_timeout_sec=5.0, llm_thinking_enabled=False,
                          llm_provider="ollama", llm_chat_model="test", ollama_chat_model="test")
    monkeypatch.setattr(log_meal, "resolve_model", lambda cfg, tier: "test")
    ctx = ToolContext(db=diary_db, cfg=cfg, system_prompt="", original_prompt="I had porridge",
                      redacted_text="I had porridge with a banana", max_retries=2, user_print=lambda s: None)

    log_meal.LogMealTool().run({"meal": "porridge with a banana"}, ctx)

    meals = diary_db.get_meals_between("0000", "9999")
    assert len(meals) <= 1, f"one meal was stored {len(meals)} times"


# ── 5. Cloud reply mode: a follow-up answered from shared activity data reaches the diary ─────

class _FakeBridge:
    def __init__(self, text):
        from jarvis.bridge.tools import BridgeOutcome
        self.outcome = BridgeOutcome("reply", text)
        self.contexts = []

    def prepare(self):
        return None

    def is_busy(self):
        return False

    def cancel_active(self, reason):
        return False

    def close(self):
        pass

    def run_request(self, utterance, context, origin, language, db, quiet, desktop=None):
        self.contexts.append(context)
        return self.outcome


def test_cloud_follow_up_from_shared_activity_data_stays_out_of_the_diary(mock_config, db, monkeypatch, capsys):
    from jarvis.bridge import modes
    from jarvis.memory import activity_log as al
    from jarvis.memory import activity_runtime
    from jarvis.reply import engine
    from jarvis.tools.confirmation import get_confirmation_store

    get_confirmation_store().clear_pending()
    modes.reset()
    al.consume_turn_private()
    monkeypatch.setattr(activity_runtime, "_sharing_stopped", False)
    mock_config.reply_mode = "claude"
    mock_config.claude_enabled = True
    mock_config.claude_share_recent_dialogue = True
    mock_config.claude_recent_dialogue_messages = 6
    mock_config.activity_log_share_with_cloud = True  # the user opted in to sharing with cloud modes

    memory = DialogueMemory(inactivity_timeout=300)
    # The earlier turn read the activity log, so it is private (kept out of the diary).
    memory.add_message("user", "what was I working on this afternoon?", diary=False)
    memory.add_message("assistant", "Mostly Excel, in Budget-2026.xlsx.", diary=False)

    bridge = _FakeBridge("You spent about two hours in Budget-2026.xlsx.")
    try:
        modes.start(mock_config, factories={"claude": lambda cfg: bridge}, save=lambda values: True)
        assert modes.wait_for_warm_up(5)
        engine.run_reply_engine(db, mock_config, None, "how long was I in that spreadsheet?", memory, quiet=False)
    finally:
        modes.reset()
        get_confirmation_store().clear_pending()
        al.consume_turn_private()

    assert any("Budget-2026" in m["content"] for m in bridge.contexts[0]), "activity data was shared (opted in)"
    leaked = [c for c in memory.get_pending_chunks() if "Budget-2026" in c]
    assert leaked == [], f"activity data queued for the diary: {leaked}"
    assert "Budget-2026" not in capsys.readouterr().out, "activity data printed to the console log"


# ── 6. Recall gate debug log carries secrets its redaction cannot see ─────────────────────────

def test_recall_gate_log_never_carries_what_redaction_removes(monkeypatch):
    from jarvis.memory import recall_gate
    from jarvis.utils.redact import redact

    logged: list[str] = []
    monkeypatch.setattr(recall_gate, "debug_log", lambda msg, tag=None: logged.append(msg))
    query = "send the report to priya.sharma@example.org"
    assert "priya" not in redact(query)  # redaction removes the address from the query itself
    recent = [
        {"role": "user", "content": query},
        {"role": "tool", "content": "report for priya.sharma@example.org ready to send"},
    ]

    assert recall_gate.should_recall(query, recent) is False
    assert logged and all("priya" not in line and "sharma" not in line for line in logged), logged


# ── 7. OTP rule misses the usual "code is 123456" order ───────────────────────────────────────

def test_one_time_code_is_redacted_when_the_keyword_comes_first():
    from jarvis.utils.redact import redact
    assert "482913" not in redact("Your verification code is 482913")

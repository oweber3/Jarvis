"""Background local work follows the reply mode.

The diary summariser, knowledge-graph extraction and dictation clean-up run
whatever the reply mode. While Codex or Claude writes replies they must run
on the fast model, so the large local chat model is never paged in.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis.bridge import modes
from jarvis.llm import Tier, resolve_model
from jarvis.memory.graph_ops import GraphUpdateResult


def _cfg():
    return SimpleNamespace(
        llm_provider="ollama",
        llm_base_url="http://localhost:11434",
        llm_chat_model="big-chat",
        fast_model="small-fast",
        embedding_model="test",
        ollama_base_url="http://localhost:11434",
        ollama_chat_model="big-chat",
        ollama_embed_model="test",
    )


class _RecordingBackend:
    def __init__(self, reply):
        self.models = []
        self._reply = reply

    def direct(self, model, *args, **kwargs):
        self.models.append(model)
        return self._reply

    def streaming(self, model, *args, **kwargs):
        self.models.append(model)
        return self._reply


@pytest.mark.unit
@pytest.mark.parametrize("mode", [modes.LOCAL, modes.CODEX, modes.CLAUDE])
def test_diary_flush_runs_on_background_tier(mode, db, dialogue_memory, monkeypatch):
    from jarvis.memory.conversation import update_diary_from_dialogue_memory

    monkeypatch.setattr(modes, "active_mode", lambda: mode)
    cfg = _cfg()
    backend = _RecordingBackend("SUMMARY: User asked about bats.\nTOPICS: bats")
    dialogue_memory.add_message("user", "Are bats blind?")
    dialogue_memory.add_message("assistant", "No, they can see.")

    with patch("jarvis.memory.conversation.get_llm_backend", return_value=backend), patch(
        "jarvis.memory.graph_ops.update_graph_from_dialogue",
        return_value=GraphUpdateResult(stored=[], skipped=0),
    ) as graph_update:
        update_diary_from_dialogue_memory(
            db=db, dialogue_memory=dialogue_memory, cfg=cfg, force=True, timeout_sec=5.0,
        )

    expected = resolve_model(cfg, Tier.BACKGROUND)
    assert expected == ("big-chat" if mode == modes.LOCAL else "small-fast")
    assert backend.models and set(backend.models) == {expected}
    assert graph_update.call_args.kwargs["chat_model"] == expected


@pytest.mark.unit
@pytest.mark.parametrize("mode", [modes.LOCAL, modes.CODEX])
def test_dictation_cleanup_runs_on_background_tier(mode, monkeypatch):
    from jarvis.dictation.dictation_engine import _llm_clean_dictation

    monkeypatch.setattr(modes, "active_mode", lambda: mode)
    cfg = _cfg()
    backend = _RecordingBackend("tidy text")

    with patch("jarvis.llm.get_llm_backend", return_value=backend):
        assert _llm_clean_dictation("um tidy text", cfg) == "tidy text"

    assert backend.models == [resolve_model(cfg, Tier.BACKGROUND)]


@pytest.mark.unit
@pytest.mark.parametrize("configured, allowed, expected", [
    ("codex", True, "small-fast"),
    ("claude", True, "small-fast"),
    ("codex", False, "big-chat"),
    ("local", True, "big-chat"),
])
def test_a_process_without_the_bridges_follows_the_mode_chosen_in_settings(configured, allowed, expected):
    """The desktop app's memory viewer runs no bridges; its background work follows Settings."""
    modes.reset()
    cfg = _cfg()
    cfg.reply_mode = configured
    cfg.codex_enabled = cfg.claude_enabled = allowed
    assert resolve_model(cfg, Tier.BACKGROUND) == expected


def _cloud_cfg(db_path=None):
    cfg = _cfg()
    cfg.reply_mode = "codex"
    cfg.codex_enabled = True
    cfg.llm_chat_timeout_sec = 5.0
    cfg.llm_thinking_enabled = False
    return cfg


@pytest.mark.unit
def test_diary_maintenance_sweeps_run_on_the_background_tier(db):
    from jarvis.memory.conversation import optimise_diary_topics, rewrite_all_diary_summaries

    modes.reset()
    cfg = _cloud_cfg()
    db.upsert_conversation_summary("2026-10-01", "User asked about bats.", topics="bats,animals")
    backend = _RecordingBackend('{"bats": "animals"}')

    with patch("jarvis.memory.conversation.get_llm_backend", return_value=backend), patch(
        "jarvis.memory.conversation._embed_text", return_value=None,
    ):
        list(rewrite_all_diary_summaries(db, cfg))
        list(optimise_diary_topics(db, cfg))

    assert backend.models and set(backend.models) == {"small-fast"}


@pytest.mark.unit
@pytest.mark.parametrize("route, runner", [
    ("/api/graph/import-diary", "jarvis.memory.graph_ops.update_graph_from_dialogue"),
    ("/api/graph/consolidate-all", "jarvis.memory.graph_ops.consolidate_all_populated_nodes"),
])
def test_memory_viewer_graph_jobs_run_on_the_background_tier(route, runner, tmp_path):
    pytest.importorskip("flask")
    from desktop_app import memory_viewer
    from jarvis.memory.db import Database
    from jarvis.memory.graph import GraphMemoryStore

    modes.reset()
    cfg = _cloud_cfg()
    db_path = str(tmp_path / "jarvis.db")
    seed = Database(db_path, sqlite_vss_path=None)
    seed.upsert_conversation_summary("2026-10-01", "User asked about bats.", topics="bats")
    seed.close()
    store = GraphMemoryStore(db_path)
    store.create_node(name="Bats", description="", data="Bats can see.", parent_id="world")
    seen = []

    def record(*args, **kwargs):
        seen.append(kwargs["chat_model"])
        if runner.endswith("update_graph_from_dialogue"):
            return GraphUpdateResult(stored=[], skipped=0)
        return iter(())

    with patch("jarvis.config.load_settings", return_value=cfg), patch.object(
        memory_viewer, "_get_db_path", return_value=db_path,
    ), patch.object(memory_viewer, "get_graph_store", return_value=store), patch(runner, side_effect=record):
        response = memory_viewer.app.test_client().post(route)
        response.get_data()

    assert seen and set(seen) == {"small-fast"}

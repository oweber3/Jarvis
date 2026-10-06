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

"""A tool's image reaches the local chat model only when that model can see, and only for that reply."""

from unittest.mock import Mock, patch

import pytest

from src.jarvis.memory.conversation import DialogueMemory
from src.jarvis.reply.engine import run_reply_engine
from src.jarvis.tools.types import ToolExecutionResult, ToolImage
from test_engine_tool_carryover import _mock_cfg

pytestmark = pytest.mark.unit

SCREEN = ToolImage("image/jpeg", "QUJD")
CALL = {"message": {"content": "", "tool_calls": [{
    "id": "c1", "type": "function", "function": {"name": "screenshot", "arguments": {}}}]}}
ANSWER = {"message": {"content": "It shows error 42."}}


def _run(sees: bool, dm=None):
    """One reply that calls screenshot and then answers; returns the messages of each backend call."""
    sent = []
    backend = Mock()
    backend.supports_images.side_effect = lambda model: sees
    backend.chat.side_effect = lambda model, messages, **_k: (sent.append(messages), CALL if len(sent) == 1
                                                               else ANSWER)[1]
    with patch("src.jarvis.reply.engine.plan_query", return_value=[]), \
            patch("src.jarvis.reply.engine.extract_search_params_for_memory", return_value={}), \
            patch("src.jarvis.reply.engine.run_tool_with_retries",
                  return_value=ToolExecutionResult(True, "Screen text: Error 42", images=(SCREEN,))), \
            patch("src.jarvis.reply.engine.extract_text_from_response", side_effect=["", "It shows error 42."]), \
            patch("src.jarvis.reply.engine.get_llm_backend", return_value=backend):
        run_reply_engine(db=Mock(), cfg=_mock_cfg(), tts=None, text="what is on my screen",
                         dialogue_memory=dm or DialogueMemory())
    return sent


def _screen_results(messages):
    return [m for m in messages if m.get("tool_name") == "screenshot" or "Error 42" in str(m.get("content"))]


def test_a_vision_model_receives_the_image_with_the_tool_result():
    sent = _run(sees=True)
    results = _screen_results(sent[1])
    assert results and results[-1]["images"] == ["QUJD"]
    assert "Error 42" in results[-1]["content"]


def test_a_model_that_cannot_see_receives_the_text_only():
    sent = _run(sees=False)
    results = _screen_results(sent[1])
    assert results and not any("images" in m for m in sent[1])
    assert "Error 42" in results[-1]["content"]


def test_the_image_is_not_carried_over_to_later_replies():
    dm = DialogueMemory()
    _run(sees=True, dm=dm)
    stored = [m for _ts, msgs in dm._tool_turns for m in msgs]
    assert stored and not any("images" in m or "_images" in m for m in stored)

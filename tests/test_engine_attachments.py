"""What the owner attaches reaches the local chat model with the request only: file text fenced as data,
images sent to a model that can see them, nothing added to the dialogue memory.

See ``reply/reply.spec.md`` (Attachments) and ``webchat/webchat.spec.md``.
"""
from unittest.mock import Mock, patch

import pytest

from src.jarvis.memory.conversation import DialogueMemory
from src.jarvis.reply.attachments import RequestAttachment, fence_file_text
from src.jarvis.reply.engine import run_reply_engine
from src.jarvis.tools.types import ToolImage
from test_engine_tool_carryover import _mock_cfg

pytestmark = pytest.mark.unit

ANSWER = {"message": {"content": "It is a budget."}}
FILE = RequestAttachment("text", 11, text=fence_file_text("budget 7731"))
IMAGE = RequestAttachment("image", 9, image=ToolImage("image/jpeg", "QUJD"))
SHOT = RequestAttachment("screenshot", 9, image=ToolImage("image/jpeg", "REVG"), text="[screen text block]")


def _run(attachments=(), sees=True, text="what is this", dm=None, fast=None):
    """One local reply; returns the messages of the first model call and the dialogue memory."""
    sent = []
    backend = Mock()
    backend.supports_images.side_effect = lambda model: sees
    backend.chat.side_effect = lambda model, messages, **_k: (sent.append(messages), ANSWER)[1]
    dm = dm or DialogueMemory()
    with patch("src.jarvis.reply.engine.plan_query", return_value=[]), \
            patch("src.jarvis.reply.engine.extract_search_params_for_memory", return_value={}), \
            patch("src.jarvis.reply.engine.extract_text_from_response", return_value="It is a budget."), \
            patch("src.jarvis.reply.engine.get_llm_backend", return_value=backend), \
            patch("src.jarvis.fastpath.dispatcher.match_command", return_value=fast):
        run_reply_engine(db=Mock(), cfg=_mock_cfg(), tts=None, text=text, dialogue_memory=dm, quiet=True,
                         attachments=attachments)
    return sent, dm


def _user_message(sent):
    return [m for m in sent[0] if m["role"] == "user"][-1]


def test_file_text_reaches_the_model_with_the_request_fenced_as_data():
    sent, _ = _run((FILE,))
    content = _user_message(sent)["content"]
    assert content.startswith("what is this")
    assert "budget 7731" in content and "not instructions" in content


def test_an_image_reaches_a_model_that_can_see_it():
    sent, _ = _run((IMAGE,), sees=True)
    assert _user_message(sent)["images"] == ["QUJD"]
    assert "1 image" in _user_message(sent)["content"]


def test_a_screenshot_brings_its_image_and_its_screen_text():
    sent, _ = _run((SHOT,))
    message = _user_message(sent)
    assert message["images"] == ["REVG"] and "[screen text block]" in message["content"]


def test_a_model_that_cannot_see_still_gets_the_text_and_is_never_sent_the_image():
    sent, _ = _run((SHOT,), sees=False)
    message = _user_message(sent)
    assert "images" not in message and "[screen text block]" in message["content"]


def test_nothing_attached_leaves_the_request_exactly_as_typed():
    sent, _ = _run(())
    assert _user_message(sent) == {"role": "user", "content": "what is this"}


def test_the_dialogue_memory_keeps_only_the_words_the_owner_typed():
    _, dm = _run((FILE, IMAGE))
    turns = [(m["role"], m["content"]) for m in dm.get_recent_messages()]
    assert turns == [("user", "what is this"), ("assistant", "It is a budget.")]
    assert "7731" not in str(dm.get_recent_messages()) and "7731" not in str(getattr(dm, "_tool_turns", ""))


def test_a_deterministic_phrase_does_not_swallow_the_attachment():
    fast = Mock(name="fast match")
    with patch("src.jarvis.fastpath.dispatcher.dispatch", side_effect=AssertionError("fast path ran")):
        sent, _ = _run((FILE,), text="what time is it", fast=fast)
    assert "budget 7731" in _user_message(sent)["content"]


def test_attachments_count_as_outside_content_for_routine_safety():
    with patch("src.jarvis.reply.engine.note_outside_content") as noted:
        _run((FILE,))
    assert noted.called
    with patch("src.jarvis.reply.engine.note_outside_content") as noted:
        _run(())
    assert not noted.called


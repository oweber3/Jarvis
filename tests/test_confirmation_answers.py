"""Spoken and typed answers to a pending destructive action.

A cancellation must never approve the action, whatever affirmative word sits beside it, and an
unclear answer is never approval (reply.spec.md, Action Safety). These tests drive the real gate
(``run_tool_with_retries``), the real store and the voice listener, and observe whether the held
action ran.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    SafetyTier,
    get_confirmation_store,
    set_dialog_callback,
    set_result_handler,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult


class Cfg:
    windows_tools_enabled = True
    windows_app_aliases = {}
    voice_debug = False
    wake_word = "jarvis"
    wake_aliases = []
    wake_fuzzy_ratio = 0.78
    tts_rate = 200


class HeldTool(Tool):
    """A destructive tool that needs a spoken yes; records every run."""

    def __init__(self, name: str):
        self._name = name
        self.runs: list[str] = []

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "test"

    @property
    def inputSchema(self):
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(
            tool_name=self._name, tier=SafetyTier.CONFIRM_VOICE, action="delete file",
            target=str((args or {}).get("target", "")), parameters=dict(args or {}),
        )

    def run(self, args, context):
        self.runs.append(str((args or {}).get("target", "")))
        return ToolExecutionResult(success=True, reply_text="Deleted.")


@pytest.fixture(autouse=True)
def clean_state():
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)
    yield
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)


@pytest.fixture
def held_tool():
    tool = HeldTool("heldDelete")
    BUILTIN_TOOLS[tool.name] = tool
    yield tool
    BUILTIN_TOOLS.pop(tool.name, None)


@pytest.fixture
def results():
    box = SimpleNamespace(items=[], event=threading.Event())

    def handler(reply, success):
        box.items.append((reply, success))
        box.event.set()

    set_result_handler(handler)
    return box


def _hold(tool: HeldTool, target: str = "report.pdf") -> ToolExecutionResult:
    result = run_tool_with_retries(
        db=None, cfg=Cfg(), tool_name=tool.name, tool_args={"target": target},
        system_prompt="", original_prompt="", redacted_text="", language="en",
    )
    assert result.success is False and get_confirmation_store().has_pending_voice()
    return result


def _listener(language="en"):
    from jarvis.listening.listener import VoiceListener

    listener = VoiceListener.__new__(VoiceListener)
    listener.cfg = Cfg()
    listener.db = SimpleNamespace()
    listener.tts = MagicMock()
    listener.state_manager = MagicMock()
    # Jarvis has just asked, so the hot window is open.
    listener.state_manager.was_speech_during_hot_window.return_value = True
    listener._last_detected_language = language
    listener._wake_timestamp = None
    listener._end_engagement = MagicMock()
    listener.track_tts_start = MagicMock()
    listener.activate_hot_window = MagicMock()
    return listener


CANCELLATIONS = [
    "okay never mind",
    "ok never mind",
    "yeah don't",
    "yeah don’t",  # typographic apostrophe, as Whisper often writes it
    "sure, do not",
    "yes wait",
    "yes no",
    "no",
    "no thanks",
    "don't",
    "never mind",
    "no, don't do it",
]


@pytest.mark.parametrize("answer", CANCELLATIONS)
def test_spoken_cancellation_never_runs_the_held_action(held_tool, results, answer):
    listener = _listener()
    _hold(held_tool)

    listener._handle_pending_confirmation(answer.lower(), 1.0, 2.0)
    time.sleep(0.2)  # an approval would run on a worker thread

    assert held_tool.runs == []
    assert not get_confirmation_store().has_pending()


@pytest.mark.parametrize("answer", ["okay never mind", "yeah don't", "sure, do not", "yes wait"])
def test_spoken_cancellation_is_reported_as_cancelled(held_tool, results, answer):
    listener = _listener()
    _hold(held_tool)

    handled = listener._handle_pending_confirmation(answer.lower(), 1.0, 2.0)

    assert handled is True
    assert listener.tts.speak.call_args[0][0] == "Action cancelled."


@pytest.mark.parametrize("answer", ["yes delete the other one", "yes but rename it first", "sure maybe"])
def test_unclear_answer_is_not_approval(held_tool, results, answer):
    listener = _listener()
    _hold(held_tool)

    listener._handle_pending_confirmation(answer.lower(), 1.0, 2.0)
    time.sleep(0.2)

    assert held_tool.runs == []
    assert not get_confirmation_store().has_pending()


@pytest.mark.parametrize("answer", ["yes", "yes please", "ok", "okay", "go ahead", "yes, go ahead",
                                    "sure thing", "yeah sure", "yes thank you", "do it"])
def test_spoken_approval_runs_the_held_action_once(held_tool, results, answer):
    listener = _listener()
    _hold(held_tool, "report.pdf")

    assert listener._handle_pending_confirmation(answer.lower(), 1.0, 2.0) is True
    assert results.event.wait(5)

    assert held_tool.runs == ["report.pdf"]


def test_typed_cancellation_never_runs_the_held_action(held_tool, mock_config, dialogue_memory):
    from jarvis.reply.engine import run_reply_engine

    _hold(held_tool)
    reply = run_reply_engine(db=None, cfg=mock_config, tts=None, text="okay never mind",
                             dialogue_memory=dialogue_memory, quiet=True)

    assert reply == "Action cancelled."
    assert held_tool.runs == []
    assert not get_confirmation_store().has_pending()


def test_listener_answers_in_the_language_whisper_detected(held_tool, results):
    """A reply in a language without a phrase table cannot approve, even if it sounds like yes."""
    listener = _listener(language="xx")
    _hold(held_tool)

    listener._handle_pending_confirmation("yes", 1.0, 2.0)
    time.sleep(0.2)

    assert held_tool.runs == []

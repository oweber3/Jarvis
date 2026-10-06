"""The bridge's assistant instructions: bounds, references to real mechanisms, and delivery.

The instructions are the base instructions of every ephemeral request session, separate from the
request in the turn input and the reference dialogue in the turn's untrusted context. Nothing here asserts that the text improves the model's
behaviour: that is the job of the live background evals.
"""
from __future__ import annotations

import json
import re

import pytest

from test_codex_bridge_service import TOOLS, build, run


def capture(service_kwargs=None, **run_kwargs):
    def script(model):
        script.model = model
        script.wrong = model.execute("getTime", {}, thread="some-other-thread")
        model.answer("completed", "ok")
        model.finish()

    service, server, _, _ = build(script, **(service_kwargs or {}))
    run(service, **run_kwargs)
    return server, script


@pytest.mark.unit
class TestInstructionText:
    def test_bounded_deterministic_and_plain(self):
        from jarvis.codex_bridge import prompts
        text = prompts.assistant_instructions()
        assert isinstance(text, str) and text.strip()
        assert len(text) <= prompts.MAX_INSTRUCTION_CHARS == 2000
        assert text == prompts.assistant_instructions()
        assert "—" not in text and all(ch.isprintable() or ch == "\n" for ch in text)

    def test_every_bridge_tool_it_names_exists(self):
        from jarvis.codex_bridge import prompts
        from jarvis.codex_bridge.service import BRIDGE_TOOLS
        mentioned = set(re.findall(r"\bjarvis_[a-z_]+\b", prompts.assistant_instructions()))
        assert mentioned and mentioned <= set(BRIDGE_TOOLS)

    def test_jarvis_tools_and_actions_it_names_exist(self):
        from jarvis.codex_bridge import prompts
        from jarvis.tools.builtin.windows import AppControlTool, WindowControlTool
        text = prompts.assistant_instructions()
        tools = {"appControl": AppControlTool, "windowControl": WindowControlTool}
        assert {name for name in tools if name in text} == set(tools)
        assert {"displays", "place"} <= set(WindowControlTool.actions) and "displays" in text and "place" in text
        schema = AppControlTool().inputSchema["properties"]
        assert all(field in schema and field in text for field in ("monitor", "zone", "state"))

    def test_statuses_and_states_it_names_are_the_brokers(self):
        from jarvis.bridge import broker
        from jarvis.codex_bridge import prompts
        text = prompts.assistant_instructions()
        for status in broker._COMPLETION_STATUSES:
            assert status in text, status
        assert "awaiting_confirmation" in text and broker.State.AWAITING_CONFIRMATION.value == "awaiting_confirmation"

    def test_it_carries_the_safety_boundaries_and_the_background_context(self):
        from jarvis.codex_bridge import prompts
        text = prompts.assistant_instructions().casefold()
        for needle in ("reference data", "never", "clarifying question", "confirmation", "british english",
                       "no history", "sub-agents"):
            assert needle in text, needle
        assert "chat" not in text.replace("no visible chat", "")


@pytest.mark.unit
class TestInstructionDelivery:
    def test_every_session_carries_the_instructions_as_base_instructions(self):
        from jarvis.codex_bridge import prompts
        server, _ = capture()
        assert server.params_of("thread/start")[0]["baseInstructions"] == prompts.assistant_instructions()

    def test_the_instructions_are_separate_from_the_reference_dialogue(self):
        from jarvis.codex_bridge import prompts
        server, script = capture(context=[{"role": "user", "content": "earlier"}])
        assert script.model.request["context"] == [{"role": "user", "content": "earlier"}]
        assert "reference" in script.model.request["context_note"].lower()
        turn = script.model.text + json.dumps(script.model.context)
        assert prompts.assistant_instructions() not in turn

    def test_the_instructions_do_not_depend_on_dialogue_sharing_or_the_request(self):
        shared, _ = capture(text="open word", context=[{"role": "user", "content": "x"}])
        unshared, script = capture(text="something else entirely", context=[])
        assert "context" not in script.model.request
        assert (shared.params_of("thread/start")[0]["baseInstructions"]
                == unshared.params_of("thread/start")[0]["baseInstructions"])

    def test_request_content_is_still_redacted_and_never_enters_the_instructions(self):
        server, script = capture(text="email bob@example.com the report",
                                 context=[{"role": "user", "content": "my address is alice@example.com"}])
        blob = script.model.text + json.dumps(script.model.context) + json.dumps(server.requests)
        assert "bob@example.com" not in blob and "alice@example.com" not in blob

    def test_the_turn_adds_nothing_else_and_no_desktop_snapshot(self):
        _, script = capture()
        assert set(script.model.request) == {"request_id", "utterance", "language", "remaining_sec"}
        assert script.model.context == {}

    def test_without_instructions_the_session_falls_back_to_none_of_ours(self):
        server, _ = capture(service_kwargs={"instructions_provider": lambda: None})
        assert "baseInstructions" not in server.params_of("thread/start")[0]

    def test_another_session_gets_nothing(self):
        _, script = capture()
        assert script.wrong["success"] is False and "text" not in script.wrong["data"]
        assert script.wrong["data"]["reason"] == "wrong_thread"

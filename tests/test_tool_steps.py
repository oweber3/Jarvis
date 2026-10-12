"""Tool steps: every call that goes through central safety leaves one short, privacy-safe step.

Behaviour only: what a step says after a call ran, was refused or is waiting for a yes, on every route.
See ``webchat/webchat.spec.md``, Tool steps.
"""
import pytest

from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, get_confirmation_store
from jarvis.tools.registry import run_tool_with_retries
from jarvis.tools.step_log import MAX_STEPS, StepLog, humanise
from jarvis.tools.types import StepNote, ToolExecutionResult
from routine_fakes import FakeTool, failing


class NotedTool(FakeTool):
    """A tool that words its own step."""

    def __init__(self, name, note, **kwargs):
        super().__init__(name, **kwargs)
        self._note = note

    def step_note(self, args, result):
        return self._note(args, result) if callable(self._note) else self._note


@pytest.fixture
def steps(monkeypatch):
    log = StepLog()
    monkeypatch.setattr("jarvis.tools.step_log._LOG", log)
    return log


@pytest.fixture
def tools(monkeypatch, steps):
    def add(tool):
        monkeypatch.setitem(registry.BUILTIN_TOOLS, tool.name, tool)
        return tool
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()


def call(cfg, tool, args=None):
    return run_tool_with_retries(None, cfg, tool, args or {}, "", "", "")


def shown(log):
    return [(s.tool, s.status, s.label, s.summary) for s in log.since(0)]


class TestLabels:
    def test_a_tool_name_becomes_words_without_any_phrase_list(self):
        assert humanise("systemVolume") == "System volume"
        assert humanise("getWeather") == "Get weather"
        assert humanise("tvControl") == "Tv control"
        assert humanise("server__fetch_page") == "Fetch page"

    def test_a_tool_with_no_hook_is_named_after_itself(self, tools, steps, mock_config):
        tools(FakeTool("lightsControl"))
        call(mock_config, "lightsControl", {"action": "set"})
        assert shown(steps) == [("lightsControl", "done", "Lights control", None)]

    def test_a_tool_can_word_its_own_step(self, tools, steps, mock_config):
        tools(NotedTool("lightsControl", StepNote("Set the lounge lights to 40%", "Brightness changed")))
        call(mock_config, "lightsControl", {"action": "set"})
        assert shown(steps) == [("lightsControl", "done", "Set the lounge lights to 40%", "Brightness changed")]

    def test_the_hook_sees_the_arguments_and_the_result(self, tools, steps, mock_config):
        seen = []

        def note(args, result):
            seen.append((dict(args), result.success))
            return StepNote(f"Level {args['level']}")

        tools(NotedTool("lightsControl", note, properties={"level": {"type": "number"}}))
        call(mock_config, "lightsControl", {"level": 40})
        assert seen == [({"level": 40}, True)]

    def test_a_hook_that_breaks_falls_back_to_the_tool_name(self, tools, steps, mock_config):
        def boom(args, result):
            raise RuntimeError("no")

        tools(NotedTool("lightsControl", boom))
        call(mock_config, "lightsControl")
        assert shown(steps) == [("lightsControl", "done", "Lights control", None)]

    @pytest.mark.parametrize("leak", [
        "Opened C:\\Users\\me\\notes.txt", "Opened /home/me/notes.txt", "Opened https://example.com/a",
        "Wrote to \\\\server\\share", "Mailed me@example.com", "Opened file:///tmp/x",
    ])
    def test_a_label_that_carries_a_path_or_address_is_replaced_by_the_tool_name(self, tools, steps, mock_config, leak):
        tools(NotedTool("fileTool", StepNote(leak, leak)))
        call(mock_config, "fileTool")
        assert shown(steps) == [("fileTool", "done", "File tool", None)]

    def test_labels_and_summaries_are_one_short_line(self, tools, steps, mock_config):
        tools(NotedTool("lightsControl", StepNote("Set\nlights " + "x" * 300, "a\n\nb " + "y" * 500)))
        call(mock_config, "lightsControl")
        (step,) = steps.since(0)
        assert "\n" not in step.label and len(step.label) <= 80
        assert "\n" not in step.summary and len(step.summary) <= 160


class TestOutcomes:
    def test_a_failed_call_is_a_failed_step_without_the_error_text(self, tools, steps, mock_config):
        tools(NotedTool("lightsControl", StepNote("Set the lights"), behaviour=failing("Bulb at C:\\x is offline")))
        call(mock_config, "lightsControl")
        assert shown(steps) == [("lightsControl", "failed", "Lights control", None)]

    def test_a_refused_call_is_a_declined_step(self, tools, steps, mock_config):
        tools(FakeTool("diskTool", tier=SafetyTier.DENY))
        call(mock_config, "diskTool", {"action": "wipe"})
        assert shown(steps) == [("diskTool", "declined", "Disk tool", None)]

    def test_a_call_waiting_for_a_yes_is_a_waiting_step(self, tools, steps, mock_config):
        tools(FakeTool("localFiles", tiers={"delete": SafetyTier.CONFIRM_VOICE}))
        call(mock_config, "localFiles", {"action": "delete", "target": "report"})
        assert shown(steps) == [("localFiles", "waiting", "Local files", None)]

    def test_an_unknown_tool_leaves_no_step(self, tools, steps, mock_config):
        call(mock_config, "doesNotExist")
        assert steps.since(0) == []

    @pytest.mark.parametrize("tool", ["routineControl", "stop", "toolSearchTool", "refreshMCPTools"])
    def test_routing_and_routine_tools_leave_no_step(self, tools, steps, mock_config, tool):
        tools(FakeTool(tool))
        call(mock_config, tool)
        assert steps.since(0) == []

    def test_every_step_of_a_routine_is_shown_on_its_own(self, tools, steps, mock_config):
        tools(FakeTool("lightsControl"))
        tools(FakeTool("tvControl"))
        call(mock_config, "lightsControl")
        call(mock_config, "tvControl")
        assert [s.tool for s in steps.since(0)] == ["lightsControl", "tvControl"]


class TestTheLog:
    def test_steps_are_numbered_in_order_and_read_from_a_point(self):
        log = StepLog()
        first = log.record("a", "done", "A")
        second = log.record("b", "done", "B")
        assert second.seq > first.seq
        assert [s.tool for s in log.since(first.seq)] == ["b"]
        assert log.latest() == second.seq

    def test_reading_up_to_a_time_leaves_later_steps_for_the_next_reply(self):
        now = [100.0]
        log = StepLog(clock=lambda: now[0])
        log.record("a", "done", "A")
        now[0] = 200.0
        log.record("b", "done", "B")
        assert [s.tool for s in log.since(0, until_ts=150.0)] == ["a"]

    def test_the_log_is_bounded(self):
        log = StepLog()
        for _ in range(MAX_STEPS + 10):
            log.record("a", "done", "A")
        assert len(log.since(0)) == MAX_STEPS

    def test_an_empty_label_falls_back_to_the_tool_name(self):
        log = StepLog()
        assert log.record("systemVolume", "done", "  ").label == "System volume"


class TestEveryRoute:
    """One place sees every executed call, so each route leaves its steps."""

    def test_an_approved_confirmation_leaves_a_waiting_step_then_a_done_one(self, tools, steps, mock_config):
        tools(FakeTool("localFiles", tiers={"delete": SafetyTier.CONFIRM_VOICE}))
        call(mock_config, "localFiles", {"action": "delete", "target": "a.pdf"})
        confirmations = get_confirmation_store()
        confirmations.handle_voice_response("yes", "en")
        confirmations.run_approved(confirmations.claim_approved(), cfg=mock_config)
        assert [(s.tool, s.status) for s in steps.since(0)] == [("localFiles", "waiting"), ("localFiles", "done")]

    def test_the_fast_path(self, tools, steps, mock_config):
        from jarvis.fastpath.dispatcher import dispatch
        from jarvis.fastpath.matcher import FastMatch
        tools(FakeTool("windowControl"))
        dispatch(FastMatch("window.minimise", "windows", "windowControl",
                           {"action": "minimise", "target": "word", "match": "process"}), None, mock_config, "minimise word")
        assert [(s.tool, s.status) for s in steps.since(0)] == [("windowControl", "done")]

    def test_a_background_bridge(self, tools, steps, mock_config):
        from jarvis.bridge.broker import Broker, BrokerLimits
        from jarvis.bridge.execution import ToolCallRunner
        props = {"action": {"type": "string"}}
        tools(FakeTool("lightsControl", properties=props))
        broker = Broker(limits=BrokerLimits())
        request = broker.submit_request("lights on", [], "voice", "en", {"lightsControl": {"inputSchema": {"properties": props}}})
        assert broker.assign_thread(request.id, "t") and broker.assign_turn(request.id, "u")
        runner = ToolCallRunner(broker, get_confirmation_store(), run_tool_with_retries, mock_config)
        envelope = {"request_id": request.id, "tool_name": "lightsControl", "arguments": {"action": "on"}}
        data, _, _ = runner.execute(request.id, "c1", envelope, "t", "u", None, "en", True, "lights on")
        assert data["status"] == "ok"
        assert [(s.tool, s.status) for s in steps.since(0)] == [("lightsControl", "done")]

    def test_the_local_agentic_loop(self, tools, steps, mock_config, db, dialogue_memory):
        from test_routine_journal import _chat, _run_engine
        tools(FakeTool("lightsControl", properties={"action": {"type": "string"}}))
        mock_config.ollama_chat_model = mock_config.llm_chat_model = "gpt-oss:20b"
        _run_engine(mock_config, db, dialogue_memory, [_chat(calls=[("lightsControl", {"action": "on"})]), _chat("Done.")])
        assert [(s.tool, s.status) for s in steps.since(0)] == [("lightsControl", "done")]

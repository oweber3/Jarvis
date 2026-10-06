"""Routines in Codex and Claude mode: routineControl is in every snapshot, and a routine never widens it."""
import pytest

from jarvis.bridge.broker import Broker, BrokerLimits
from jarvis.bridge.execution import ToolCallRunner
from jarvis.routines import store
from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, get_confirmation_store
from jarvis.tools.registry import run_tool_with_retries
from jarvis.tools.request_scope import current_call
from jarvis.tools.types import ToolExecutionResult
from routine_fakes import FakeTool

THREAD, TURN = "thread-A", "turn-A"


@pytest.fixture
def tools(monkeypatch):
    def add(name, **kwargs):
        tool = FakeTool(name, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()


@pytest.mark.parametrize("mode", ["codex", "claude"])
@pytest.mark.parametrize("share_memory", [False, True])
def test_routine_control_is_in_every_bridge_snapshot(mode, share_memory, mock_config):
    from jarvis.claude_bridge.service import claude_tool_snapshot
    from jarvis.codex_bridge.service import codex_tool_snapshot
    setattr(mock_config, f"{mode}_share_long_term_memory", share_memory)
    snapshot = (codex_tool_snapshot if mode == "codex" else claude_tool_snapshot)(mock_config)
    assert "routineControl" in snapshot
    assert set(snapshot["routineControl"]["inputSchema"]["properties"]["action"]["enum"]) == {
        "run", "list", "recent", "save", "delete", "rename"}


def _request(broker, snapshot_names):
    snapshot = {name: {"description": "", "inputSchema": registry.BUILTIN_TOOLS[name].inputSchema}
                for name in snapshot_names}
    request = broker.submit_request("start movie mode", [], "voice", "en", snapshot)
    assert broker.assign_thread(request.id, THREAD) and broker.assign_turn(request.id, TURN)
    return request


def _execute(runner, request, arguments, call_id="c1"):
    envelope = {"request_id": request.id, "tool_name": "routineControl", "arguments": arguments}
    return runner.execute(request.id, call_id, envelope, THREAD, TURN, None, "en", True, "start movie mode")


def test_a_step_outside_the_request_snapshot_is_unavailable_and_nothing_runs(tools, mock_config):
    tv = tools("tvControl")
    tools("replyMode")
    store.load({"movie mode": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}},
                                         {"tool": "replyMode", "args": {"action": "set", "target": "local"}}]}})
    broker = Broker(limits=BrokerLimits())
    request = _request(broker, ["routineControl", "tvControl"])
    runner = ToolCallRunner(broker, get_confirmation_store(), run_tool_with_retries, mock_config)
    data, question, _ = _execute(runner, request, {"action": "run", "name": "movie mode"})
    assert data["status"] == "error" and question is None
    assert "step 2 of 2 (replyMode set local): this tool is not available here" in data["text"]
    assert tv.calls == []


def test_a_routine_inside_the_snapshot_runs_as_one_call(tools, mock_config):
    tv = tools("tvControl")
    seen = []

    def lights(_args):
        seen.append(current_call().allowed_tools)
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("lightsControl", behaviour=lights)
    store.load({"movie mode": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}},
                                         {"tool": "lightsControl", "args": {"action": "dim"}}]}})
    broker = Broker(limits=BrokerLimits(max_tool_calls=1))
    request = _request(broker, ["routineControl", "tvControl", "lightsControl"])
    runner = ToolCallRunner(broker, get_confirmation_store(), run_tool_with_retries, mock_config)
    data, _, _ = _execute(runner, request, {"action": "run", "name": "movie mode"})
    assert data["status"] == "ok" and data["text"] == "Movie mode: all 2 steps done."
    assert len(tv.calls) == 1
    assert seen == [frozenset({"routineControl", "tvControl", "lightsControl"})]


def test_a_confirming_routine_waits_for_the_user_and_keeps_the_snapshot_once_approved(tools, mock_config):
    seen = []

    def delete(_args):
        seen.append(current_call().allowed_tools)
        return ToolExecutionResult(success=True, reply_text="deleted")

    deleter = tools("localFiles", tiers={"delete": SafetyTier.CONFIRM_VOICE}, behaviour=delete)
    store.load({"tidy up": {"steps": [{"tool": "localFiles", "args": {"action": "delete", "target": "a.pdf"}}]}})
    broker = Broker(limits=BrokerLimits())
    request = _request(broker, ["routineControl", "localFiles"])
    confirmations = get_confirmation_store()
    runner = ToolCallRunner(broker, confirmations, run_tool_with_retries, mock_config)
    data, question, _ = _execute(runner, request, {"action": "run", "name": "tidy up"})
    assert data["status"] == "awaiting_confirmation"
    assert "run the routine tidy up, which will delete a.pdf" in question
    assert deleter.calls == []
    confirmations.handle_voice_response("yes", "en")
    result = confirmations.run_approved(confirmations.claim_approved(), cfg=mock_config)
    assert result.success
    assert seen == [frozenset({"routineControl", "localFiles"})]


def test_the_result_a_provider_sees_carries_labels_and_outcomes_only(tools, mock_config):
    from routine_fakes import failing
    tools("openPath", behaviour=failing(r"Could not open C:\Users\me\Secret\plan.pdf"))
    store.load({"study": {"steps": [{"tool": "openPath",
                                     "args": {"action": "open", "target": r"C:\Users\me\Secret\plan.pdf"}}]}})
    broker = Broker(limits=BrokerLimits())
    request = _request(broker, ["routineControl", "openPath"])
    runner = ToolCallRunner(broker, get_confirmation_store(), run_tool_with_retries, mock_config)
    data, _, _ = _execute(runner, request, {"action": "run", "name": "study"})
    assert "Secret" not in data["text"] and "plan.pdf" not in data["text"]
    assert "Step 1 (openPath open) failed" in data["text"]


def test_a_step_question_mid_run_is_spoken_with_its_target_and_never_reaches_the_provider(tools, mock_config):
    secret = r"C:\Users\me\Private Folder\b.pdf"
    tools("tvControl")
    asker = tools("trashBin")

    def raise_the_stakes(_args):
        asker.tier = SafetyTier.CONFIRM_VOICE
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("lightsControl", behaviour=raise_the_stakes)
    store.load({"tidy up": {"steps": [{"tool": "lightsControl", "args": {"action": "dim"}},
                                      {"tool": "trashBin", "args": {"action": "delete", "target": secret}},
                                      {"tool": "tvControl", "args": {"action": "launch"}}]}})
    broker = Broker(limits=BrokerLimits())
    request = _request(broker, ["routineControl", "tvControl", "lightsControl", "trashBin"])
    runner = ToolCallRunner(broker, get_confirmation_store(), run_tool_with_retries, mock_config)
    data, question, _ = _execute(runner, request, {"action": "run", "name": "tidy up"})
    assert data["status"] == "awaiting_confirmation" and "Private" not in str(data)
    assert f"delete {secret}. Say yes or no." in question
    assert asker.calls == []

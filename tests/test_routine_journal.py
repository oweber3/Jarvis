"""The recent-actions journal: what Jarvis actually did, on every route, kept in memory for this conversation."""
from unittest.mock import patch

import pytest

from jarvis.routines import definitions, store
from jarvis.routines.journal import MAX_ENTRIES, Journal, get_journal
from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, get_confirmation_store
from jarvis.tools.registry import run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult
from routine_fakes import FakeTool, failing

LIGHTS = {"action": {"type": "string"}, "room": {"type": "string"}, "level": {"type": "number"}}
WINDOW = 300


@pytest.fixture
def tools(monkeypatch):
    def add(name, **kwargs):
        tool = FakeTool(name, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()


def recorded():
    """The journal as ``(tool, args)``, newest first."""
    return [(entry.tool, dict(entry.args)) for entry in get_journal().recent(WINDOW)]


def call(cfg, tool, args):
    return run_tool_with_retries(None, cfg, tool, args, "", "", "")


# --- the journal itself -------------------------------------------------------------------------

def test_entries_keep_only_the_argument_keys_the_schema_declares():
    journal = Journal()
    journal.record("appControl", {"action": "focus", "target": "Word", "match": "process"},
                   {"properties": {"action": {}, "target": {}}})
    assert [dict(entry.args) for entry in journal.recent(WINDOW)] == [{"action": "focus", "target": "Word"}]


def test_entries_are_numbered_uniquely_newest_first_and_bounded():
    journal = Journal()
    for level in range(MAX_ENTRIES + 5):
        journal.record("lights", {"level": level}, {"properties": {"level": {}}})
    entries = journal.recent(WINDOW)
    assert len(entries) == MAX_ENTRIES
    assert [entry.args["level"] for entry in entries] == list(range(MAX_ENTRIES + 4, 4, -1))
    numbers = [entry.number for entry in entries]
    assert numbers == sorted(set(numbers), reverse=True)


def test_entries_expire_with_the_dialogue_window():
    now = [1000.0]
    journal = Journal(clock=lambda: now[0])
    journal.record("lights", {"level": 1}, {"properties": {"level": {}}})
    now[0] += 200
    journal.record("lights", {"level": 2}, {"properties": {"level": {}}})
    now[0] += 150
    assert [entry.args["level"] for entry in journal.recent(300)] == [2]
    now[0] += 200
    assert journal.recent(300) == []


@pytest.mark.parametrize("tool", sorted(definitions.NEVER_STEPS))
def test_routine_conversation_and_routing_tools_are_never_recorded(tool):
    journal = Journal()
    journal.record(tool, {}, {"properties": {}})
    assert journal.recent(WINDOW) == []


# --- through the central path ---------------------------------------------------------------------

def test_a_successful_call_is_recorded_with_schema_keys_only(tools, mock_config):
    tools("lightsControl", properties=LIGHTS)
    call(mock_config, "lightsControl", {"action": "set", "room": "lounge", "level": 40})
    assert recorded() == [("lightsControl", {"action": "set", "room": "lounge", "level": 40})]


def test_failures_refusals_and_pending_confirmations_record_nothing(tools, mock_config):
    tools("lightsControl", properties=LIGHTS, behaviour=failing("The bulb is offline."))
    tools("diskTool", tier=SafetyTier.DENY)
    tools("localFiles", tiers={"delete": SafetyTier.CONFIRM_VOICE})
    call(mock_config, "lightsControl", {"action": "set", "room": "lounge"})
    call(mock_config, "diskTool", {"action": "wipe"})
    call(mock_config, "localFiles", {"action": "delete", "target": "a.pdf"})
    call(mock_config, "noSuchTool", {})
    assert recorded() == []


def test_an_approved_confirmation_is_recorded_once_it_runs(tools, mock_config):
    tools("localFiles", tiers={"delete": SafetyTier.CONFIRM_VOICE})
    call(mock_config, "localFiles", {"action": "delete", "target": "a.pdf"})
    confirmations = get_confirmation_store()
    confirmations.handle_voice_response("yes", "en")
    confirmations.run_approved(confirmations.claim_approved(), cfg=mock_config)
    assert recorded() == [("localFiles", {"action": "delete", "target": "a.pdf"})]


def test_the_steps_of_a_routine_are_recorded_and_the_routine_call_is_not(tools, mock_config):
    tools("lightsControl", properties=LIGHTS)
    tools("tvControl")
    store.load({"movie mode": {"steps": [{"tool": "lightsControl", "args": {"action": "set", "level": 10}},
                                         {"tool": "tvControl", "args": {"action": "launch"}}]}})
    assert call(mock_config, "routineControl", {"action": "run", "name": "movie mode"}).success
    assert recorded() == [("tvControl", {"action": "launch"}), ("lightsControl", {"action": "set", "level": 10})]


def test_the_fast_path_records_the_action_without_its_routing_hint(tools, mock_config):
    from jarvis.fastpath.dispatcher import dispatch
    from jarvis.fastpath.matcher import FastMatch
    tools("windowControl")
    dispatch(FastMatch("window.minimise", "windows", "windowControl",
                       {"action": "minimise", "target": "word", "match": "process"}),
             None, mock_config, "minimise word")
    assert recorded() == [("windowControl", {"action": "minimise", "target": "word"})]


def test_a_background_bridge_call_is_recorded(tools, mock_config):
    from jarvis.bridge.broker import Broker, BrokerLimits
    from jarvis.bridge.execution import ToolCallRunner
    tools("lightsControl", properties=LIGHTS)
    broker = Broker(limits=BrokerLimits())
    request = broker.submit_request("lights on", [], "voice", "en",
                                    {"lightsControl": {"inputSchema": {"properties": LIGHTS}}})
    assert broker.assign_thread(request.id, "t") and broker.assign_turn(request.id, "u")
    runner = ToolCallRunner(broker, get_confirmation_store(), run_tool_with_retries, mock_config)
    envelope = {"request_id": request.id, "tool_name": "lightsControl", "arguments": {"action": "on"}}
    data, _, _ = runner.execute(request.id, "c1", envelope, "t", "u", None, "en", True, "lights on")
    assert data["status"] == "ok"
    assert recorded() == [("lightsControl", {"action": "on"})]


def _chat(content="", calls=()):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"function": {"name": name, "arguments": args}} for name, args in calls]
    return {"message": message}


def _engine_patches(engine_mod, responses, plan=()):
    replies = iter(responses)
    return [patch.object(engine_mod, "chat_with_messages", side_effect=lambda *a, **k: next(replies)),
            patch.object(engine_mod, "select_tools", return_value=["lightsControl", "stop"]),
            patch.object(engine_mod, "plan_query", return_value=list(plan)),
            patch.object(engine_mod, "extract_search_params_for_memory", return_value={"keywords": []})]


def _run_engine(mock_config, db, dialogue_memory, responses, plan=(), extra=()):
    from contextlib import ExitStack
    from jarvis.reply import engine as engine_mod
    mock_config.evaluator_enabled = False
    mock_config.fast_commands_enabled = False
    with ExitStack() as stack:
        for item in [*_engine_patches(engine_mod, responses, plan), *extra]:
            stack.enter_context(item)
        engine_mod.run_reply_engine(db=db, cfg=mock_config, tts=None, text="dim the lounge lights",
                                    dialogue_memory=dialogue_memory, quiet=True)


def test_the_local_agentic_loop_records_the_calls_it_ran(tools, mock_config, db, dialogue_memory):
    tools("lightsControl", properties=LIGHTS)
    mock_config.ollama_chat_model = mock_config.llm_chat_model = "gpt-oss:20b"
    _run_engine(mock_config, db, dialogue_memory, [
        _chat(calls=[("lightsControl", {"action": "set", "room": "lounge", "level": 20})]), _chat("Done.")])
    assert recorded() == [("lightsControl", {"action": "set", "room": "lounge", "level": 20})]


def test_tool_model_mode_records_the_calls_it_ran(tools, mock_config, db, dialogue_memory):
    tools("lightsControl", properties=LIGHTS)
    mock_config.ollama_chat_model = mock_config.llm_chat_model = "qwen3.5:9b"
    mock_config.tool_model = "gpt-oss:20b"
    _run_engine(mock_config, db, dialogue_memory, [
        _chat(calls=[("lightsControl", {"action": "off"})]), _chat("Off."), _chat("The lights are off.")])
    assert recorded() == [("lightsControl", {"action": "off"})]


def test_planner_direct_exec_records_the_calls_it_ran(tools, mock_config, db, dialogue_memory):
    from jarvis.reply import engine as engine_mod
    tools("lightsControl", properties=LIGHTS)
    mock_config.ollama_chat_model = mock_config.llm_chat_model = "gemma4:e2b"
    resolved = iter([("lightsControl", {"action": "set", "level": 5})])
    _run_engine(mock_config, db, dialogue_memory, [_chat("Dimmed.")] * 3,
                plan=["lightsControl action='set' level=5", "Reply to the user."],
                extra=[patch.object(engine_mod, "_resolve_plan_step",
                                    side_effect=lambda *a, **k: next(resolved, None))])
    assert recorded() == [("lightsControl", {"action": "set", "level": 5})]


def test_the_journal_never_reaches_the_debug_log(tools, mock_config, monkeypatch):
    from jarvis.routines import journal as journal_module
    logged = []
    monkeypatch.setattr(journal_module, "debug_log", lambda message, category="": logged.append(message))
    tools("lightsControl", properties=LIGHTS)
    call(mock_config, "lightsControl", {"action": "set", "room": "secret lounge", "level": 33})
    assert logged and not any("secret" in message or "33" in message or "lightsControl" in message
                              for message in logged)

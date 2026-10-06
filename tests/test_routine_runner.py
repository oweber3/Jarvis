"""Running a routine: whole-routine validation first, then each step once, in order, through the central path."""
import json
import threading

import pytest

from jarvis.routines import definitions, runner
from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, get_confirmation_store
from routine_fakes import FakeTool, Gate, failing

VOLUME = {"action": {"type": "string"}, "percent": {"type": "number"}}


@pytest.fixture
def tools(monkeypatch):
    """Registers inert fake tools in the live catalogue for one test; they share one call log."""
    log = []

    def add(name, **kwargs):
        tool = FakeTool(name, log=log, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool

    add.log = log
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()


def routine(*steps, name="movie mode"):
    """A loaded routine from ``(tool, args)`` or ``(tool, args, label)`` steps."""
    raw = [{"tool": step[0], "args": step[1], **({"label": step[2]} if len(step) > 2 else {})} for step in steps]
    return definitions.load_routines({name: {"steps": raw}})[name]


def run(r, cfg, **kwargs):
    kwargs.setdefault("user_print", lambda _line: None)
    return runner.run_routine(r, cfg, **kwargs)


# --- validation -------------------------------------------------------------------------------

def test_a_missing_tool_fails_the_whole_routine_before_any_step_runs(tools, mock_config):
    tools("tvControl")
    tools("systemVolume", properties=VOLUME)
    r = routine(("tvControl", {"action": "launch"}), ("systemVolume", {"action": "set", "percent": 30}),
                ("appControlMissing", {"action": "open", "target": "Music"}))
    result = run(r, mock_config)
    assert not result.success
    assert tools.log == []
    assert "step 3 of 3 (appControlMissing open Music)" in result.text
    assert "not available" in result.text


def test_a_tool_outside_the_bridge_snapshot_is_unavailable(tools, mock_config):
    tools("tvControl")
    tools("replyMode")
    r = routine(("tvControl", {"action": "launch"}), ("replyMode", {"action": "set", "target": "claude"}))
    result = run(r, mock_config, allowed_tools=frozenset({"tvControl", "routineControl"}))
    assert not result.success and tools.log == []
    assert "step 2 of 2 (replyMode set claude)" in result.text and "not available" in result.text
    assert run(r, mock_config).success


def test_arguments_that_do_not_fit_the_schema_fail_the_routine(tools, mock_config):
    tools("systemVolume", properties=VOLUME, required=["action"])
    for args in ({"action": "set", "volume": 30}, {"percent": 30}):
        result = run(routine(("systemVolume", {"action": "set"}), ("systemVolume", args)), mock_config)
        assert not result.success and tools.log == []
        assert "step 2 of 2" in result.text


def test_a_denied_step_fails_the_routine(tools, mock_config):
    tools("diskTool", tier=SafetyTier.DENY)
    tools("tvControl")
    result = run(routine(("tvControl", {"action": "launch"}), ("diskTool", {"action": "wipe"})), mock_config)
    assert not result.success and tools.log == []
    assert "step 2 of 2 (diskTool wipe)" in result.text


@pytest.mark.parametrize("nested", sorted(definitions.NEVER_STEPS))
def test_routines_do_not_nest_and_never_run_conversation_or_routing_tools(tools, mock_config, nested):
    tools("tvControl")
    result = run(routine(("tvControl", {"action": "launch"}), (nested, {})), mock_config)
    assert not result.success and tools.log == []
    assert "step 2 of 2" in result.text


def test_a_validation_error_never_contains_argument_values(tools, mock_config):
    secret = r"C:\Users\me\Secret Plans\report.pdf"
    r = routine(("missingTool", {"action": "open", "target": secret, "note": "https://example.test/x"}))
    result = run(r, mock_config)
    assert "Secret" not in result.text and "example.test" not in result.text and "report" not in result.text


# --- running ----------------------------------------------------------------------------------

def test_steps_run_once_each_in_order(tools, mock_config):
    tools("tvControl")
    tools("systemVolume", properties=VOLUME)
    tools("appControl")
    r = routine(("tvControl", {"action": "launch", "target": "HDMI 1"}, "TV to the console"),
                ("systemVolume", {"action": "set", "percent": 30}),
                ("appControl", {"action": "open", "target": "Music"}))
    result = run(r, mock_config)
    assert result.success
    assert tools.log == [("tvControl", {"action": "launch", "target": "HDMI 1"}),
                         ("systemVolume", {"action": "set", "percent": 30}),
                         ("appControl", {"action": "open", "target": "Music"})]
    assert result.text == "Movie mode: all 3 steps done."
    assert [step.status for step in result.steps] == ["done", "done", "done"]


def test_a_failed_step_is_reported_and_the_next_step_still_runs(tools, mock_config):
    tools("tvControl")
    tools("appControl", behaviour=failing("Application not found."))
    tools("systemVolume", properties=VOLUME)
    r = routine(("tvControl", {"action": "launch"}), ("appControl", {"action": "open", "target": "Music"}),
                ("systemVolume", {"action": "set", "percent": 30}))
    result = run(r, mock_config)
    assert not result.success
    assert [name for name, _ in tools.log] == ["tvControl", "appControl", "systemVolume"]
    assert result.text.splitlines() == [
        "Movie mode: 2 of 3 steps done.", "Step 2 (appControl open Music) failed: Application not found."]


def test_a_failed_step_is_never_retried(tools, mock_config):
    app = tools("appControl", behaviour=failing("Application not found."))
    run(routine(("appControl", {"action": "open", "target": "Music"})), mock_config)
    assert len(app.calls) == 1


def test_steps_not_started_by_the_deadline_are_skipped(tools, mock_config):
    now = [0.0]

    def slow(_args):
        now[0] += runner.DEADLINE_SEC + 1
        from jarvis.tools.types import ToolExecutionResult
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("tvControl", behaviour=slow)
    tools("appControl")
    r = routine(("tvControl", {"action": "launch"}), ("appControl", {"action": "open", "target": "Music"}),
                ("appControl", {"action": "open", "target": "Mail"}))
    result = run(r, mock_config, clock=lambda: now[0])
    assert [name for name, _ in tools.log] == ["tvControl"]
    assert [(step.status, step.reason) for step in result.steps] == [
        ("done", ""), ("skipped", "out of time"), ("skipped", "out of time")]
    assert "Step 2 (appControl open Music) skipped: out of time." in result.text


def test_a_stop_lets_the_step_in_progress_finish_and_skips_the_rest(tools, mock_config):
    def stop_now(_args):
        assert runner.stop_running()
        from jarvis.tools.types import ToolExecutionResult
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("tvControl", behaviour=stop_now)
    tools("appControl")
    r = routine(("tvControl", {"action": "launch"}), ("appControl", {"action": "open", "target": "Music"}))
    result = run(r, mock_config)
    assert [name for name, _ in tools.log] == ["tvControl"]
    assert [(step.status, step.reason) for step in result.steps] == [("done", ""), ("skipped", "stopped")]


def test_a_stop_with_no_routine_running_changes_nothing_for_the_next_one(tools, mock_config):
    assert runner.stop_running() is False
    tools("tvControl")
    assert run(routine(("tvControl", {"action": "launch"})), mock_config).success


def test_a_second_routine_while_one_runs_is_refused_as_busy(tools, mock_config):
    gate = Gate()
    tools("tvControl", behaviour=gate)
    tools("appControl")
    first_result = []
    first = threading.Thread(target=lambda: first_result.append(
        run(routine(("tvControl", {"action": "launch"})), mock_config)))
    first.start()
    try:
        assert gate.entered.wait(5)
        second = run(routine(("appControl", {"action": "open", "target": "Music"}), name="other"), mock_config)
        assert not second.success and "already running" in second.text
        assert [name for name, _ in tools.log] == ["tvControl"]
    finally:
        gate.release.set()
        first.join(5)
    assert first_result[0].success
    assert run(routine(("appControl", {"action": "open", "target": "Music"}), name="other"), mock_config).success


def test_a_step_that_raises_is_failed_and_the_next_step_runs(tools, mock_config):
    def explode(_args):
        raise RuntimeError("C:\\secret\\path exploded")

    tools("tvControl", behaviour=explode)
    tools("appControl")
    result = run(routine(("tvControl", {"action": "launch"}), ("appControl", {"action": "open"})), mock_config)
    assert [step.status for step in result.steps] == ["failed", "done"]
    assert "secret" not in result.text


def test_each_step_prints_one_line_and_the_steps_print_nothing_themselves(tools, mock_config, capsys):
    mock_config.voice_debug = False

    def chatty(_args):
        from jarvis.tools.types import ToolExecutionResult
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("tvControl", behaviour=chatty)
    tools("appControl", behaviour=failing("Application not found."))
    lines = []
    run(routine(("tvControl", {"action": "launch"}, "TV to the console"),
                ("appControl", {"action": "open", "target": "Music"})), mock_config, user_print=lines.append)
    assert lines == ["✅ TV to the console", "❌ appControl open Music: Application not found."]
    assert capsys.readouterr().out == ""


# --- the report -------------------------------------------------------------------------------

def test_the_report_has_labels_and_outcomes_but_no_argument_values_paths_or_addresses(tools, mock_config):
    tools("openPath", behaviour=failing(r"Could not open 'C:\Users\me\Secret Plans\report.pdf' from "
                                        "https://example.test/files/report.pdf"))
    tools("getWeather", behaviour=lambda _a: __import__("jarvis.tools.types", fromlist=["x"]).ToolExecutionResult(
        success=True, reply_text="Sunny, 21 degrees in Springfield"))
    r = routine(("getWeather", {"action": "now", "target": "Springfield"}),
                ("openPath", {"action": "open", "target": r"C:\Users\me\Secret Plans\report.pdf"}))
    result = run(r, mock_config)
    text = json.dumps([result.text, [vars(step) for step in result.steps]])
    for private in ("Secret", "report.pdf", "example.test", "Sunny", "21 degrees"):
        assert private not in text
    assert "[path]" in result.text and "[address]" in result.text
    assert "Step 2 (openPath open) failed" in result.text


def test_a_single_step_routine_reports_in_the_singular(tools, mock_config):
    tools("tvControl")
    assert run(routine(("tvControl", {"action": "launch"}), name="tv"), mock_config).text == "Tv: the step is done."

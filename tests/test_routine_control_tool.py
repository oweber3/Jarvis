"""The routineControl tool: always registered, short, and its results carry labels and outcomes only."""
import json

import pytest

from jarvis.routines import store
from jarvis.tools import registry
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from routine_fakes import FakeTool

SECRET = r"C:\Users\me\Secret Plans\report.pdf"


@pytest.fixture
def tools(monkeypatch):
    def add(name, **kwargs):
        tool = FakeTool(name, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool
    return add


def call(cfg, **args):
    return run_tool_with_retries(None, cfg, "routineControl", args, "", "", "")


def test_routine_control_is_registered_on_every_platform_and_setting(mock_config):
    from jarvis.tools.registry import configure_windows_tools
    mock_config.windows_tools_enabled = False
    configure_windows_tools(mock_config, platform="linux", start_index=False)
    try:
        assert "routineControl" in BUILTIN_TOOLS
    finally:
        mock_config.windows_tools_enabled = True
        configure_windows_tools(mock_config, start_index=False)


def test_its_description_is_short_so_it_does_not_crowd_the_router():
    assert len(BUILTIN_TOOLS["routineControl"].description) <= 200


def test_an_unknown_routine_fails_and_lists_the_available_names(tools, mock_config):
    tools("tvControl")
    store.load({"movie mode": {"steps": [{"tool": "tvControl"}]}, "bedtime": {"steps": [{"tool": "tvControl"}]}})
    result = call(mock_config, action="run", name="party")
    assert not result.success
    assert "bedtime" in result.error_message and "movie mode" in result.error_message
    assert BUILTIN_TOOLS["tvControl"].calls == []


def test_listing_shows_names_aliases_and_step_labels_and_nothing_else(tools, mock_config):
    tools("openPath")
    tools("tvControl")
    store.load({"study": {"aliases": ["revision"], "extra": "kept", "steps": [
        {"tool": "openPath", "args": {"action": "open", "target": SECRET}},
        {"tool": "tvControl", "args": {"action": "launch", "target": "HDMI 1"}, "label": "TV to the console"}]}})
    result = call(mock_config, action="list")
    assert result.success
    assert json.loads(result.reply_text) == {"routines": [
        {"name": "study", "aliases": ["revision"], "steps": ["openPath open", "TV to the console"]}]}
    assert "Secret" not in result.reply_text


def test_listing_with_no_routines_says_so(mock_config):
    result = call(mock_config, action="list")
    assert result.success and "no routines" in result.reply_text


def test_recent_lists_what_was_done_newest_last_by_number_and_label(tools, mock_config):
    tools("tvControl")
    tools("openPath")
    run_tool_with_retries(None, mock_config, "tvControl", {"action": "launch", "target": "HDMI 1"}, "", "", "")
    run_tool_with_retries(None, mock_config, "openPath", {"action": "open", "target": SECRET}, "", "", "")
    result = call(mock_config, action="recent")
    recent = json.loads(result.reply_text)["recent"]
    assert [entry["step"] for entry in recent] == ["tvControl launch HDMI 1", "openPath open"]
    assert recent[0]["number"] < recent[1]["number"]
    assert "Secret" not in result.reply_text


def test_recent_with_nothing_done_says_so(mock_config):
    result = call(mock_config, action="recent")
    assert result.success and "not done anything" in result.reply_text


def test_an_unknown_action_fails_with_the_actions(mock_config):
    result = call(mock_config, action="explode")
    assert not result.success and "run" in result.error_message


def test_the_routine_report_is_the_result(tools, mock_config):
    tools("tvControl")
    store.load({"movie mode": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}}]}})
    result = call(mock_config, action="run", name="movie mode")
    assert result.success and result.reply_text == "Movie mode: the step is done."

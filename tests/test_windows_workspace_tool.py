"""The workspaceControl tool end to end on a simulated desktop.

Only the OS layer is fake (``evals/desktop_sim.py``); the real tool, central safety, retries,
redaction and desktop records run. Paths and URLs are placeholders.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from desktop_sim import SECOND, SimulatedDesktop  # noqa: E402

from jarvis.memory.desktop_referents import get_desktop_referents  # noqa: E402
from jarvis.platform.windows import workspaces  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_referents():
    get_desktop_referents().clear()
    yield
    get_desktop_referents().clear()


@pytest.fixture
def desktop(mock_config, tmp_path):
    with SimulatedDesktop(mock_config) as sim:
        mock_config.windows_workspaces = workspaces.load_workspaces(sim.workspace_definitions(tmp_path))
        yield sim


def run(cfg, **args):
    from jarvis.tools.registry import run_tool_with_retries
    return run_tool_with_retries(None, cfg, "workspaceControl", args, "", "", "")


def referents():
    return get_desktop_referents().recent(max_age_sec=300)


@pytest.fixture(autouse=True)
def registered():
    from jarvis.tools.builtin.windows import WorkspaceControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS["workspaceControl"] = WorkspaceControlTool()
    yield
    BUILTIN_TOOLS.pop("workspaceControl", None)


@pytest.mark.unit
class TestOpen:
    def test_open_places_both_windows_in_their_zones_and_reports_outcomes(self, mock_config, desktop):
        before = {w.hwnd for w in desktop.open_windows()}
        result = run(mock_config, action="open", target="Design")
        assert result.success
        data = json.loads(result.reply_text)
        assert data["action"] == "workspace_opened" and data["workspace"] == "design"
        assert [(i["label"], i["outcome"], i["zone"], i["monitor"]) for i in data["items"]] == [
            ("textbooks", "opened_and_placed", "left", SECOND), ("chat", "opened_and_placed", "right", SECOND)]
        new = [w for w in desktop.open_windows("chrome") if w.hwnd not in before]
        assert len(new) == 2 and {w.hwnd for w in new} == {i["hwnd"] for i in data["items"]}
        assert all(w.monitor == SECOND and w.zone_rect for w in new)
        assert len(desktop.browser_launches) == 2
        assert len([a for a in desktop.browser_launches[0] if a.startswith("file:")]) == 2

    def test_an_alias_opens_the_same_workspace(self, mock_config, desktop):
        assert run(mock_config, action="open", target="  DESIGN project ").success

    def test_the_result_never_contains_paths_or_urls(self, mock_config, desktop, tmp_path):
        result = run(mock_config, action="open", target="design")
        shown = result.reply_text or ""
        for secret in (str(tmp_path), "first.pdf", "second.pdf", "chat.example.test", "file:", "chrome.exe"):
            assert secret not in shown

    def test_each_placed_window_is_remembered_under_its_label(self, mock_config, desktop):
        data = json.loads(run(mock_config, action="open", target="design").reply_text)
        by_application = {r.application: r for r in referents()}
        assert set(by_application) == {"textbooks", "chat"}
        chat = by_application["chat"]
        assert (chat.hwnd, chat.process, chat.monitor, chat.zone, chat.state, chat.last_action) == (
            data["items"][1]["hwnd"], "chrome", SECOND, "right", "normal", "open")

    def test_a_follow_up_by_handle_moves_the_remembered_window_and_keeps_its_label(self, mock_config, desktop):
        from jarvis.tools.registry import run_tool_with_retries
        data = json.loads(run(mock_config, action="open", target="design").reply_text)
        chat = data["items"][1]["hwnd"]
        moved = run_tool_with_retries(None, mock_config, "windowControl",
                                      {"action": "place", "target": str(chat), "monitor": "primary"}, "", "", "")
        assert moved.success
        assert referents()[0].application == "chat" and referents()[0].hwnd == chat

    def test_a_workspace_that_does_not_fit_launches_nothing(self, mock_config, desktop):
        mock_config.windows_workspaces["design"]["items"][1]["zone"] = "nowhere"
        result = run(mock_config, action="open", target="design")
        assert not result.success and "item 2" in result.error_message and "chat" in result.error_message
        assert desktop.browser_launches == [] and referents() == []

    def test_a_failed_placement_is_a_structured_failure_that_is_still_remembered(self, mock_config, desktop):
        desktop.fail_placement = "The application did not reach the requested position."
        result = run(mock_config, action="open", target="design")
        assert not result.success
        data = json.loads(result.reply_text)
        assert data["action"] == "workspace_partial"
        assert [i["outcome"] for i in data["items"]] == ["failed", "failed"]
        assert all(i["launch"] == "accepted" for i in data["items"])
        assert len(desktop.browser_launches) == 2
        assert {r.application for r in referents()} == {"textbooks", "chat"}

    def test_an_unknown_workspace_lists_the_available_names(self, mock_config, desktop):
        result = run(mock_config, action="open", target="gaming")
        assert not result.success and "design" in result.error_message
        assert desktop.browser_launches == []

    def test_a_target_is_required_to_open(self, mock_config, desktop):
        assert not run(mock_config, action="open", target="").success

    def test_opening_is_a_routine_action_without_confirmation(self, mock_config, desktop):
        from jarvis.tools.builtin.windows import WorkspaceControlTool
        from jarvis.tools.confirmation import SafetyTier, evaluate_safety
        verdict = evaluate_safety("workspaceControl", {"action": "open", "target": "design"}, mock_config,
                                  tool=WorkspaceControlTool())
        assert verdict.tier == SafetyTier.SAFE

    def test_disabled_windows_tools_do_nothing(self, mock_config, desktop):
        mock_config.windows_tools_enabled = False
        result = run(mock_config, action="open", target="design")
        assert not result.success and desktop.browser_launches == []


@pytest.mark.unit
class TestList:
    def test_list_shows_names_aliases_and_item_labels_only(self, mock_config, desktop):
        result = run(mock_config, action="list", target="")
        assert result.success
        assert json.loads(result.reply_text) == {"workspaces": [{
            "name": "design", "aliases": ["design project"],
            "items": [{"label": "textbooks", "kind": "browser_window"}, {"label": "chat", "kind": "browser_window"}]}]}
        assert "example.test" not in result.reply_text and ".pdf" not in result.reply_text

    def test_list_records_nothing(self, mock_config, desktop):
        run(mock_config, action="list", target="")
        assert referents() == []


@pytest.mark.unit
class TestCodexSnapshot:
    def test_a_registered_workspace_tool_is_available_to_codex_with_its_strict_schema(self):
        from jarvis.codex_bridge.service import codex_tool_snapshot
        snapshot = codex_tool_snapshot(SimpleNamespace(codex_share_long_term_memory=False, mcps={}))
        entry = snapshot["workspaceControl"]
        assert entry["description"] and entry["inputSchema"]["additionalProperties"] is False
        assert entry["inputSchema"]["properties"]["action"]["enum"] == ["open", "list"]

    def test_an_unregistered_workspace_tool_is_not_offered_to_codex(self):
        from jarvis.codex_bridge.service import codex_tool_snapshot
        from jarvis.tools.registry import BUILTIN_TOOLS
        BUILTIN_TOOLS.pop("workspaceControl")
        assert "workspaceControl" not in codex_tool_snapshot(SimpleNamespace(codex_share_long_term_memory=False, mcps={}))


@pytest.mark.unit
class TestSchemaAndRegistration:
    def test_the_schema_is_small_and_strict(self):
        from jarvis.tools.builtin.windows import WorkspaceControlTool
        schema = WorkspaceControlTool().inputSchema
        assert set(schema["properties"]) == {"action", "target"}
        assert schema["properties"]["action"]["enum"] == ["open", "list"]
        assert schema["required"] == ["action", "target"] and schema["additionalProperties"] is False

    def test_placement_fields_are_not_accepted(self, mock_config, desktop):
        assert not run(mock_config, action="open", target="design", monitor="2").success

    def test_the_other_window_tools_keep_their_short_descriptions(self):
        from jarvis.tools.builtin.windows import AppControlTool, WindowControlTool
        assert "workspace" not in AppControlTool.description.casefold()
        assert "workspace" not in WindowControlTool.description.casefold()

    def test_registered_only_when_workspaces_are_configured(self):
        from jarvis.tools import registry
        ok = workspaces.load_workspaces({"x": {"items": [
            {"kind": "app", "target": "Word", "monitor": "primary"}]}})
        try:
            registry.configure_windows_tools(SimpleNamespace(windows_tools_enabled=True, windows_workspaces=ok),
                                             platform="win32", start_index=False)
            assert "workspaceControl" in registry.BUILTIN_TOOLS
            registry.configure_windows_tools(SimpleNamespace(windows_tools_enabled=True, windows_workspaces={}),
                                             platform="win32", start_index=False)
            assert "workspaceControl" not in registry.BUILTIN_TOOLS
            registry.configure_windows_tools(SimpleNamespace(windows_tools_enabled=True),
                                             platform="win32", start_index=False)
            assert "workspaceControl" not in registry.BUILTIN_TOOLS
            registry.configure_windows_tools(SimpleNamespace(windows_tools_enabled=False, windows_workspaces=ok),
                                             platform="win32", start_index=False)
            assert "workspaceControl" not in registry.BUILTIN_TOOLS
        finally:
            registry.configure_windows_tools(SimpleNamespace(windows_tools_enabled=True),
                                             platform="win32", start_index=False)

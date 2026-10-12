"""The listControl and noteControl tools: results, confirmation, privacy and the cloud boundary."""
from __future__ import annotations

import json

import pytest

from jarvis.memory.lists_notes import ListsNotesStore
from jarvis.tools.confirmation import get_confirmation_store
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries


@pytest.fixture(autouse=True)
def no_pending_confirmation():
    get_confirmation_store().clear_pending()
    yield
    get_confirmation_store().clear_pending()


@pytest.fixture
def cfg(mock_config, tmp_path):
    mock_config.db_path = str(tmp_path / "jarvis.db")
    return mock_config


def call(cfg, tool, **args):
    return run_tool_with_retries(db=None, cfg=cfg, tool_name=tool, tool_args=args, system_prompt="",
                                 original_prompt="", redacted_text="", max_retries=0, language="en")


def data(result):
    assert result.success, result.error_message or result.reply_text
    return json.loads(result.reply_text)


def confirm_with_yes():
    store = get_confirmation_store()
    assert store.has_pending_voice()
    store.handle_voice_response("yes", language="en")


def stored(cfg):
    return ListsNotesStore(cfg.db_path)


@pytest.mark.unit
def test_both_tools_are_always_registered():
    assert "listControl" in BUILTIN_TOOLS
    assert "noteControl" in BUILTIN_TOOLS


@pytest.mark.unit
class TestListControl:
    def test_add_then_show(self, cfg):
        added = data(call(cfg, "listControl", action="add", list="shopping", items=["milk", "eggs"]))
        assert added["added"] == ["milk", "eggs"]
        shown = data(call(cfg, "listControl", action="show", list="Shopping"))
        assert [(i["text"], i["done"]) for i in shown["items"]] == [("milk", False), ("eggs", False)]

    def test_adding_a_single_item_needs_no_confirmation(self, cfg):
        call(cfg, "listControl", action="add", list="shopping", items=["milk"])
        assert not get_confirmation_store().has_pending()

    def test_an_item_given_as_a_string_is_accepted(self, cfg):
        added = data(call(cfg, "listControl", action="add", list="shopping", items="milk"))
        assert added["added"] == ["milk"]

    def test_check_and_remove(self, cfg):
        call(cfg, "listControl", action="add", list="to-do", items=["call mum", "pay rent"])
        assert data(call(cfg, "listControl", action="check", list="todo", items=["pay rent"]))["checked"] == ["pay rent"]
        removed = data(call(cfg, "listControl", action="remove", list="todo", items=["call mum"]))
        assert removed["removed"] == ["call mum"]
        assert [(i["text"], i["done"]) for i in removed["items"]] == [("pay rent", True)]

    def test_lists_names_every_list(self, cfg):
        call(cfg, "listControl", action="add", list="shopping", items=["milk"])
        call(cfg, "listControl", action="add", list="to-do", items=["x"])
        assert [entry["name"] for entry in data(call(cfg, "listControl", action="lists"))["lists"]] == [
            "shopping", "to-do"]

    def test_an_unknown_list_fails_and_names_the_existing_ones(self, cfg):
        call(cfg, "listControl", action="add", list="shopping", items=["milk"])
        result = call(cfg, "listControl", action="show", list="garden")
        assert not result.success
        assert "shopping" in result.error_message

    def test_clearing_asks_first_and_runs_only_after_yes(self, cfg):
        call(cfg, "listControl", action="add", list="shopping", items=["milk", "eggs"])
        asked = call(cfg, "listControl", action="clear", list="shopping list")
        assert not asked.success
        assert "shopping" in asked.reply_text
        assert len(stored(cfg).show("shopping")["items"]) == 2
        confirm_with_yes()
        cleared = data(call(cfg, "listControl", action="clear", list="shopping list"))
        assert cleared["removed_count"] == 2
        assert stored(cfg).show("shopping")["items"] == []

    def test_saying_no_keeps_the_list(self, cfg):
        call(cfg, "listControl", action="add", list="shopping", items=["milk"])
        call(cfg, "listControl", action="delete", list="shopping")
        get_confirmation_store().handle_voice_response("no", language="en")
        result = call(cfg, "listControl", action="delete", list="shopping")
        assert not result.success  # asks again rather than deleting
        assert stored(cfg).show("shopping") is not None

    def test_deleting_a_list_after_yes(self, cfg):
        call(cfg, "listControl", action="add", list="shopping", items=["milk"])
        call(cfg, "listControl", action="delete", list="shopping")
        confirm_with_yes()
        data(call(cfg, "listControl", action="delete", list="shopping"))
        assert stored(cfg).lists() == []

    def test_clearing_a_list_that_does_not_exist_asks_nothing(self, cfg):
        result = call(cfg, "listControl", action="clear", list="garden")
        assert not result.success
        assert not get_confirmation_store().has_pending()

    def test_missing_items_fail_clearly(self, cfg):
        result = call(cfg, "listControl", action="add", list="shopping")
        assert not result.success and result.error_message

    def test_unknown_action_fails(self, cfg):
        assert not call(cfg, "listControl", action="explode", list="shopping").success


@pytest.mark.unit
class TestNoteControl:
    def test_add_then_read_the_last_note(self, cfg):
        data(call(cfg, "noteControl", action="add", text="the wifi password is on the router"))
        notes = data(call(cfg, "noteControl", action="recent"))["notes"]
        assert [n["text"] for n in notes] == ["the wifi password is on the router"]

    def test_search(self, cfg):
        call(cfg, "noteControl", action="add", text="the wifi password is on the router")
        call(cfg, "noteControl", action="add", text="dentist on Tuesday")
        notes = data(call(cfg, "noteControl", action="search", query="WiFi"))["notes"]
        assert [n["text"] for n in notes] == ["the wifi password is on the router"]

    def test_search_without_a_match_succeeds_with_no_notes(self, cfg):
        call(cfg, "noteControl", action="add", text="dentist on Tuesday")
        assert data(call(cfg, "noteControl", action="search", query="passport"))["notes"] == []

    def test_deleting_quotes_the_note_and_runs_only_after_yes(self, cfg):
        note = data(call(cfg, "noteControl", action="add", text="the wifi password is on the router"))["saved"]
        asked = call(cfg, "noteControl", action="delete", id=note["id"])
        assert not asked.success
        assert "wifi password" in asked.reply_text
        assert stored(cfg).recent_notes(1)
        confirm_with_yes()
        data(call(cfg, "noteControl", action="delete", id=note["id"]))
        assert stored(cfg).recent_notes(1) == []

    def test_deleting_without_an_id_targets_the_newest_note(self, cfg):
        call(cfg, "noteControl", action="add", text="older")
        call(cfg, "noteControl", action="add", text="newest one")
        asked = call(cfg, "noteControl", action="delete")
        assert "newest one" in asked.reply_text
        confirm_with_yes()
        call(cfg, "noteControl", action="delete")
        assert [n["text"] for n in stored(cfg).recent_notes(5)] == ["older"]

    def test_deleting_a_missing_note_asks_nothing(self, cfg):
        result = call(cfg, "noteControl", action="delete", id=999)
        assert not result.success
        assert not get_confirmation_store().has_pending()

    def test_an_empty_note_fails(self, cfg):
        assert not call(cfg, "noteControl", action="add", text="  ").success


@pytest.mark.unit
class TestPrivacy:
    def test_logs_and_console_never_carry_list_or_note_content(self, cfg, capsys, monkeypatch):
        from jarvis import debug
        monkeypatch.setattr(debug, "_is_debug_enabled", lambda: True)
        call(cfg, "listControl", action="add", list="secretlist", items=["zebrafish"])
        call(cfg, "listControl", action="show", list="secretlist")
        call(cfg, "listControl", action="clear", list="secretlist")
        call(cfg, "noteControl", action="add", text="platypus code 4471")
        call(cfg, "noteControl", action="search", query="platypus")
        call(cfg, "noteControl", action="delete")
        captured = capsys.readouterr()
        logs = captured.out + captured.err
        for secret in ("secretlist", "zebrafish", "platypus", "4471"):
            assert secret not in logs

    def test_cloud_reply_modes_get_them_only_when_memories_are_shared(self, cfg):
        from jarvis.bridge.tools import build_tool_snapshot
        private = build_tool_snapshot(cfg, share_long_term_memory=False)
        shared = build_tool_snapshot(cfg, share_long_term_memory=True)
        for name in ("listControl", "noteControl"):
            assert name not in private
            assert name in shared

    def test_read_results_count_as_outside_content_and_changes_do_not(self):
        lists, notes = BUILTIN_TOOLS["listControl"], BUILTIN_TOOLS["noteControl"]
        assert lists.returns_outside_content_for({"action": "show"})
        assert lists.returns_outside_content_for({"action": "lists"})
        assert not lists.returns_outside_content_for({"action": "add"})
        assert notes.returns_outside_content_for({"action": "search"})
        assert notes.returns_outside_content_for({"action": "recent"})
        assert not notes.returns_outside_content_for({"action": "add"})

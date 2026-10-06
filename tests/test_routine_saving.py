"""Saving builds a routine from what Jarvis actually did, reads it back, and writes nothing before the user says yes."""
import json
import os
from pathlib import Path

import pytest

from jarvis.routines import store
from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, VoiceResponseStatus, get_confirmation_store
from jarvis.tools.registry import run_tool_with_retries
from routine_fakes import FakeTool

LIGHTS = {"action": {"type": "string"}, "room": {"type": "string"}, "level": {"type": "number"}}


@pytest.fixture
def tools(monkeypatch):
    def add(name, **kwargs):
        tool = FakeTool(name, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool
    get_confirmation_store().clear_pending()
    add("tvControl")
    add("lightsControl", properties=LIGHTS)
    yield add
    get_confirmation_store().clear_pending()


def config_path() -> Path:
    return Path(os.environ["JARVIS_CONFIG_PATH"])


def write_config(data):
    config_path().write_text(json.dumps(data), encoding="utf-8")


def read_config():
    path = config_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def do(cfg, tool, args):
    result = run_tool_with_retries(None, cfg, tool, args, "", "", "")
    assert result.success, result.error_message
    return result


def routine_control(cfg, **args):
    return run_tool_with_retries(None, cfg, "routineControl", args, "", "", "")


def answer(text, cfg):
    confirmations = get_confirmation_store()
    status = confirmations.handle_voice_response(text, "en")
    if status != VoiceResponseStatus.AFFIRMATIVE:
        return None
    return confirmations.run_approved(confirmations.claim_approved(), cfg=cfg)


def do_movie_steps(cfg):
    do(cfg, "tvControl", {"action": "launch", "target": "HDMI 1"})
    do(cfg, "lightsControl", {"action": "set", "level": 30})


def test_save_reads_every_step_back_and_writes_nothing_before_the_answer(tools, mock_config):
    write_config({"user_name": "kept"})
    do_movie_steps(mock_config)
    before = read_config()
    result = routine_control(mock_config, action="save", name="movie mode")
    assert not result.success
    assert ("save the routine movie mode with 2 steps: 1. tvControl launch HDMI 1; 2. lightsControl set 30. "
            "Say yes or no.") in result.reply_text
    assert get_confirmation_store().get_pending().request.tier == SafetyTier.CONFIRM_VOICE
    assert read_config() == before and "routines" not in before
    assert store.current(mock_config) == {}


def test_yes_writes_the_routine_from_the_journal_and_it_is_live_at_once(tools, mock_config):
    write_config({"user_name": "kept"})
    do_movie_steps(mock_config)
    routine_control(mock_config, action="save", name="movie mode")
    result = answer("yes", mock_config)
    assert result.success and "movie mode" in result.reply_text
    saved = read_config()
    assert saved["user_name"] == "kept"
    assert saved["routines"] == {"movie mode": {"steps": [
        {"tool": "tvControl", "args": {"action": "launch", "target": "HDMI 1"}},
        {"tool": "lightsControl", "args": {"action": "set", "level": 30}}]}}
    # Live for the tool and the fast path without a restart.
    assert "movie mode" in store.current(mock_config)
    names = [target.names for target in registry.BUILTIN_TOOLS["routineControl"].fast_targets(mock_config)]
    assert names == [("movie mode",)]
    assert routine_control(mock_config, action="run", name="movie mode").success


def test_no_writes_nothing(tools, mock_config):
    do_movie_steps(mock_config)
    routine_control(mock_config, action="save", name="movie mode")
    assert answer("no", mock_config) is None
    assert "routines" not in read_config() and store.current(mock_config) == {}


def test_chosen_entries_are_saved_in_the_order_given(tools, mock_config):
    do_movie_steps(mock_config)
    recent = json.loads(routine_control(mock_config, action="recent").reply_text)["recent"]
    tv, lights = recent[0]["number"], recent[1]["number"]
    result = routine_control(mock_config, action="save", name="reverse", steps=[lights, tv])
    assert "1. lightsControl set 30; 2. tvControl launch HDMI 1" in result.reply_text
    answer("yes", mock_config)
    assert [step["tool"] for step in read_config()["routines"]["reverse"]["steps"]] == ["lightsControl", "tvControl"]


@pytest.mark.parametrize("steps,problem", [
    ([999], "no recent action numbered 999"),
    (["first"], "list of numbers"),
    ([], "list of numbers"),
])
def test_unknown_or_malformed_numbers_fail_with_nothing_changed(tools, mock_config, steps, problem):
    do_movie_steps(mock_config)
    result = routine_control(mock_config, action="save", name="movie mode", steps=steps)
    assert not result.success and problem in result.error_message
    assert not get_confirmation_store().has_pending() and "routines" not in read_config()


def test_a_repeated_number_fails(tools, mock_config):
    do_movie_steps(mock_config)
    number = json.loads(routine_control(mock_config, action="recent").reply_text)["recent"][0]["number"]
    result = routine_control(mock_config, action="save", name="movie mode", steps=[number, number])
    assert not result.success and "only once" in result.error_message


def test_an_expired_entry_cannot_be_saved(tools, mock_config):
    do_movie_steps(mock_config)
    mock_config.dialogue_memory_timeout = 0
    result = routine_control(mock_config, action="save", name="movie mode")
    assert not result.success and result.error_message == "I have not done anything in this conversation to save yet."


def test_saving_with_nothing_done_says_so(tools, mock_config):
    result = routine_control(mock_config, action="save", name="movie mode")
    assert result.error_message == "I have not done anything in this conversation to save yet."


def test_a_draft_that_could_not_run_is_never_saved(tools, mock_config, monkeypatch):
    do_movie_steps(mock_config)
    monkeypatch.delitem(registry.BUILTIN_TOOLS, "lightsControl")
    result = routine_control(mock_config, action="save", name="movie mode")
    assert not result.success
    assert "step 2 of 2 (lightsControl set 30): this tool is not available here" in result.error_message
    assert not get_confirmation_store().has_pending()


def test_the_draft_must_still_match_when_the_user_answers(tools, mock_config):
    do_movie_steps(mock_config)
    routine_control(mock_config, action="save", name="movie mode")
    # The journal changes before the answer: the approval no longer matches and a fresh question is asked.
    from jarvis.routines.journal import get_journal
    get_journal().record("tvControl", {"action": "key", "target": "Home"}, {"properties": {"action": {}, "target": {}}})
    result = answer("yes", mock_config)
    assert not result.success and "with 3 steps" in result.reply_text and "Say yes or no" in result.reply_text
    assert "routines" not in read_config()


def test_saving_under_an_existing_name_replaces_it_and_says_so_keeping_its_aliases_and_other_keys(
        tools, mock_config):
    write_config({"routines": {"Movie Mode": {"aliases": ["film night"], "colour": "red",
                                              "steps": [{"tool": "tvControl", "args": {"action": "off"}}]}}})
    store.reload()
    do_movie_steps(mock_config)
    result = routine_control(mock_config, action="save", name="movie mode")
    assert "(it replaces your existing Movie Mode)" in result.reply_text
    answer("yes", mock_config)
    assert read_config()["routines"] == {"movie mode": {"aliases": ["film night"], "colour": "red", "steps": [
        {"tool": "tvControl", "args": {"action": "launch", "target": "HDMI 1"}},
        {"tool": "lightsControl", "args": {"action": "set", "level": 30}}]}}


def test_saving_under_another_routines_alias_fails(tools, mock_config):
    write_config({"routines": {"movie mode": {"aliases": ["film night"], "steps": [{"tool": "tvControl"}]}}})
    store.reload()
    do_movie_steps(mock_config)
    result = routine_control(mock_config, action="save", name="Film Night")
    assert not result.success and "alias" in result.error_message
    assert not get_confirmation_store().has_pending()


def test_unknown_keys_and_routines_that_failed_to_load_survive_a_save(tools, mock_config):
    original = {
        "user_name": "kept",
        "_config_version": 6,
        "routines": {
            "broken": {"steps": []},
            "other": {"aliases": ["o"], "note": {"deep": [1, 2]}, "steps": [
                {"tool": "tvControl", "args": {"action": "off"}, "colour": "blue"}]},
        },
    }
    write_config(original)
    store.reload()
    do_movie_steps(mock_config)
    routine_control(mock_config, action="save", name="movie mode")
    answer("yes", mock_config)
    saved = read_config()
    assert saved["user_name"] == "kept" and saved["_config_version"] == 6
    assert saved["routines"]["broken"] == original["routines"]["broken"]
    assert saved["routines"]["other"] == original["routines"]["other"]
    assert "movie mode" in saved["routines"]


def test_delete_confirms_then_removes_only_that_routine(tools, mock_config):
    write_config({"routines": {"movie mode": {"steps": [{"tool": "tvControl"}]},
                               "bedtime": {"steps": [{"tool": "tvControl"}]}}})
    store.reload()
    result = routine_control(mock_config, action="delete", name="Movie Mode")
    assert "delete the routine movie mode" in result.reply_text
    assert "movie mode" in read_config()["routines"]
    answer("yes", mock_config)
    assert list(read_config()["routines"]) == ["bedtime"]
    assert list(store.current(mock_config)) == ["bedtime"]


def test_deleting_the_last_routine_removes_the_setting(tools, mock_config):
    write_config({"user_name": "kept", "routines": {"movie mode": {"steps": [{"tool": "tvControl"}]}}})
    store.reload()
    routine_control(mock_config, action="delete", name="movie mode")
    answer("yes", mock_config)
    saved = read_config()
    assert "routines" not in saved and saved["user_name"] == "kept"


def test_rename_confirms_and_keeps_aliases_and_steps(tools, mock_config):
    write_config({"routines": {"movie mode": {"aliases": ["film night"], "steps": [
        {"tool": "tvControl", "args": {"action": "off"}}]}}})
    store.reload()
    result = routine_control(mock_config, action="rename", name="film night", new_name="cinema")
    assert "rename the routine movie mode to cinema" in result.reply_text
    answer("yes", mock_config)
    assert read_config()["routines"] == {"cinema": {"aliases": ["film night"], "steps": [
        {"tool": "tvControl", "args": {"action": "off"}}]}}
    assert list(store.current(mock_config)) == ["cinema"]


def test_rename_to_a_name_another_routine_claims_fails(tools, mock_config):
    write_config({"routines": {"movie mode": {"steps": [{"tool": "tvControl"}]},
                               "bedtime": {"aliases": ["night"], "steps": [{"tool": "tvControl"}]}}})
    store.reload()
    for taken in ("bedtime", "Night"):
        result = routine_control(mock_config, action="rename", name="movie mode", new_name=taken)
        assert not result.success and "already used" in result.error_message
    assert not get_confirmation_store().has_pending()


def test_a_change_to_a_routine_after_the_question_asks_afresh(tools, mock_config):
    write_config({"routines": {"movie mode": {"steps": [{"tool": "tvControl"}]}}})
    store.reload()
    routine_control(mock_config, action="delete", name="movie mode")
    write_config({"routines": {"movie mode": {"steps": [{"tool": "lightsControl"}]}}})
    store.reload()
    result = answer("yes", mock_config)
    assert not result.success and "Say yes or no" in result.reply_text
    assert "movie mode" in read_config()["routines"]


def test_a_routines_value_that_is_not_an_object_is_never_overwritten(tools, mock_config):
    write_config({"routines": ["not", "an", "object"]})
    do_movie_steps(mock_config)
    routine_control(mock_config, action="save", name="movie mode")
    result = answer("yes", mock_config)
    assert not result.success and "nothing was changed" in result.error_message
    assert read_config()["routines"] == ["not", "an", "object"]


def test_a_failed_write_changes_nothing(tools, mock_config, monkeypatch):
    import jarvis.config as config
    do_movie_steps(mock_config)
    routine_control(mock_config, action="save", name="movie mode")
    monkeypatch.setattr(config, "_save_json", lambda path, data: False)
    result = answer("yes", mock_config)
    assert not result.success and "could not be written" in result.error_message
    assert store.current(mock_config) == {}


def test_changes_always_confirm_and_are_never_safe(tools, mock_config):
    from jarvis.tools.confirmation import evaluate_safety
    write_config({"routines": {"movie mode": {"steps": [{"tool": "tvControl"}]}}})
    store.reload()
    do_movie_steps(mock_config)
    tool = registry.BUILTIN_TOOLS["routineControl"]
    for args in ({"action": "save", "name": "new"}, {"action": "delete", "name": "movie mode"},
                 {"action": "rename", "name": "movie mode", "new_name": "cinema"}):
        assert evaluate_safety("routineControl", args, mock_config, tool=tool).tier == SafetyTier.CONFIRM_VOICE

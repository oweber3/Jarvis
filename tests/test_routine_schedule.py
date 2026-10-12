"""Scheduled routines: off unless a routine has a schedule, run at their time of day through central safety,
never auto-confirmed, and a briefing routine reads out what its steps found."""
import json
import os
from pathlib import Path

import pytest

from jarvis.routines import definitions, store
from jarvis.scheduler import phrases
from jarvis.scheduler.routine_runs import run_scheduled_routine
from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, VoiceResponseStatus, get_confirmation_store
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult
from routine_fakes import FakeTool, failing
from scheduler_fakes import Announcements, FakeClock, local_ts, make_service

MONDAY_0659 = local_ts(2026, 10, 12, 6, 59)  # 12 October 2026 is a Monday
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


@pytest.fixture
def tools(monkeypatch):
    def add(name, **kwargs):
        tool = FakeTool(name, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()


def config_path() -> Path:
    return Path(os.environ["JARVIS_CONFIG_PATH"])


def write_config(data):
    config_path().write_text(json.dumps(data), encoding="utf-8")


def read_config():
    return json.loads(config_path().read_text(encoding="utf-8"))


def routine_control(cfg, **args):
    return run_tool_with_retries(None, cfg, "routineControl", args, "", "", "")


def answer_yes(cfg):
    confirmations = get_confirmation_store()
    assert confirmations.handle_voice_response("yes", "en") == VoiceResponseStatus.AFFIRMATIVE
    return confirmations.run_approved(confirmations.claim_approved(), cfg=cfg)


# --- loading ----------------------------------------------------------------------------------

def test_a_schedule_is_loaded_with_every_day_by_default():
    routines = definitions.load_routines({
        "briefing": {"steps": [{"tool": "getTime"}], "schedule": {"time": "07:00"}},
        "evening": {"steps": [{"tool": "getTime"}], "schedule": {"time": "19:30", "days": ["sat", "sun"]}},
        "plain": {"steps": [{"tool": "getTime"}]}})
    assert routines["briefing"].schedule == definitions.Schedule(7, 0, frozenset(range(7)), True)
    assert routines["evening"].schedule.days == frozenset({5, 6})
    assert routines["plain"].schedule is None


@pytest.mark.parametrize("schedule", [
    "07:00", {"time": "7am"}, {"time": "25:00"}, {"days": ["mon"]}, {"time": "07:00", "days": ["funday"]},
    {"time": "07:00", "days": []}, {"time": "07:00", "enabled": "yes"}])
def test_a_malformed_schedule_is_ignored_but_the_routine_is_kept(schedule):
    routines = definitions.load_routines({"briefing": {"steps": [{"tool": "getTime"}], "schedule": schedule}})
    assert routines["briefing"].schedule is None


def test_a_disabled_schedule_is_kept_but_off():
    routines = definitions.load_routines({"briefing": {"steps": [{"tool": "getTime"}],
                                                       "schedule": {"time": "07:00", "enabled": False}}})
    assert routines["briefing"].schedule.enabled is False


# --- the scheduler starts it at its time --------------------------------------------------------

def _scheduler(tmp_path, routines, ran):
    clock, said = FakeClock(MONDAY_0659), Announcements()
    service = make_service(tmp_path, clock, said, routines=lambda: routines.values(), run_routine=ran.append)
    service.start(thread=False)
    return service, clock


def test_a_scheduled_routine_starts_at_its_time_on_its_days_only(tmp_path):
    routines = definitions.load_routines({
        "briefing": {"steps": [{"tool": "getTime"}], "schedule": {"time": "07:00", "days": WEEKDAYS}},
        "weekend": {"steps": [{"tool": "getTime"}], "schedule": {"time": "07:00", "days": ["sat"]}},
        "off": {"steps": [{"tool": "getTime"}], "schedule": {"time": "07:00", "enabled": False}},
        "manual": {"steps": [{"tool": "getTime"}]}})
    ran = []
    service, clock = _scheduler(tmp_path, routines, ran)
    clock.advance(30)
    service.tick()
    assert ran == []
    clock.advance(30)  # 07:00
    service.tick()
    assert [routine.name for routine in ran] == ["briefing"]
    clock.advance(60)
    service.tick()
    assert len(ran) == 1  # once a day
    clock.advance(86400)  # Tuesday
    service.tick()
    assert [routine.name for routine in ran] == ["briefing", "briefing"]
    service.stop()


def test_a_run_whose_time_passed_long_ago_while_asleep_is_skipped(tmp_path):
    routines = definitions.load_routines({"briefing": {"steps": [{"tool": "getTime"}],
                                                       "schedule": {"time": "07:00"}}})
    ran = []
    service, clock = _scheduler(tmp_path, routines, ran)
    clock.advance(4 * 3600)  # the computer slept through 07:00
    service.tick()
    assert ran == []
    service.stop()


def test_nothing_runs_for_times_before_jarvis_started(tmp_path):
    routines = definitions.load_routines({"briefing": {"steps": [{"tool": "getTime"}],
                                                       "schedule": {"time": "06:58"}}})
    ran = []
    service, clock = _scheduler(tmp_path, routines, ran)
    service.tick()
    assert ran == []
    service.stop()


# --- running through central safety -------------------------------------------------------------

def test_a_scheduled_run_goes_through_central_safety_like_a_manual_one(tools, mock_config):
    tv = tools("tvControl")
    lights = tools("lightsControl")
    store.load({"wake up": {"steps": [{"tool": "tvControl", "args": {"action": "launch", "target": "News"}},
                                      {"tool": "lightsControl", "args": {"action": "on"}}],
                            "schedule": {"time": "07:00"}}})
    said = Announcements()
    run_scheduled_routine(store.current(mock_config)["wake up"], mock_config, announce=said)
    assert tv.calls == [{"action": "launch", "target": "News"}] and lights.calls == [{"action": "on"}]
    assert said.said == []  # an action routine that worked is not spoken


def test_a_step_that_needs_confirmation_is_never_auto_confirmed(tools, mock_config):
    tv = tools("tvControl")
    files = tools("localFiles", tier=SafetyTier.CONFIRM_VOICE)
    store.load({"tidy": {"steps": [{"tool": "tvControl", "args": {"action": "launch", "target": "News"}},
                                   {"tool": "localFiles", "args": {"action": "delete", "target": "old.txt"}}],
                         "schedule": {"time": "07:00"}}})
    said = Announcements()
    run_scheduled_routine(store.current(mock_config)["tidy"], mock_config, announce=said)
    assert tv.calls == [] and files.calls == []  # nothing runs, not even the safe step
    assert get_confirmation_store().get_pending() is None
    assert said.said == [phrases.text("routine_needs_confirmation", "en", routine="tidy")]


def test_a_denied_or_missing_step_is_reported_and_nothing_runs(tools, mock_config):
    tv = tools("tvControl")
    store.load({"broken": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}},
                                     {"tool": "noSuchTool"}], "schedule": {"time": "07:00"}}})
    said = Announcements()
    run_scheduled_routine(store.current(mock_config)["broken"], mock_config, announce=said)
    assert tv.calls == []
    assert len(said.said) == 1 and "not available" in said.said[0]


def test_a_failed_step_in_an_action_routine_is_announced(tools, mock_config):
    tools("tvControl", behaviour=failing("The TV is not answering."))
    store.load({"wake up": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}}],
                            "schedule": {"time": "07:00"}}})
    said = Announcements()
    run_scheduled_routine(store.current(mock_config)["wake up"], mock_config, announce=said)
    assert len(said.said) == 1 and "The TV is not answering" in said.said[0]


def test_a_scheduled_run_holds_the_shared_query_lock(tools, mock_config):
    events = []
    tools("tvControl", behaviour=lambda args: events.append("step") or ToolExecutionResult(True, "ok"))
    store.load({"wake up": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}}],
                            "schedule": {"time": "07:00"}}})

    class Lock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *exc):
            events.append("unlock")

    run_scheduled_routine(store.current(mock_config)["wake up"], mock_config, announce=Announcements(),
                          exclusive=Lock)
    assert events == ["lock", "step", "unlock"]


# --- briefings ----------------------------------------------------------------------------------

def _briefing(tools):
    tools("getWeather", properties={"location": {"type": "string"}},
          behaviour=lambda args: ToolExecutionResult(True, "London: 14 C, light rain"))
    tools("getTime", properties={"local_only": {"type": "boolean"}},
          behaviour=lambda args: ToolExecutionResult(True, "Monday 12 October 2026, 07:00 BST"))
    store.load({"morning briefing": {"report": "results", "schedule": {"time": "07:00", "days": WEEKDAYS},
                                     "steps": [{"tool": "getWeather"}, {"tool": "getTime"}]}})


def test_a_scheduled_briefing_speaks_what_its_steps_found(tools, mock_config):
    _briefing(tools)
    seen = {}

    def compose(cfg, name, outputs, language):
        seen.update(name=name, outputs=outputs)
        return "Good morning. It is 7 and 14 degrees with light rain in London."

    said = Announcements()
    run_scheduled_routine(store.current(mock_config)["morning briefing"], mock_config, announce=said,
                          compose=compose)
    assert said.said == ["Good morning. It is 7 and 14 degrees with light rain in London."]
    assert seen["name"] == "morning briefing"
    assert [label for label, _ in seen["outputs"]] == ["getWeather", "getTime"]
    assert "light rain" in seen["outputs"][0][1]


def test_a_briefing_that_cannot_be_composed_says_so_instead_of_staying_silent(tools, mock_config):
    _briefing(tools)
    said = Announcements()
    run_scheduled_routine(store.current(mock_config)["morning briefing"], mock_config, announce=said,
                          compose=lambda *args: None)
    assert said.said == [phrases.text("briefing_unavailable", "en", routine="Morning briefing")]


def test_running_a_briefing_by_hand_gives_the_model_the_step_results(tools, mock_config):
    _briefing(tools)
    result = routine_control(mock_config, action="run", name="morning briefing")
    assert result.success
    assert "light rain" in result.reply_text and "07:00 BST" in result.reply_text


def test_an_action_routine_still_reports_only_labels(tools, mock_config):
    tools("tvControl", behaviour=lambda args: ToolExecutionResult(True, "secret output"))
    store.load({"movie": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}}]}})
    result = routine_control(mock_config, action="run", name="movie")
    assert result.success and "secret output" not in result.reply_text


def test_a_briefing_is_not_a_fast_command_so_the_model_can_read_it_out(tools, mock_config):
    _briefing(tools)
    store.load({**read_routines_value(), "movie": {"steps": [{"tool": "getTime"}]}})
    names = [target.open_target for target in BUILTIN_TOOLS["routineControl"].fast_targets(mock_config)]
    assert names == ["movie"]


def read_routines_value():
    return {"morning briefing": {"report": "results", "steps": [{"tool": "getWeather"}, {"tool": "getTime"}]}}


# --- scheduling by voice ------------------------------------------------------------------------

def test_scheduling_a_routine_reads_back_and_writes_only_after_yes(tools, mock_config):
    tools("getTime", properties={"local_only": {"type": "boolean"}})
    write_config({"routines": {"briefing": {"steps": [{"tool": "getTime"}]}}})
    store.reload()
    result = routine_control(mock_config, action="schedule", name="briefing", time="07:00", days=WEEKDAYS)
    assert not result.success
    assert "07:00" in result.reply_text and "briefing" in result.reply_text
    assert get_confirmation_store().get_pending().request.tier == SafetyTier.CONFIRM_VOICE
    assert "schedule" not in read_config()["routines"]["briefing"]
    outcome = answer_yes(mock_config)
    assert outcome.success, outcome
    assert read_config()["routines"]["briefing"]["schedule"] == {"time": "07:00", "days": WEEKDAYS, "enabled": True}
    assert store.current(mock_config)["briefing"].schedule.days == frozenset(range(5))


def test_turning_a_schedule_off_keeps_its_time(tools, mock_config):
    tools("getTime", properties={"local_only": {"type": "boolean"}})
    write_config({"routines": {"briefing": {"steps": [{"tool": "getTime"}], "schedule": {"time": "07:00"}}}})
    store.reload()
    routine_control(mock_config, action="schedule", name="briefing", enabled=False)
    assert answer_yes(mock_config).success
    assert read_config()["routines"]["briefing"]["schedule"] == {"time": "07:00", "enabled": False}
    assert store.current(mock_config)["briefing"].schedule.enabled is False


@pytest.mark.parametrize("args", [{"time": "7am"}, {"time": "07:00", "days": ["someday"]}, {}])
def test_an_invalid_schedule_request_fails_with_nothing_asked(tools, mock_config, args):
    tools("getTime", properties={"local_only": {"type": "boolean"}})
    write_config({"routines": {"briefing": {"steps": [{"tool": "getTime"}]}}})
    store.reload()
    result = routine_control(mock_config, action="schedule", name="briefing", **args)
    assert not result.success and result.error_message
    assert get_confirmation_store().get_pending() is None


def test_listing_shows_each_schedule(tools, mock_config):
    tools("getTime", properties={"local_only": {"type": "boolean"}})
    store.load({"briefing": {"steps": [{"tool": "getTime"}], "schedule": {"time": "07:00", "days": WEEKDAYS}}})
    listing = json.loads(routine_control(mock_config, action="list").reply_text)["routines"][0]
    assert listing["schedule"] == {"time": "07:00", "days": WEEKDAYS, "enabled": True}


def test_saving_a_briefing_marks_it_to_read_out_results(tools, mock_config):
    tools("getTime", properties={"local_only": {"type": "boolean"}})
    write_config({})
    run_tool_with_retries(None, mock_config, "getTime", {"local_only": True}, "", "", "")
    result = routine_control(mock_config, action="save", name="morning briefing", report="results")
    assert not result.success and "read out" in result.reply_text
    assert answer_yes(mock_config).success
    assert read_config()["routines"]["morning briefing"]["report"] == "results"

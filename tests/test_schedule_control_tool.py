"""scheduleControl: timers, alarms and reminders set from tool arguments (seconds or ISO times)."""
import pytest

from jarvis.scheduler import phrases, runtime
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from scheduler_fakes import Announcements, FakeClock, local_ts, make_service

START = local_ts(2026, 10, 12, 12, 0)


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def said():
    return Announcements()


@pytest.fixture
def service(tmp_path, clock, said):
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    runtime.set_service(service)
    yield service
    runtime.set_service(None)
    service.stop()


def call(cfg, **args):
    return run_tool_with_retries(None, cfg, "scheduleControl", args, "", "", "")


def test_the_tool_is_registered_and_its_description_leads_with_timers():
    tool = BUILTIN_TOOLS["scheduleControl"]
    assert "timer" in tool.description[:120].lower() and "remind" in tool.description[:120].lower()
    assert len(tool.description) <= 240


def test_setting_a_named_timer_confirms_it_and_it_fires(service, clock, said, mock_config):
    result = call(mock_config, action="set_timer", seconds=600, label="pasta")
    assert result.success
    assert result.reply_text == phrases.text("timer_set", "en", label="pasta",
                                             duration=phrases.format_duration(600, "en"))
    clock.advance(600)
    service.tick()
    assert said.said == [phrases.text("timer_done", "en", label="pasta")]


def test_time_left_on_a_named_timer(service, clock, mock_config):
    call(mock_config, action="set_timer", seconds=600, label="pasta")
    call(mock_config, action="set_timer", seconds=300, label="eggs")
    clock.advance(330)
    result = call(mock_config, action="list", kind="timer", label="Pasta")
    assert result.success
    assert result.reply_text == phrases.text("timer_left", "en", label="pasta",
                                             duration=phrases.format_duration(270, "en"))


def test_listing_everything_names_each_item(service, mock_config):
    call(mock_config, action="set_timer", seconds=600, label="pasta")
    call(mock_config, action="set_reminder", at="14:30", label="call the dentist")
    call(mock_config, action="set_reminder", at="2026-10-13T07:00")
    text = call(mock_config, action="list").reply_text.lower()
    assert "pasta" in text and "call the dentist" in text
    assert phrases.clock_time(local_ts(2026, 10, 13, 7, 0), "en") in text


def test_nothing_set_says_so(service, mock_config):
    result = call(mock_config, action="list", kind="timer")
    assert result.success and result.reply_text == phrases.text("none_timer", "en")


def test_a_reminder_at_a_clock_time_later_today(service, clock, said, mock_config):
    result = call(mock_config, action="set_reminder", at="14:30", label="call the dentist")
    assert result.success
    due = local_ts(2026, 10, 12, 14, 30)
    assert result.reply_text == phrases.text("reminder_set", "en", when=phrases.format_when(due, START, "en"),
                                             message="call the dentist")
    clock.now = due
    service.tick()
    assert said.said == [phrases.text("reminder_due", "en", message="call the dentist")]


def test_a_clock_time_that_has_passed_today_means_tomorrow(service, mock_config):
    call(mock_config, action="set_reminder", at="07:00")
    (item,) = service.pending()
    assert item.kind == "alarm"
    assert item.due == local_ts(2026, 10, 13, 7, 0)


def test_a_reminder_in_a_relative_number_of_seconds(service, mock_config):
    call(mock_config, action="set_reminder", seconds=1200, label="check the oven")
    (item,) = service.pending()
    assert (item.kind, item.due) == ("reminder", START + 1200)


def test_a_full_date_and_time_and_one_with_an_offset(service, mock_config):
    call(mock_config, action="set_reminder", at="2026-10-13T09:00:00", label="email Sam")
    call(mock_config, action="set_reminder", at="2026-10-13T09:00:00+00:00", label="utc")
    items = {item.label: item.due for item in service.pending()}
    assert items["email Sam"] == local_ts(2026, 10, 13, 9, 0)
    from datetime import datetime, timezone
    assert items["utc"] == datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("args", [
    {"action": "set_timer"},
    {"action": "set_timer", "seconds": 0},
    {"action": "set_timer", "seconds": -5},
    {"action": "set_timer", "seconds": 10 ** 9},
    {"action": "set_reminder"},
    {"action": "set_reminder", "at": "half past never"},
    {"action": "set_reminder", "at": "2026-10-11T09:00"},  # already passed
    {"action": "set_timer", "seconds": 60, "label": "x" * 200},
])
def test_invalid_times_fail_with_nothing_set(service, mock_config, args):
    result = call(mock_config, **args)
    assert not result.success and result.error_message
    assert service.pending() == []


def test_cancelling_by_name(service, clock, said, mock_config):
    call(mock_config, action="set_timer", seconds=600, label="pasta")
    call(mock_config, action="set_timer", seconds=300, label="eggs")
    result = call(mock_config, action="cancel", label="pasta")
    assert result.success and result.reply_text == phrases.text("cancelled_timer", "en", label="pasta")
    assert [item.label for item in service.pending()] == ["eggs"]
    clock.advance(700)
    service.tick()
    assert said.said == [phrases.text("timer_done", "en", label="eggs")]


def test_cancelling_the_only_timer_without_a_name(service, mock_config):
    call(mock_config, action="set_timer", seconds=600)
    call(mock_config, action="set_reminder", at="14:30", label="call the dentist")
    result = call(mock_config, action="cancel", kind="timer")
    assert result.success
    assert [item.kind for item in service.pending()] == ["reminder"]


def test_an_ambiguous_cancel_asks_which_and_cancels_nothing(service, mock_config):
    call(mock_config, action="set_timer", seconds=600, label="pasta")
    call(mock_config, action="set_timer", seconds=300, label="eggs")
    result = call(mock_config, action="cancel", kind="timer")
    assert not result.success
    assert "pasta" in result.error_message.lower() and "eggs" in result.error_message.lower()
    assert len(service.pending()) == 2


def test_cancelling_something_that_does_not_exist_fails(service, mock_config):
    result = call(mock_config, action="cancel", label="pizza")
    assert not result.success and result.error_message == phrases.text("no_match", "en")


def test_without_a_running_scheduler_the_tool_fails_honestly(mock_config):
    runtime.set_service(None)
    result = call(mock_config, action="set_timer", seconds=60)
    assert not result.success and result.error_message


def test_every_action_is_safe(mock_config):
    tool = BUILTIN_TOOLS["scheduleControl"]
    from jarvis.tools.confirmation import SafetyTier
    for action in ("set_timer", "set_reminder", "list", "cancel"):
        assert tool.classify_safety({"action": action}, mock_config).tier == SafetyTier.SAFE


def test_reminders_are_personal_data_for_the_reply_bridges():
    assert BUILTIN_TOOLS["scheduleControl"].personal_data is True

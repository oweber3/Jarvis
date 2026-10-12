"""Timers are deterministic fast commands for the obvious phrasings; anything unsure falls through."""
import pytest

from jarvis.fastpath.matcher import match

TOOLS = {"getTime", "systemVolume", "mediaControl", "routineControl", "scheduleControl"}


def route(text, language="en"):
    return match(text, language, available_tools=TOOLS)


@pytest.mark.parametrize("text,seconds,label", [
    ("set a timer for 5 minutes", 300, None),
    ("Set a timer for 10 minutes please", 600, None),
    ("set a 10 minute timer", 600, None),
    ("set a 10-minute timer", 600, None),
    ("start a timer for 30 seconds", 30, None),
    ("timer for 2 hours", 7200, None),
    ("set a timer for 1 hour 30 minutes", 5400, None),
    ("set a timer for five minutes", 300, None),
    ("set a timer for an hour", 3600, None),
    ("set a timer for half an hour", 1800, None),
    ("set a 10 minute pasta timer", 600, "pasta"),
    ("Set a 3 minute boiled egg timer", 180, "boiled egg"),
    ("set a pasta timer for 10 minutes", 600, "pasta"),
])
def test_setting_a_timer(text, seconds, label):
    result = route(text)
    assert result is not None, text
    assert result.tool_name == "scheduleControl"
    expected = {"action": "set_timer", "seconds": seconds}
    if label:
        expected["label"] = label
    assert result.args == expected


@pytest.mark.parametrize("text,label", [
    ("how long left on the pasta timer", "pasta"),
    ("how much time is left on my pasta timer", "pasta"),
    ("how long is left on the timer", None),
    ("how much time is left on the timer", None),
])
def test_time_left(text, label):
    result = route(text)
    assert result is not None, text
    expected = {"action": "list", "kind": "timer"}
    if label:
        expected["label"] = label
    assert (result.tool_name, result.args) == ("scheduleControl", expected)


@pytest.mark.parametrize("text,label", [
    ("cancel the pasta timer", "pasta"),
    ("stop my pasta timer", "pasta"),
    ("cancel the timer", None),
    ("cancel my timer", None),
])
def test_cancelling_a_timer(text, label):
    result = route(text)
    assert result is not None, text
    expected = {"action": "cancel", "kind": "timer"}
    if label:
        expected["label"] = label
    assert (result.tool_name, result.args) == ("scheduleControl", expected)


def test_listing_timers_and_reminders():
    assert route("what timers do i have").args == {"action": "list", "kind": "timer"}
    assert route("what are my reminders").args == {"action": "list", "kind": "reminder"}


@pytest.mark.parametrize("text", [
    "set a timer",  # no duration: the model asks
    "set a timer for 0 minutes",
    "set a timer for 5 minutes and play music",
    "set a timer for 5 minutes then pause",
    "remind me at 2:30 to call the dentist",  # clock times are the model's job
    "wake me at 7",
    "set a timer for 30 hours",  # longer than a day falls through
    "set a 10 minute 5 timer",  # a name with a number is not a name
    "set a timer for 5 parsecs",
])
def test_uncertain_timer_requests_fall_through(text):
    assert route(text) is None, text


def test_without_the_tool_nothing_matches():
    assert match("set a timer for 5 minutes", "en", available_tools={"getTime"}) is None


def test_the_tool_reply_is_used_rather_than_a_template():
    assert route("set a timer for 5 minutes").reply_template is None


def test_a_fast_timer_runs_through_the_dispatcher_and_is_set(tmp_path, mock_config):
    from jarvis.fastpath.dispatcher import dispatch, match_command
    from jarvis.scheduler import phrases, runtime
    from scheduler_fakes import Announcements, FakeClock, local_ts, make_service
    clock, said = FakeClock(local_ts(2026, 10, 12, 12, 0)), Announcements()
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    runtime.set_service(service)
    try:
        found = match_command("Set a 10 minute pasta timer", mock_config, "en")
        assert found is not None and found.tool_name == "scheduleControl"
        reply = dispatch(found, None, mock_config, "Set a 10 minute pasta timer", "en")
        assert reply == phrases.text("timer_set", "en", label="pasta", duration=phrases.format_duration(600, "en"))
        clock.advance(600)
        service.tick()
        assert said.said == [phrases.text("timer_done", "en", label="pasta")]
    finally:
        runtime.set_service(None)
        service.stop()

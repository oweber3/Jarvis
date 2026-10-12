"""The scheduler: timers and reminders fire once, survive restarts, and missed ones are announced once."""
import sqlite3

import pytest

from jarvis.scheduler import phrases
from jarvis.scheduler.store import ScheduleStore
from scheduler_fakes import Announcements, FakeClock, local_ts, make_service

START = local_ts(2026, 10, 12, 12, 0)


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def said():
    return Announcements()


def test_a_timer_fires_once_when_it_is_due(tmp_path, clock, said):
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    service.add_timer(600, "pasta", "en")
    clock.advance(599)
    service.tick()
    assert said.said == []
    clock.advance(1)
    service.tick()
    assert said.said == [phrases.text("timer_done", "en", label="pasta")]
    clock.advance(60)
    service.tick()
    assert len(said.said) == 1
    assert service.pending() == []
    service.stop()


def test_several_named_timers_run_side_by_side(tmp_path, clock, said):
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    service.add_timer(300, "eggs", "en")
    service.add_timer(600, "pasta", "en")
    service.add_timer(120, "", "en")
    assert [item.label for item in service.pending()] == ["", "eggs", "pasta"]
    clock.advance(301)
    service.tick()
    assert said.said == [phrases.text("timer_done_unnamed", "en"), phrases.text("timer_done", "en", label="eggs")]
    assert [item.label for item in service.pending(kind="timer")] == ["pasta"]
    service.stop()


def test_cancelled_items_never_fire(tmp_path, clock, said):
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    item = service.add_timer(60, "tea", "en")
    assert service.cancel(item.id) is True
    assert service.cancel(item.id) is False
    clock.advance(120)
    service.tick()
    assert said.said == []
    service.stop()


def test_reminders_and_alarms_say_their_message_or_the_time(tmp_path, clock, said):
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    service.add_reminder(START + 3600, "call the dentist", "en")
    service.add_reminder(START + 7200, "", "en")
    clock.advance(7200)
    service.tick()
    assert said.said[0] == phrases.text("reminder_due", "en", message="call the dentist")
    assert said.said[1] == phrases.text("alarm_due", "en", time=phrases.clock_time(START + 7200, "en"))
    service.stop()


def test_pending_items_survive_a_restart(tmp_path, clock, said):
    first = make_service(tmp_path, clock, said)
    first.start(thread=False)
    first.add_reminder(START + 3600, "call the dentist", "en")
    first.stop()

    second = make_service(tmp_path, clock, said)
    second.start(thread=False)
    assert [item.label for item in second.pending()] == ["call the dentist"]
    clock.advance(3600)
    second.tick()
    assert said.said == [phrases.text("reminder_due", "en", message="call the dentist")]
    second.stop()


def test_items_missed_while_jarvis_was_off_are_announced_once_as_missed(tmp_path, clock, said):
    first = make_service(tmp_path, clock, said)
    first.start(thread=False)
    first.add_reminder(START + 600, "call the dentist", "en")
    first.add_timer(300, "pasta", "en")
    first.add_reminder(START + 86400, "pay rent", "en")
    first.stop()

    clock.advance(3600)  # Jarvis was off when both were due
    second = make_service(tmp_path, clock, said)
    second.start(thread=False)
    assert len(said.said) == 1
    missed = said.said[0]
    assert "call the dentist" in missed and "pasta" in missed and "pay rent" not in missed
    assert [item.label for item in second.pending()] == ["pay rent"]
    second.stop()

    third = make_service(tmp_path, clock, said)
    third.start(thread=False)
    assert len(said.said) == 1  # never announced twice
    third.stop()


def test_an_item_due_a_moment_before_start_still_fires_normally(tmp_path, clock, said):
    first = make_service(tmp_path, clock, said)
    first.start(thread=False)
    first.add_timer(60, "tea", "en")
    first.stop()
    clock.advance(70)
    second = make_service(tmp_path, clock, said)
    second.start(thread=False)
    second.tick()
    assert said.said == [phrases.text("timer_done", "en", label="tea")]
    second.stop()


def test_a_failing_announcement_does_not_stop_later_items(tmp_path, clock):
    heard = []

    def announce(text):
        heard.append(text)
        if len(heard) == 1:
            raise RuntimeError("speaker gone")

    service = make_service(tmp_path, clock, announce)
    service.start(thread=False)
    service.add_timer(10, "one", "en")
    service.add_timer(20, "two", "en")
    clock.advance(30)
    service.tick()
    assert len(heard) == 2
    assert service.pending() == []
    service.stop()


def test_the_store_keeps_its_own_table_in_the_jarvis_database(tmp_path):
    path = str(tmp_path / "jarvis.db")
    store = ScheduleStore(path)
    store.add("timer", "pasta", START + 60, START, "en")
    store.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_items").fetchone()[0] == 1
    finally:
        conn.close()


def test_finished_items_are_pruned_after_a_month(tmp_path, clock, said):
    service = make_service(tmp_path, clock, said)
    service.start(thread=False)
    service.add_timer(10, "old", "en")
    clock.advance(20)
    service.tick()
    service.stop()
    clock.advance(31 * 86400)
    again = make_service(tmp_path, clock, said)
    again.start(thread=False)
    assert again.store.count() == 0
    again.stop()


def test_the_background_thread_fires_a_timer_on_its_own(tmp_path, said):
    import time
    service = make_service(tmp_path, time.time, said)
    service.start()
    try:
        service.add_timer(1, "quick", "en")
        deadline = time.time() + 5
        while not said.said and time.time() < deadline:
            time.sleep(0.05)
        assert said.said == [phrases.text("timer_done", "en", label="quick")]
    finally:
        service.stop()


def test_a_clock_moved_back_never_runs_a_scheduled_routine_twice(tmp_path, clock, said):
    from jarvis.routines import definitions
    routines = definitions.load_routines({"b": {"steps": [{"tool": "getTime"}], "schedule": {"time": "12:05"}}})
    ran = []
    service = make_service(tmp_path, clock, said, routines=lambda: routines.values(), run_routine=ran.append)
    service.start(thread=False)
    clock.advance(301)
    service.tick()
    clock.advance(-120)  # the clock is corrected backwards across 12:05
    service.tick()
    clock.advance(240)
    service.tick()
    assert len(ran) == 1
    service.stop()

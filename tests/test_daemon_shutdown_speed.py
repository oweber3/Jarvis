"""Shutdown responsiveness: a stop request wakes the idle daemon at once, and independent
subsystems stop side by side instead of one after another."""

import threading
import time

import pytest


@pytest.fixture
def daemon():
    from jarvis import daemon as module

    module._clear_stop_request()
    yield module
    module._clear_stop_request()


def test_a_stop_request_wakes_the_idle_daemon_at_once(daemon):
    threading.Timer(0.05, daemon.request_stop).start()
    started = time.monotonic()

    assert daemon._wait_for_stop(5.0) is True
    assert time.monotonic() - started < 1.0
    assert daemon.is_stop_requested() is True


def test_the_idle_wait_ends_quietly_when_nobody_asks_to_stop(daemon):
    assert daemon._wait_for_stop(0.05) is False
    assert daemon.is_stop_requested() is False


def test_a_new_run_forgets_the_previous_stop_request(daemon):
    daemon.request_stop()
    daemon._clear_stop_request()

    assert daemon.is_stop_requested() is False
    assert daemon._wait_for_stop(0.05) is False


def test_independent_subsystems_stop_side_by_side(daemon):
    stopped = []

    def slow(name):
        def stop():
            time.sleep(0.3)
            stopped.append(name)
        return stop

    started = time.monotonic()
    daemon._stop_side_by_side({"voice": slow("voice"), "tools": slow("tools"), "replies": slow("replies")},
                              timeout_sec=5.0)

    assert sorted(stopped) == ["replies", "tools", "voice"]
    assert time.monotonic() - started < 0.75


def test_one_failing_stop_does_not_keep_the_others_from_stopping(daemon):
    stopped = []

    def broken():
        raise RuntimeError("helper already gone")

    daemon._stop_side_by_side({"broken": broken, "fine": lambda: stopped.append("fine")}, timeout_sec=5.0)

    assert stopped == ["fine"]


def test_a_stop_that_hangs_does_not_hold_up_shutdown_past_the_bound(daemon):
    release = threading.Event()
    started = time.monotonic()

    daemon._stop_side_by_side({"stuck": lambda: release.wait(10)}, timeout_sec=0.2)

    assert time.monotonic() - started < 1.0
    release.set()

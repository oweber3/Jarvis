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


def test_the_engine_stop_bound_outlasts_the_mcp_runtimes_own_worst_case(daemon):
    """The daemon never gives up on the MCP runtime before it has killed every stuck server."""
    from jarvis.tools.external import mcp_runtime

    assert daemon._engine_stop_timeout_sec() > mcp_runtime.SHUTDOWN_WORST_CASE_SEC


def test_a_shutdown_step_that_raises_never_skips_the_steps_after_it(daemon):
    # The diary pass, the engine stop (MCP and bridge children) and db.close() come after the phone server,
    # scheduler, web chat, gestures, dictation and voice: one of those failing must not skip them.
    done = []

    def broken():
        raise RuntimeError("store already closed")

    daemon._stop_in_order([("phone", broken), ("diary", lambda: done.append("diary")),
                           ("engines", lambda: done.append("engines")), ("database", lambda: done.append("database"))])
    assert done == ["diary", "engines", "database"]


def test_shutdown_steps_run_in_their_order(daemon):
    done = []
    daemon._stop_in_order([(name, lambda n=name: done.append(n)) for name in ("a", "b", "c")])
    assert done == ["a", "b", "c"]

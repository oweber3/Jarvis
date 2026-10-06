"""Behavioural tests for the background Codex bridge broker (request lifecycle and ownership)."""
import threading

import pytest

from jarvis.bridge.broker import (
    Broker,
    BrokerLimits,
    BusyError,
    PayloadTooLargeError,
    State,
)

THREAD, TURN = "thread-A", "turn-A"
TOOLS = {
    "getTime": {"type": "object", "properties": {}, "required": []},
    "systemVolume": {
        "type": "object",
        "properties": {"action": {"type": "string"}, "percent": {"type": "number"}},
        "required": ["action"],
    },
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def validate(schema, args):
    """Minimal schema validator: required keys only."""
    missing = [k for k in schema.get("required", []) if k not in args]
    return f"missing {missing}" if missing else None


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def broker(clock):
    return Broker(limits=BrokerLimits(deadline_sec=90, queue_limit=1, max_tool_calls=3),
                  clock=clock, validate_args=validate)


def submit(broker, text="open word", context=None):
    return broker.submit_request(text, context or [], "voice", "en", dict(TOOLS))


def submitted(broker, thread=THREAD, **kw):
    req = submit(broker, **kw)
    assert broker.assign_thread(req.id, thread)
    return req


def new_active(broker, thread=THREAD, turn=TURN, **kw):
    """A request whose turn has started, which makes it active."""
    req = submitted(broker, thread, **kw)
    assert broker.assign_turn(req.id, turn)
    return req


def execute(broker, req, call_id, tool, args, thread=THREAD, turn=TURN):
    return broker.begin_execute(req.id, call_id, tool, args, thread, turn)


def finish(broker, req, answer=("completed", "done"), status="completed", turn=TURN, failure=None):
    return broker.finish_turn(req.id, turn, status, failure, answer)


@pytest.mark.unit
class TestRequestCreation:
    def test_ids_are_unguessable_and_distinct(self, broker):
        a, b = submit(broker, "a"), submit(broker, "b")
        assert a.id != b.id and len(a.id) >= 32
        assert broker.state_of(a.id) is State.QUEUED

    def test_busy_beyond_active_plus_queue_limit(self, broker):
        submit(broker, "1")
        submit(broker, "2")
        with pytest.raises(BusyError):
            submit(broker, "3")

    def test_terminal_requests_free_capacity(self, broker):
        a = submit(broker, "1")
        submit(broker, "2")
        broker.cancel(a.id, "stop")
        submit(broker, "3")

    def test_oversized_utterance_rejected(self, broker):
        with pytest.raises(PayloadTooLargeError):
            submit(broker, "x" * 5000)

    def test_the_request_keeps_what_the_turn_carries(self, broker, clock):
        req = submit(broker, "hi", context=[{"role": "user", "content": "earlier"}])
        kept = broker.request_of(req.id)
        assert kept.utterance == "hi" and set(kept.allowed_tools) == set(TOOLS)
        assert kept.context == [{"role": "user", "content": "earlier"}]
        clock.advance(30)
        assert broker.remaining_sec(req.id) == pytest.approx(60)
        assert broker.request_of("nope") is None and broker.remaining_sec("nope") == 0.0


@pytest.mark.unit
class TestOwnership:
    def test_a_thread_is_assigned_once_and_marks_the_request_submitted(self, broker):
        req = submit(broker)
        assert broker.assign_thread(req.id, THREAD)
        assert broker.state_of(req.id) is State.SUBMITTED
        assert not broker.assign_thread(req.id, "thread-B")
        assert broker.owner_of(req.id) == (THREAD, None)

    def test_the_turn_is_assigned_once_and_activates_the_request(self, broker):
        req = submitted(broker)
        assert broker.assign_turn(req.id, TURN)
        assert broker.state_of(req.id) is State.ACTIVE
        assert not broker.assign_turn(req.id, "turn-B")
        assert broker.owner_of(req.id) == (THREAD, TURN)

    def test_a_turn_needs_a_thread_first(self, broker):
        req = submit(broker)
        assert not broker.assign_turn(req.id, TURN)
        assert broker.state_of(req.id) is State.QUEUED

    @pytest.mark.parametrize("thread,turn,reason", [
        ("thread-B", TURN, "wrong_thread"), (THREAD, "turn-B", "wrong_turn"), (None, None, "wrong_thread")])
    def test_another_session_gets_nothing_and_changes_nothing(self, broker, thread, turn, reason):
        req = new_active(broker, text="secret words")
        d = execute(broker, req, "c1", "getTime", {}, thread, turn)
        assert d.kind == "refused" and d.reason == reason and d.text is None
        assert broker.state_of(req.id) is State.ACTIVE

    def test_calls_before_the_turn_is_known_are_refused(self, broker):
        req = submitted(broker)
        assert execute(broker, req, "c1", "getTime", {}).reason == "wrong_turn"

    def test_unknown_request_refused(self, broker):
        assert broker.begin_execute("nope", "c1", "getTime", {}, THREAD, TURN).reason == "unknown_request"


@pytest.mark.unit
class TestDesktopRecords:
    def test_desktop_records_are_a_bounded_snapshot(self, broker):
        records = [{"application": f"App{n}", "hwnd": n} for n in range(9)]
        others = [{"kind": "device", "tool": "tvControl", "device": f"d{n}"} for n in range(9)]
        desktop = {"desktop_referents": records, "other_referents": others,
                   "foreground_window": {"hwnd": 1}, "desktop_referents_note": "note"}
        req = broker.submit_request("move it", [], "voice", "en", dict(TOOLS), desktop=desktop)
        records.clear()  # later changes to the caller's data do not reach the request
        desktop["foreground_window"]["hwnd"] = 2
        shared = broker.request_of(req.id).desktop
        assert [r["hwnd"] for r in shared["desktop_referents"]] == list(range(BrokerLimits().max_referents))
        assert len(shared["other_referents"]) == BrokerLimits().max_other_referents
        assert shared["foreground_window"] == {"hwnd": 1} and shared["desktop_referents_note"] == "note"

    def test_only_known_desktop_keys_reach_a_request(self, broker):
        req = broker.submit_request("move it", [], "voice", "en", dict(TOOLS),
                                    desktop={"desktop_referents": [], "window_titles": ["secret"]})
        assert broker.request_of(req.id).desktop == {}

    def test_a_request_without_records_carries_none(self, broker):
        assert submit(broker).desktop == {}


@pytest.mark.unit
class TestExpiry:
    def test_expired_request_cannot_execute_or_deliver(self, broker, clock):
        req = new_active(broker)
        clock.advance(91)
        assert broker.state_of(req.id) is State.EXPIRED
        assert execute(broker, req, "c1", "getTime", {}).reason == "expired"
        assert not finish(broker, req).deliver

    def test_expire_due_sweeps_non_terminal(self, broker, clock):
        a = submit(broker, "1")
        clock.advance(100)
        broker.expire_due()
        assert broker.state_of(a.id) is State.EXPIRED


@pytest.mark.unit
class TestExecute:
    def test_valid_call_is_cleared_to_run_then_stored(self, broker):
        req = new_active(broker)
        assert execute(broker, req, "c1", "systemVolume", {"action": "get"}).kind == "run"
        broker.record_result(req.id, "c1", "ok", "volume 30%")
        again = execute(broker, req, "c1", "systemVolume", {"action": "get"})
        assert again.kind == "stored" and again.text == "volume 30%"

    @pytest.mark.parametrize("name,args,reason", [
        ("shutdown", {}, "tool_not_allowed"),
        ("systemVolume", {}, "invalid_arguments"),
        ("systemVolume", ["mute"], "invalid_arguments"),
    ])
    def test_unknown_or_malformed_never_run(self, broker, name, args, reason):
        req = new_active(broker)
        d = execute(broker, req, "c1", name, args)
        assert d.kind == "refused" and d.reason == reason

    def test_oversized_arguments_never_run(self, broker):
        req = new_active(broker)
        assert execute(broker, req, "c1", "systemVolume", {"action": "x" * 9000}).reason == "payload_too_large"

    def test_identical_call_under_a_new_call_id_does_not_rerun(self, broker):
        req = new_active(broker)
        assert execute(broker, req, "c1", "systemVolume", {"action": "set", "percent": 30}).kind == "run"
        broker.record_result(req.id, "c1", "ok", "set")
        assert execute(broker, req, "c2", "systemVolume", {"percent": 30, "action": "set"}).kind == "stored"

    def test_a_reused_call_id_with_different_arguments_is_refused(self, broker):
        req = new_active(broker)
        execute(broker, req, "c1", "systemVolume", {"action": "mute"})
        broker.record_result(req.id, "c1", "ok", "muted")
        assert execute(broker, req, "c1", "systemVolume", {"action": "unmute"}).reason == "call_id_reuse"

    def test_in_flight_duplicate_is_refused(self, broker):
        req = new_active(broker)
        execute(broker, req, "c1", "getTime", {})
        d = execute(broker, req, "c1", "getTime", {})
        assert d.kind == "refused" and d.reason == "in_flight"

    def test_call_cap(self, broker):
        req = new_active(broker)
        for i in range(3):
            assert execute(broker, req, f"c{i}", "systemVolume", {"action": "up", "percent": i}).kind == "run"
            broker.record_result(req.id, f"c{i}", "ok", "x")
        d = execute(broker, req, "c9", "systemVolume", {"action": "down", "percent": 9})
        assert d.kind == "refused" and d.reason == "too_many_calls"

    def test_no_actions_after_the_turn_ended(self, broker):
        req = new_active(broker)
        assert finish(broker, req).deliver
        assert execute(broker, req, "c1", "getTime", {}).reason == "request_finished"

    def test_result_text_is_bounded(self, broker):
        req = new_active(broker)
        execute(broker, req, "c1", "getTime", {})
        broker.record_result(req.id, "c1", "ok", "y" * 10000)
        assert len(execute(broker, req, "c1", "getTime", {}).text) <= 4000 + 40


@pytest.mark.unit
class TestUncertainOutcome:
    def test_uncertain_call_never_replays_by_id_or_content(self, broker):
        req = new_active(broker)
        execute(broker, req, "c1", "systemVolume", {"action": "mute"})
        broker.record_uncertain(req.id, "c1")
        for call_id in ("c1", "c2"):
            assert execute(broker, req, call_id, "systemVolume", {"action": "mute"}).kind == "uncertain"

    def test_result_arriving_after_cancel_is_discarded_as_uncertain(self, broker):
        req = new_active(broker)
        execute(broker, req, "c1", "getTime", {})
        broker.cancel(req.id, "stop")
        assert broker.record_result(req.id, "c1", "ok", "late") is False
        assert broker.outcome_of(req.id, "c1") == "uncertain"

    def test_a_failure_while_a_call_runs_leaves_it_uncertain(self, broker):
        req = new_active(broker)
        execute(broker, req, "c1", "getTime", {})
        broker.fail(req.id, "process_exited")
        assert broker.state_of(req.id) is State.FAILED and broker.failure_reason(req.id) == "process_exited"
        assert broker.outcome_of(req.id, "c1") == "uncertain"


@pytest.mark.unit
class TestCompletion:
    def test_the_final_answer_is_delivered_by_a_successful_turn(self, broker):
        req = new_active(broker)
        done = finish(broker, req, ("completed", "Opening Word."))
        assert done.deliver and done.reply == "Opening Word." and not done.is_question
        assert broker.state_of(req.id) is State.COMPLETED

    @pytest.mark.parametrize("status", ["failed", "interrupted"])
    def test_a_failed_or_interrupted_turn_never_delivers_its_answer(self, broker, status):
        req = new_active(broker)
        done = finish(broker, req, ("completed", "It worked!"), status=status,
                      failure="usage_limit" if status == "failed" else None)
        assert not done.deliver and broker.state_of(req.id) is State.FAILED
        assert broker.failure_reason(req.id) == ("usage_limit" if status == "failed" else "interrupted")

    @pytest.mark.parametrize("answer", [
        None, ("banana", "x"), ("completed", "   "), ("completed", None), (None, "hi"), ("completed", 42)])
    def test_a_successful_turn_without_a_valid_answer_is_a_failure(self, broker, answer):
        req = new_active(broker)
        done = finish(broker, req, answer)
        assert not done.deliver and broker.failure_reason(req.id) == "no_answer"

    def test_another_turn_finishing_changes_nothing(self, broker):
        req = new_active(broker)
        assert not finish(broker, req, turn="turn-B").deliver
        assert broker.state_of(req.id) is State.ACTIVE

    def test_failed_and_question_statuses(self, broker):
        a = new_active(broker)
        done = finish(broker, a, ("failed", "I could not."))
        assert done.deliver and done.reason == "reported_failure" and broker.state_of(a.id) is State.FAILED
        b = new_active(broker, "thread-B", "turn-B")
        done = finish(broker, b, ("needs_user_input", "Which Word?"), turn="turn-B")
        assert done.deliver and done.is_question and broker.state_of(b.id) is State.COMPLETED

    def test_reply_is_bounded(self, broker):
        req = new_active(broker)
        assert len(finish(broker, req, ("completed", "z" * 9000)).reply) <= 2000 + 40

    def test_a_request_whose_turn_never_started_cannot_deliver(self, broker):
        req = submitted(broker)
        assert not finish(broker, req).deliver
        assert broker.state_of(req.id) is State.SUBMITTED

    def test_concurrent_turn_completions_deliver_exactly_once(self, broker):
        req = new_active(broker)
        results = []
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            results.append(finish(broker, req).deliver)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count(True) == 1


@pytest.mark.unit
class TestCancellation:
    def test_cancel_blocks_all_later_calls(self, broker):
        req = new_active(broker)
        broker.cancel(req.id, "stop")
        assert broker.state_of(req.id) is State.CANCELLED
        assert execute(broker, req, "c1", "getTime", {}).reason == "cancelled"
        assert not finish(broker, req, ("completed", "late answer")).deliver
        assert broker.state_of(req.id) is State.CANCELLED

    def test_late_answer_for_an_old_request_never_attaches_to_a_newer_one(self, broker):
        old = new_active(broker)
        broker.cancel(old.id, "stop")
        new = new_active(broker, "thread-B", "turn-B")
        assert not finish(broker, old, ("completed", "answer to the old one")).deliver
        assert not broker.finish_turn(new.id, TURN, "completed", None, ("completed", "wrong turn")).deliver
        assert broker.state_of(new.id) is State.ACTIVE

    def test_cancel_all_cancels_every_non_terminal_request(self, broker):
        a = submit(broker, "1")
        b = new_active(broker)
        broker.cancel_all("mode_switch")
        assert broker.state_of(a.id) is State.CANCELLED and broker.state_of(b.id) is State.CANCELLED

    def test_terminal_state_is_immutable(self, broker):
        req = new_active(broker)
        finish(broker, req)
        broker.cancel(req.id, "stop")
        broker.fail(req.id, "process_exited")
        assert broker.state_of(req.id) is State.COMPLETED


@pytest.mark.unit
class TestConfirmation:
    def _awaiting(self, broker):
        req = new_active(broker)
        assert execute(broker, req, "c1", "systemVolume", {"action": "mute"}).kind == "run"
        broker.mark_awaiting_confirmation(req.id, "c1", confirmation_id="conf-1")
        return req

    def test_no_further_calls_while_awaiting(self, broker):
        req = self._awaiting(broker)
        assert broker.state_of(req.id) is State.AWAITING_CONFIRMATION
        d = execute(broker, req, "c2", "getTime", {})
        assert d.kind == "refused" and d.reason == "awaiting_confirmation"

    def test_codex_answer_is_ignored_while_awaiting(self, broker):
        req = self._awaiting(broker)
        assert not finish(broker, req, ("completed", "The user approved.")).deliver
        assert broker.state_of(req.id) is State.AWAITING_CONFIRMATION

    def test_approval_completes_without_delivery_by_codex(self, broker):
        req = self._awaiting(broker)
        broker.confirmation_resolved("conf-1", "approved")
        assert broker.state_of(req.id) is State.COMPLETED
        assert not finish(broker, req, ("completed", "x")).deliver

    @pytest.mark.parametrize("outcome", ["denied", "expired", "replaced", "cancelled"])
    def test_other_outcomes_cancel(self, broker, outcome):
        req = self._awaiting(broker)
        broker.confirmation_resolved("conf-1", outcome)
        assert broker.state_of(req.id) is State.CANCELLED

    def test_unknown_confirmation_is_ignored(self, broker):
        req = self._awaiting(broker)
        broker.confirmation_resolved("other", "approved")
        assert broker.state_of(req.id) is State.AWAITING_CONFIRMATION

    def test_awaiting_request_still_expires(self, broker, clock):
        req = self._awaiting(broker)
        clock.advance(500)
        broker.expire_due()
        assert broker.state_of(req.id) is State.EXPIRED


@pytest.mark.unit
class TestRetention:
    """Finished requests (utterance, context, tool results) are kept only briefly, never for the service's life."""

    def test_old_finished_requests_are_forgotten_and_recent_ones_kept(self, broker):
        ids = []
        for i in range(200):
            req = submit(broker, f"request {i}", context=[{"role": "user", "content": "x" * 500}])
            broker.cancel(req.id, "user_stop")
            ids.append(req.id)
        assert broker.request_of(ids[0]) is None
        assert broker.state_of(ids[-1]) is State.CANCELLED

    def test_live_requests_are_never_forgotten(self, broker):
        live = submit(broker, "still working")
        for i in range(200):
            broker.cancel(submit(broker, f"r{i}").id, "user_stop")
        assert broker.state_of(live.id) is State.QUEUED

"""One jarvis_execute call through the shared runner: confirmations stay tied to a live request."""
import pytest

from jarvis.bridge.broker import Broker, BrokerLimits
from jarvis.bridge.execution import ToolCallRunner
from jarvis.tools.confirmation import ConfirmationRequest, ConfirmationStore, SafetyTier
from jarvis.tools.types import ToolExecutionResult, ToolImage

pytestmark = pytest.mark.unit

THREAD, TURN = "thread-A", "turn-A"
TOOLS = {"deleteFile": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}


def _active(broker):
    req = broker.submit_request("delete the draft", [], "voice", "en", dict(TOOLS))
    assert broker.assign_thread(req.id, THREAD) and broker.assign_turn(req.id, TURN)
    return req


def _asking_executor(store, before_return=None):
    """Registers a voice confirmation for the request, as run_tool_with_retries does."""
    def executor(db, cfg, tool, arguments, *_a, request_ref=None, **_k):
        request = ConfirmationRequest(tool_name=tool, tier=SafetyTier.CONFIRM_VOICE, action="delete",
                                      target="draft.txt", parameters=arguments)
        store.set_pending(request, {"request_ref": request_ref})
        if before_return:
            before_return()
        return ToolExecutionResult(success=False, reply_text="Delete draft.txt?")
    return executor


def _run(runner, req, call_id="c1"):
    envelope = {"request_id": req.id, "tool_name": "deleteFile", "arguments": {"path": "draft.txt"}}
    return runner.execute(req.id, call_id, envelope, THREAD, TURN, None, "en", True, "delete the draft")


def test_a_confirmation_for_a_live_request_waits_for_the_user():
    broker, store = Broker(limits=BrokerLimits()), ConfirmationStore()
    req = _active(broker)
    runner = ToolCallRunner(broker, store, _asking_executor(store), cfg=None)
    data, question, _ = _run(runner, req)
    assert data["status"] == "awaiting_confirmation" and question
    assert store.pending_for_ref(req.id) is not None


def test_a_request_cancelled_while_the_tool_ran_leaves_no_confirmation_behind():
    broker, store = Broker(limits=BrokerLimits()), ConfirmationStore()
    req = _active(broker)
    runner = ToolCallRunner(broker, store, _asking_executor(store, lambda: broker.cancel(req.id, "user_stop")),
                            cfg=None)
    data, question, _ = _run(runner, req)
    assert data["status"] != "awaiting_confirmation" and question is None
    assert store.get_pending() is None  # a later "yes" cannot approve an unheard question


def test_an_unrelated_confirmation_is_left_alone():
    broker, store = Broker(limits=BrokerLimits()), ConfirmationStore()
    req = _active(broker)
    other = ConfirmationRequest(tool_name="x", tier=SafetyTier.CONFIRM_VOICE, action="a", target="t", parameters={})

    def replace_with_other():
        broker.cancel(req.id, "user_stop")
        store.set_pending(other, {"request_ref": "someone-else"})

    runner = ToolCallRunner(broker, store, _asking_executor(store, replace_with_other), cfg=None)
    _run(runner, req)
    assert store.get_pending().request.id == other.id


SCREEN = ToolImage("image/jpeg", "QUJD")


def _imaging_executor(success=True):
    def executor(*_a, **_k):
        return ToolExecutionResult(success=success, reply_text="screen text", images=(SCREEN,))
    return executor


def test_a_successful_result_hands_its_images_to_the_model():
    broker, store = Broker(limits=BrokerLimits()), ConfirmationStore()
    req = _active(broker)
    runner = ToolCallRunner(broker, store, _imaging_executor(), cfg=None)
    data, question, images = _run(runner, req)
    assert data == {"status": "ok", "text": "screen text"} and question is None
    assert images == (SCREEN,)


def test_a_repeated_call_answered_from_the_stored_result_carries_no_image():
    broker, store = Broker(limits=BrokerLimits()), ConfirmationStore()
    req = _active(broker)
    runner = ToolCallRunner(broker, store, _imaging_executor(), cfg=None)
    _run(runner, req, "c1")
    data, _, images = _run(runner, req, "c2")
    assert data.get("repeated") is True and images == ()


def test_a_failed_result_carries_no_image():
    broker, store = Broker(limits=BrokerLimits()), ConfirmationStore()
    req = _active(broker)
    runner = ToolCallRunner(broker, store, _imaging_executor(success=False), cfg=None)
    data, _, images = _run(runner, req)
    assert data["status"] == "error" and images == ()

"""Confirmation outcome observers and request references (used by the Codex bridge)."""
from __future__ import annotations

import time

import pytest

from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    ConfirmationStore,
    SafetyTier,
    get_confirmation_store,
    set_dialog_callback,
    set_result_handler,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult


def make_request(tier=SafetyTier.CONFIRM_VOICE, target="report.pdf"):
    return ConfirmationRequest(tool_name="t", tier=tier, action="delete", target=target,
                               parameters={"target": target})


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, request_id, outcome):
        self.items.append((request_id, outcome))


@pytest.fixture
def store():
    return ConfirmationStore(default_timeout_sec=30.0)


@pytest.fixture
def events(store):
    ev = Events()
    store.add_observer(ev)
    return ev


@pytest.mark.unit
class TestOutcomes:
    def test_voice_approval_claim_reports_approved(self, store, events):
        req = make_request()
        pending = store.set_pending(req)
        pending.authorised = True
        pending.authorised_at = time.time()
        assert store.claim_approved() is not None
        assert events.items == [(req.id, "approved")]

    def test_voice_denial_reports_denied(self, store, events):
        req = make_request()
        store.set_pending(req)
        store.handle_voice_response("no", language="en")
        assert events.items == [(req.id, "denied")]

    def test_unrelated_speech_reports_cancelled(self, store, events):
        req = make_request()
        store.set_pending(req)
        store.handle_voice_response("what is the weather like today", language="en")
        assert events.items == [(req.id, "cancelled")]

    def test_expiry_seen_on_lookup_reports_expired(self, store, events):
        req = make_request()
        pending = store.set_pending(req, timeout_sec=0.01)
        pending.expires_at = time.time() - 1
        assert store.get_pending() is None
        assert events.items == [(req.id, "expired")]

    def test_replacement_reports_replaced_for_the_old_request(self, store, events):
        first, second = make_request(target="a"), make_request(target="b")
        store.set_pending(first)
        store.set_pending(second)
        assert events.items == [(first.id, "replaced")]

    def test_explicit_clear_reports_cancelled(self, store, events):
        req = make_request()
        store.set_pending(req)
        store.clear_pending()
        assert events.items == [(req.id, "cancelled")]

    def test_dialog_denial_and_expiry(self, store, events):
        class Handle:
            def close(self):
                pass

        answers = {}

        def present(request, resolve):
            answers[request.id] = resolve
            return Handle()

        denied = make_request(SafetyTier.CONFIRM_DIALOG, "x")
        store.begin_dialog(denied, {}, present)
        answers[denied.id](False)
        assert events.items == [(denied.id, "denied")]

        events.items.clear()
        expired = make_request(SafetyTier.CONFIRM_DIALOG, "y")
        store.begin_dialog(expired, {}, present)
        store._pending.expires_at = time.time() - 1
        answers[expired.id](True)
        assert events.items == [(expired.id, "expired")]

    def test_dialog_approval_reports_approved_once(self, store, events):
        class Handle:
            def close(self):
                pass

        captured = {}
        store.schedule = lambda action, db=None, cfg=None: captured.setdefault("action", action)
        req = make_request(SafetyTier.CONFIRM_DIALOG)
        store.begin_dialog(req, {}, lambda r, resolve: captured.setdefault("resolve", resolve) and Handle())
        captured["resolve"](True)
        assert events.items == [(req.id, "approved")]

    def test_consuming_an_approved_action_does_not_report_again(self, store, events):
        req = make_request()
        pending = store.set_pending(req)
        pending.authorised = True
        pending.authorised_at = time.time()
        store.claim_approved()
        store.consume_authorisation("t", "delete", "report.pdf", {"target": "report.pdf"})
        assert events.items == [(req.id, "approved")]

    def test_a_failing_observer_never_breaks_the_store(self, store):
        def boom(request_id, outcome):
            raise RuntimeError("observer failed")

        good = Events()
        store.add_observer(boom)
        store.add_observer(good)
        req = make_request()
        store.set_pending(req)
        store.clear_pending()
        assert good.items == [(req.id, "cancelled")]

    def test_removed_observer_receives_nothing(self, store, events):
        store.remove_observer(events)
        store.set_pending(make_request())
        store.clear_pending()
        assert events.items == []


class RecordingTool(Tool):
    name = "bridgeProbeTool"
    description = "test"
    inputSchema = {"type": "object", "properties": {"target": {"type": "string"}}}

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(
            tool_name=self.name, tier=SafetyTier.CONFIRM_VOICE, action="terminate process",
            target=str((args or {}).get("target", "thing")), parameters=dict(args or {}))

    def run(self, args, context):
        return ToolExecutionResult(success=True, reply_text="Done.")


class Cfg:
    windows_tools_enabled = True
    windows_app_aliases = {}
    voice_debug = False
    wake_word = "jarvis"
    wake_aliases = []
    wake_fuzzy_ratio = 0.78
    tts_rate = 200


@pytest.mark.unit
class TestRequestReference:
    @pytest.fixture(autouse=True)
    def clean(self):
        store = get_confirmation_store()
        store.clear_pending()
        BUILTIN_TOOLS[RecordingTool.name] = RecordingTool()
        set_dialog_callback(None)
        set_result_handler(None)
        yield
        BUILTIN_TOOLS.pop(RecordingTool.name, None)
        store.clear_pending()

    def test_reference_is_stored_with_the_pending_confirmation(self):
        result = run_tool_with_retries(None, Cfg(), RecordingTool.name, {"target": "x"},
                                       "", "", "", request_ref="req-1")
        assert result.success is False and "confirmation" in result.reply_text
        pending = get_confirmation_store().pending_for_ref("req-1")
        assert pending is not None
        assert pending.execution_context["request_ref"] == "req-1"

    def test_other_references_find_nothing(self):
        run_tool_with_retries(None, Cfg(), RecordingTool.name, {"target": "x"}, "", "", "",
                              request_ref="req-1")
        assert get_confirmation_store().pending_for_ref("req-2") is None

    def test_calls_without_a_reference_are_unchanged(self):
        run_tool_with_retries(None, Cfg(), RecordingTool.name, {"target": "x"}, "", "", "")
        assert get_confirmation_store().pending_for_ref("req-1") is None
        assert get_confirmation_store().has_pending_voice()

    def test_origin_follows_quiet_so_existing_delivery_paths_apply(self):
        run_tool_with_retries(None, Cfg(), RecordingTool.name, {"target": "x"}, "", "", "",
                              quiet=True, request_ref="req-1")
        pending = get_confirmation_store().pending_for_ref("req-1")
        assert pending.execution_context["origin"] == "chat"

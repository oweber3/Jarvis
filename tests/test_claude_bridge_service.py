"""Behavioural tests for the background Claude bridge service (fake Claude Code sessions, inert tools)."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from claude_bridge_fakes import NO_RESPONSE, SIGNED_IN, FakeClaude, make_cfg, scripted
from codex_bridge_fakes import FakeExecutor, FakeStore
from jarvis.bridge.tools import ANSWER_SCHEMA
from jarvis.claude_bridge.service import ClaudeBridgeService, failure_text
from jarvis.tools.schema_validation import validate_arguments
from jarvis.tools.types import ToolExecutionResult, ToolImage

TOOLS = {
    "getTime": {"description": "time", "inputSchema": {"type": "object", "properties": {}, "required": []}},
    "systemVolume": {"description": "volume", "inputSchema": {
        "type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
}


def build(script=None, cfg=None, tools=None, executor=None, auth=None, claude_kwargs=None, tmp_path=None,
          **service_kwargs):
    cfg = cfg or make_cfg()
    claude = FakeClaude(script, **(claude_kwargs or {}))
    store = FakeStore()
    executor = executor or FakeExecutor()
    auth_calls = []

    def auth_reader():
        auth_calls.append(1)
        if isinstance(auth, Exception):
            raise auth
        return dict(auth or SIGNED_IN)

    service = ClaudeBridgeService(cfg, claude, executor=executor, confirmation_store=store,
                                  tools_provider=lambda c: dict(tools or TOOLS), auth_reader=auth_reader,
                                  instructions_provider=lambda: "CONTRACT", **service_kwargs)
    service.auth_calls = auth_calls
    return service, claude, executor, store


def run(service, text="open word", **kw):
    return service.run_request(text, context=kw.pop("context", []), origin=kw.pop("origin", "voice"),
                               language="en", db=None, quiet=kw.pop("quiet", False),
                               desktop=kw.pop("desktop", None))


def run_in_background(service, **kw):
    holder = {}
    worker = threading.Thread(target=lambda: holder.setdefault("out", run(service, **kw)), daemon=True)
    worker.start()
    return worker, holder


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def closing():
    built = []
    yield built


@pytest.mark.unit
class TestRoundTrip:
    def test_reply_is_delivered_and_tools_run_on_the_query_thread(self):
        service, claude, executor, _ = build(scripted(
            ("exec", "getTime", {}), ("answer", "completed", "It is three o'clock.")))
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.text) == ("reply", "It is three o'clock.")
            assert executor.calls[0][0] == "getTime"
            assert executor.threads == [threading.get_ident()]
            model = claude.used()[0].turns[0]
            assert model.results[0] == {"is_error": False, "data": {"status": "ok", "text": "getTime ok"}}
        finally:
            service.close()

    def test_a_tool_image_reaches_the_model_as_an_image_block_beside_the_text(self):
        executor = FakeExecutor()
        executor.next = ToolExecutionResult(True, "screen text", images=(ToolImage("image/jpeg", "QUJD"),))
        service, claude, _, _ = build(scripted(
            ("exec", "getTime", {}), ("answer", "completed", "ok")), executor=executor)
        try:
            run(service)
            result = claude.used()[0].turns[0].results[0]
            assert result["data"] == {"status": "ok", "text": "screen text"}
            assert result["images"] == [("image/jpeg", "QUJD")]
        finally:
            service.close()

    def test_each_request_runs_in_its_own_isolated_session_that_is_closed_afterwards(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service)
            run(service)
            first, second = claude.used()
            assert first is not second and first.session_id != second.session_id
            assert first.closes >= 1 and not first.alive
            assert len(first.user_texts) == 1 and len(second.user_texts) == 1
            assert first.arg("--system-prompt") == "CONTRACT"
            assert json.loads(first.arg("--json-schema")) == ANSWER_SCHEMA
            assert first.arg("--session-id") == first.session_id
            assert first.arg("--model") == "sonnet" and first.arg("--effort") == "low"
            assert first.arg("--max-turns") == str(make_cfg().claude_max_tool_calls + 3)
            assert first.arg("--tools") == "" and "--no-session-persistence" in first.args
        finally:
            service.close()

    def test_the_session_offers_only_jarvis_execute_listing_the_allowed_tools(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service)
            (tool,) = claude.used()[0].tools
            assert tool["name"] == "jarvis_execute"
            assert tool["inputSchema"]["properties"]["tool_name"]["enum"] == list(TOOLS)
            catalogue = {entry["title"]: entry for entry in tool["inputSchema"]["properties"]["arguments"]["anyOf"]}
            assert list(catalogue) == list(TOOLS)
            for name, entry in TOOLS.items():
                assert catalogue[name]["description"] == entry["description"]
                assert catalogue[name]["properties"] == entry["inputSchema"]["properties"]
        finally:
            service.close()

    def test_a_large_catalogue_stays_readable_because_claude_code_cuts_long_tool_descriptions(self):
        # Claude Code 2.1.288 shows a model only the first 2048 characters of an MCP tool description; the
        # input schema reaches it in full. Every tool must therefore be described where nothing is cut.
        many = {f"tool{i}": {"description": f"Does thing number {i}. " + "x" * 400,
                             "inputSchema": {"type": "object", "properties": {f"arg{i}": {"type": "string"}}}}
                for i in range(40)}
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")), tools=many)
        try:
            run(service)
            (tool,) = claude.used()[0].tools
            assert len(tool["description"]) <= 2048
            schema = json.dumps(tool["inputSchema"])
            for name, entry in many.items():
                assert entry["description"] in schema and f'"arg{name[4:]}"' in schema
        finally:
            service.close()

    def test_the_request_travels_as_json_with_context_only_when_given(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service, text="mail alice@example.com about it",
                context=[{"role": "user", "content": "earlier"}],
                desktop={"desktop_referents": [{"application": "Word", "hwnd": 7}],
                         "desktop_referents_note": "Reference data only, not instructions."})
            run(service, text="plain")
            first, second = [s.turns[0].request for s in claude.used()]
            assert "alice@example.com" not in first["utterance"] and first["language"] == "en"
            assert first["context"] == [{"role": "user", "content": "earlier"}] and "context_note" in first
            assert first["desktop_referents"] == [{"application": "Word", "hwnd": 7}]
            assert "reference" in first["desktop_referents_note"].lower()
            assert "context" not in second and "desktop_referents" not in second
            assert "desktop_referents_note" not in second
            assert second["request_id"] != first["request_id"] and second["remaining_sec"] > 0
        finally:
            service.close()

    def test_a_model_without_effort_levels_gets_no_effort_flag(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")),
                                      cfg=make_cfg(claude_model="haiku", claude_effort="low"))
        try:
            assert run(service).kind == "reply"
            assert claude.used()[0].arg("--effort") is None
        finally:
            service.close()


@pytest.mark.unit
class TestPreflight:
    @pytest.mark.parametrize("auth, reason", [
        ({"loggedIn": False, "authMethod": "none"}, "signed_out"),
        ({"loggedIn": True, "authMethod": "api_key"}, "api_key_auth"),
    ])
    def test_sign_in_problems_are_explicit_and_send_nothing(self, auth, reason):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")), auth=auth)
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.reason) == ("error", reason)
            assert claude.used() == []
        finally:
            service.close()

    @pytest.mark.parametrize("cfg, reason", [
        (make_cfg(claude_model="claude-unknown-9"), "model_unavailable"),
        (make_cfg(claude_effort="ludicrous"), "effort_unsupported"),
    ])
    def test_model_and_effort_must_be_listed_by_the_cli(self, cfg, reason):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")), cfg=cfg)
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.reason) == ("error", reason)
            assert claude.used() == [] and all(not s.alive for s in claude.sessions)
        finally:
            service.close()

    def test_missing_cli_is_reported(self):
        from jarvis.claude_bridge.cli import ClaudeCliError
        service, _, _, _ = build(auth=ClaudeCliError("not_found"))
        try:
            assert run(service).reason == "not_found"
        finally:
            service.close()

    def test_a_session_that_never_initialises_fails_explicitly(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")), claude_kwargs={"no_init": True},
                                      start_timeout_sec=0.5)
        try:
            outcome = run(service)
            assert outcome.kind == "error" and outcome.reason in ("start_failed", "timeout")
            assert claude.used() == []
        finally:
            service.close()

    def test_preflight_runs_once_until_something_fails(self):
        service, _, _, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service)
            run(service)
            assert len(service.auth_calls) == 1
        finally:
            service.close()


@pytest.mark.unit
class TestOwnership:
    def test_a_call_for_another_request_gets_nothing(self):
        def script(model):
            model.call({"request_id": "someone-else", "tool_name": "getTime", "arguments": {}})
            model.answer("completed", "done")

        service, claude, executor, _ = build(script)
        try:
            run(service)
            assert claude.used()[0].turns[0].results[0]["data"] == {"status": "refused", "reason": "wrong_request"}
            assert executor.calls == []
        finally:
            service.close()

    def test_an_unknown_mcp_tool_is_refused(self):
        def script(model):
            model.call({"request_id": model.request_id, "tool_name": "getTime", "arguments": {}}, name="shell")
            model.answer("completed", "done")

        service, claude, executor, _ = build(script)
        try:
            run(service)
            assert claude.used()[0].turns[0].results[0]["data"]["reason"] == "unknown_tool"
            assert executor.calls == []
        finally:
            service.close()

    def test_out_of_snapshot_and_invalid_arguments_have_no_side_effect(self):
        def script(model):
            model.execute("deleteEverything", {})
            model.execute("systemVolume", {})
            model.answer("completed", "done")

        service, claude, executor, _ = build(script)
        try:
            run(service)
            reasons = [r["data"]["reason"] for r in claude.used()[0].turns[0].results]
            assert reasons == ["tool_not_allowed", "invalid_arguments"] and executor.calls == []
        finally:
            service.close()

    def test_invalid_arguments_carry_the_validation_error(self):
        def script(model):
            model.execute("systemVolume", {"volume": 30})
            model.answer("completed", "done")

        service, claude, executor, _ = build(script)
        try:
            run(service)
            data = claude.used()[0].turns[0].results[0]["data"]
            expected = validate_arguments(TOOLS["systemVolume"]["inputSchema"], {"volume": 30})
            assert data == {"status": "refused", "reason": "invalid_arguments", "error": expected}
            assert executor.calls == []
        finally:
            service.close()

    def test_repeated_calls_return_the_stored_result_and_execute_once(self):
        def script(model):
            model.execute("getTime", {}, msg_id=5)
            model.execute("getTime", {}, msg_id=5)
            model.execute("getTime", {})
            model.answer("completed", "done")

        service, claude, executor, _ = build(script)
        try:
            run(service)
            results = claude.used()[0].turns[0].results
            assert [r["data"].get("repeated") for r in results] == [None, True, True]
            assert len(executor.calls) == 1
        finally:
            service.close()

    def test_tool_calls_are_capped(self):
        def script(model):
            model.execute("getTime", {})
            model.execute("systemVolume", {"action": "get"})
            model.answer("completed", "done")

        service, claude, executor, _ = build(script, cfg=make_cfg(claude_max_tool_calls=1))
        try:
            run(service)
            assert claude.used()[0].turns[0].results[1]["data"]["reason"] == "too_many_calls"
            assert len(executor.calls) == 1
        finally:
            service.close()

    def test_permission_prompts_and_other_mcp_methods_are_declined(self):
        def script(model):
            model.permission = model.session.host_request(
                {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "dir"}})
            model.other = model.session.mcp("resources/list", {}, 77)
            model.unknown = model.session.host_request({"subtype": "elicitation", "message": "?"})
            model.answer("completed", "done")

        service, claude, executor, _ = build(script)
        try:
            run(service)
            model = claude.used()[0].turns[0]
            assert model.permission["response"]["behavior"] == "deny"
            assert model.other["error"]["code"] == -32601
            assert model.unknown["subtype"] == "error"
            assert executor.calls == []
        finally:
            service.close()


@pytest.mark.unit
class TestDelivery:
    def test_needs_user_input_is_a_question(self):
        service, _, _, _ = build(scripted(("answer", "needs_user_input", "Which monitor?")))
        try:
            assert (run(service).kind, run(service).text) == ("question", "Which monitor?")
        finally:
            service.close()

    def test_a_reported_failure_is_delivered_honestly(self):
        service, _, _, _ = build(scripted(("answer", "failed", "I could not open Word.")))
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.text) == ("reply", "I could not open Word.")
        finally:
            service.close()

    def test_prose_without_the_answer_object_is_no_answer(self):
        service, _, _, _ = build(scripted(("result", {"text": "Sure, here you go"})))
        try:
            assert run(service).reason == "no_answer"
        finally:
            service.close()

    @pytest.mark.parametrize("result, reason", [
        ({"subtype": "error_during_execution", "is_error": True, "api_status": 429}, "usage_limit"),
        ({"subtype": "error_during_execution", "is_error": True, "api_status": 401}, "signed_out"),
        ({"subtype": "error_during_execution", "is_error": True, "api_status": 529}, "service_unavailable"),
        ({"subtype": "error_max_turns", "is_error": True, "terminal": "max_turns"}, "max_turns"),
        ({"subtype": "error_during_execution", "is_error": True}, "turn_failed"),
    ])
    def test_failed_turns_never_deliver_a_success(self, result, reason):
        def script(model):
            model.result(structured={"status": "completed", "reply": "All done!"}, **result)

        service, _, _, _ = build(script)
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.reason) == ("error", reason)
            assert "All done" not in outcome.text
        finally:
            service.close()

    def test_process_death_after_a_side_effect_neither_replays_nor_claims_success(self):
        turns = []

        def script(model):
            turns.append(model)
            model.execute("getTime", {})
            if len(turns) == 1:
                model.session.die()
            else:
                model.answer("completed", "ok")

        service, claude, executor, _ = build(script)
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.reason) == ("error", "process_exited")
            assert len(executor.calls) == 1
            run_again = run(service)
            assert len(executor.calls) == 2 and run_again.kind == "reply"
        finally:
            service.close()

    def test_the_reply_is_redacted_and_bounded(self):
        service, _, _, _ = build(scripted(("answer", "completed", "Mail alice@example.com " + "x" * 3000)))
        try:
            text = run(service).text
            assert "alice@example.com" not in text and len(text) < 2100
        finally:
            service.close()


@pytest.mark.unit
class TestConfirmation:
    def _confirming_run(self, outcome_after=None):
        executor = FakeExecutor()

        def script(model):
            model.execute("systemVolume", {"action": "mute"})
            model.later = model.execute("getTime", {}, timeout=0.5)
            model.answer("completed", "The user approved.")

        service, claude, _, store = build(script, executor=executor)
        pending = SimpleNamespace(request=SimpleNamespace(id="conf-1"))

        def exec_needing_confirmation(*args, **kwargs):
            store.pending[kwargs.get("request_ref")] = pending
            executor.calls.append(args[2])
            return ToolExecutionResult(False, "I need your confirmation to mute. Say yes or no.")

        service._executor = exec_needing_confirmation
        out = run(service)
        return service, claude, store, out, executor.calls

    def test_confirmation_is_returned_immediately_and_the_session_is_stopped(self):
        service, claude, _, out, calls = self._confirming_run()
        try:
            assert out.kind == "awaiting_confirmation" and "confirmation" in out.text
            session = claude.used()[0]
            assert wait_for(lambda: session.turns[0].results)
            assert session.turns[0].results[0]["data"]["status"] == "awaiting_confirmation"
            assert "interrupt" in session.controls and not session.alive
            assert calls == ["systemVolume"]
        finally:
            service.close()

    @pytest.mark.parametrize("event, state", [("approved", "completed"), ("denied", "cancelled"),
                                              ("expired", "cancelled")])
    def test_the_users_answer_settles_the_request(self, event, state):
        service, _, store, out, _ = self._confirming_run()
        try:
            rid = next(iter(store.pending))
            store.emit("conf-1", event)
            assert service.broker.state_of(rid).value == state
        finally:
            service.close()


@pytest.mark.unit
class TestCancellationAndBounds:
    def test_cancel_stops_the_session_and_delivers_nothing(self):
        release = threading.Event()

        def script(model):
            release.wait(5)

        service, claude, _, _ = build(script)
        try:
            worker, holder = run_in_background(service)
            assert wait_for(lambda: claude.used() and service.is_busy())
            assert service.cancel_active("stop") is True
            worker.join(5)
            assert holder["out"].kind == "cancelled"
            session = claude.used()[0]
            assert wait_for(lambda: not session.alive)
        finally:
            release.set()
            service.close()

    def test_the_deadline_expires_the_request(self):
        release = threading.Event()
        service, claude, _, _ = build(lambda m: release.wait(5), cfg=make_cfg(claude_timeout_sec=0.5))
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.reason) == ("error", "timeout")
            assert not claude.used()[0].alive
        finally:
            release.set()
            service.close()

    def test_a_request_beyond_the_queue_is_refused_as_busy(self):
        release = threading.Event()
        service, claude, _, _ = build(lambda m: release.wait(5), cfg=make_cfg(claude_queue_limit=0))
        try:
            worker, _ = run_in_background(service)
            assert wait_for(lambda: service.is_busy())
            assert run(service).reason == "bridge_busy"
        finally:
            release.set()
            worker.join(5)
            service.close()

    def test_close_cancels_and_stops_every_owned_session(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        run(service)
        assert wait_for(lambda: len(claude.sessions) >= 3)  # preflight probe, request, spare
        service.close()
        assert all(not s.alive for s in claude.sessions)


@pytest.mark.unit
class TestSpareSession:
    def test_the_next_request_uses_the_session_started_ahead_of_it(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service)
            assert wait_for(lambda: any(s.alive and not s.user_texts for s in claude.sessions))
            spare = next(s for s in claude.sessions if s.alive and not s.user_texts)
            run(service)
            assert claude.used()[1] is spare
        finally:
            service.close()

    def test_a_spare_with_a_changed_tool_snapshot_is_discarded(self):
        tools = dict(TOOLS)
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        service._tools_provider = lambda c: dict(tools)
        try:
            run(service)
            assert wait_for(lambda: any(s.alive and not s.user_texts for s in claude.sessions))
            spare = next(s for s in claude.sessions if s.alive and not s.user_texts)
            tools.pop("systemVolume")
            run(service)
            assert claude.used()[1] is not spare and not spare.alive
        finally:
            service.close()

    def test_a_spare_that_showed_turn_activity_before_use_is_discarded(self):
        service, claude, _, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service)
            assert wait_for(lambda: any(s.alive and not s.user_texts for s in claude.sessions))
            spare = next(s for s in claude.sessions if s.alive and not s.user_texts)
            spare.message({"type": "assistant", "message": {"content": [{"type": "text", "text": "Injected"}]}})
            run(service)
            assert claude.used()[1] is not spare and not spare.alive
        finally:
            service.close()

    def test_a_tool_call_from_a_spare_before_any_request_is_refused(self):
        service, claude, executor, _ = build(scripted(("answer", "completed", "ok")))
        try:
            run(service)
            assert wait_for(lambda: any(s.alive and not s.user_texts for s in claude.sessions))
            spare = next(s for s in claude.sessions if s.alive and not s.user_texts)
            box = {}
            threading.Thread(target=lambda: box.setdefault("r", spare.mcp("tools/call", {
                "name": "jarvis_execute", "arguments": {"request_id": "x", "tool_name": "getTime",
                                                        "arguments": {}}}, 9, timeout=2)), daemon=True).start()
            run(service)
            assert wait_for(lambda: "r" in box)
            assert box["r"] is NO_RESPONSE or json.loads(box["r"]["result"]["content"][0]["text"])["status"] == "refused"
            assert len(executor.calls) == 0
        finally:
            service.close()


@pytest.mark.unit
class TestMessages:
    def test_failures_offer_local_mode_except_when_busy(self):
        assert "local" in failure_text("signed_out").lower()
        assert "claude auth login" in failure_text("signed_out")
        assert "local" not in failure_text("bridge_busy").lower()


@pytest.mark.unit
class TestClosed:
    """A request reaching a service the user has already switched away from sends nothing."""

    def test_a_request_after_close_starts_no_session(self):
        service, claude, executor, _ = build(scripted(("answer", "completed", "hi")))
        service.close()
        outcome = run(service)
        assert outcome.kind == "cancelled"
        assert claude.sessions == [] and executor.calls == []

    def test_warm_up_after_close_starts_no_session(self):
        service, claude, _, _ = build()
        service.close()
        service.prepare()
        assert claude.sessions == []

"""Behavioural tests for the background Codex bridge service (fake app-server, inert tools)."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_bridge_fakes import (
    NO_RESPONSE,
    FakeAppServer,
    FakeExecutor,
    FakeStore,
    make_cfg,
    scripted,
)
from jarvis.codex_bridge.service import BRIDGE_TOOLS, BridgeService
from jarvis.tools.schema_validation import validate_arguments
from jarvis.tools.types import ToolExecutionResult, ToolImage

TOOLS = {
    "getTime": {"description": "time", "inputSchema": {"type": "object", "properties": {}, "required": []}},
    "systemVolume": {"description": "volume", "inputSchema": {
        "type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
}


def build(script=None, cfg=None, tools=None, executor=None, server_kwargs=None, tmp_path=None, **service_kwargs):
    cfg = cfg or make_cfg()
    server = FakeAppServer(script, **(server_kwargs or {}))
    store = FakeStore()
    executor = executor or FakeExecutor()
    service = BridgeService(cfg, server, executor=executor, confirmation_store=store,
                            tools_provider=lambda c: dict(tools or TOOLS),
                            runtime_dir=tmp_path or Path("."), **service_kwargs)
    return service, server, executor, store


def run(service, text="open word", **kw):
    return service.run_request(text, context=kw.pop("context", []), origin=kw.pop("origin", "voice"),
                               language="en", db=None, quiet=kw.pop("quiet", False))


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


@pytest.mark.unit
class TestRoundTrip:
    def test_reply_is_delivered_and_tools_run_on_the_query_thread(self):
        service, server, executor, _ = build(scripted(
            ("exec", "getTime", {}), ("answer", "completed", "It is three o'clock.")))
        outcome = run(service)
        assert (outcome.kind, outcome.text) == ("reply", "It is three o'clock.")
        assert executor.calls[0][0] == "getTime"
        assert executor.threads == [threading.get_ident()]
        assert server.turns[0].results[0] == {"success": True, "data": {"status": "ok", "text": "getTime ok"}}

    def test_each_request_gets_its_own_ephemeral_isolated_session(self, tmp_path):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")), tmp_path=tmp_path,
                                      instructions_provider=lambda: "CONTRACT")
        run(service)
        started = server.params_of("thread/start")[0]
        assert started["ephemeral"] is True and Path(started["cwd"]) == tmp_path
        assert started["model"] == "gpt-6-luna" and started["allowProviderModelFallback"] is False
        assert (started["sandbox"], started["approvalPolicy"], started["environments"]) == ("read-only", "never", [])
        assert started["baseInstructions"] == "CONTRACT"
        assert {t["name"] for t in started["dynamicTools"]} == set(BRIDGE_TOOLS)
        assert started["config"] == {"mcp_servers.personal.enabled": False, "plugins.p@m.enabled": False}
        assert server.params_of("turn/start")[0]["effort"] == "low"
        assert server.params_of("thread/unsubscribe")[0]["threadId"] == server.turns[0].thread_id

    def test_a_codex_config_change_between_requests_is_followed(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        assert run(service).text == "ok"
        server.effective_config = {"mcp_servers": {"other": {}}}
        assert run(service).text == "ok"
        second_thread = server.params_of("turn/start")[1]["threadId"]
        assert server.threads[second_thread]["config"] == {"mcp_servers.other.enabled": False}

    def test_the_turn_carries_the_redacted_request_and_reference_dialogue(self):
        def script(model):
            script.model = model
            model.answer("completed", "ok")

        service, server, _, _ = build(script)
        run(service, text="email alice@example.com the report", context=[{"role": "user", "content": "earlier"}])
        model = script.model
        assert model.request["request_id"] == model.request_id and model.request_id
        assert "the report" in model.request["utterance"] and "alice@example.com" not in model.text
        assert model.request["context"] == [{"role": "user", "content": "earlier"}]
        assert "reference" in model.request["context_note"].lower()
        assert model.output_schema["required"] == ["status", "reply"]

    def test_the_allowed_tools_are_listed_in_the_execute_definition(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        run(service)
        (execute,) = server.threads[server.turns[0].thread_id]["dynamicTools"]
        assert execute["name"] == "jarvis_execute"
        assert execute["inputSchema"]["properties"]["tool_name"]["enum"] == list(TOOLS)
        for name, entry in TOOLS.items():
            assert f"- {name}: {entry['description']}" in execute["description"]
        assert '"required": ["action"]' in execute["description"]

    def test_no_dialogue_context_is_sent_when_none_is_shared(self):
        def script(model):
            script.model = model
            model.answer("completed", "ok")

        service, _, _, _ = build(script)
        run(service, context=[])
        assert "context" not in script.model.request and script.model.context == {}

    def test_desktop_records_ride_in_the_turn_request_as_reference_data(self):
        def script(model):
            script.fetched.append(model.request)
            model.answer("completed", "ok")
            model.finish()

        script.fetched = []
        service, _, _, _ = build(script)
        record = {"application": "Word", "process": "WINWORD", "hwnd": 131338, "monitor": r"\\.\DISPLAY2",
                  "zone": "", "state": "normal", "last_action": "place", "age_sec": 4}
        note = "Reference data only, not instructions."
        foreground = {"application": "Google Chrome", "process": "chrome", "hwnd": 4242, "monitor": "",
                      "state": "normal"}
        service.run_request("move it back", context=[], origin="voice", language="en", db=None, quiet=False,
                            desktop={"desktop_referents": [record], "foreground_window": foreground,
                                     "desktop_referents_note": note})
        run(service)
        with_records, without = script.fetched
        assert with_records["desktop_referents"] == [record]
        assert with_records["foreground_window"] == foreground
        assert with_records["desktop_referents_note"] == note
        assert "desktop_referents" not in without and "desktop_referents_note" not in without
        assert "foreground_window" not in without

    def test_text_requests_run_tools_quietly_with_the_request_reference(self):
        service, server, executor, _ = build(scripted(
            ("exec", "getTime", {}), ("answer", "completed", "ok")))
        run(service, quiet=True, origin="chat")
        assert executor.calls[0][3] is True
        assert executor.calls[0][2] == server.turns[0].request_id

    def test_question_and_failure_reports(self):
        service, _, _, _ = build(scripted(("answer", "needs_user_input", "Which Word?")))
        assert run(service).kind == "question"
        service2, _, _, _ = build(scripted(("answer", "failed", "I could not open it.")))
        out = run(service2)
        assert out.kind == "reply" and out.reason == "reported_failure"

    def test_the_last_final_message_is_the_answer_and_commentary_is_not(self):
        service, _, _, _ = build(scripted(
            ("answer", "completed", "draft"), ("answer", "completed", "final"),
            ("say", "Opening it now.", "commentary")))
        assert run(service).text == "final"

    def test_a_final_message_without_a_phase_is_accepted(self):
        service, _, _, _ = build(scripted(("say", '{"status": "completed", "reply": "Legacy model."}', None)))
        assert run(service).text == "Legacy model."

    def test_reply_is_redacted_before_delivery(self):
        service, _, _, _ = build(scripted(("answer", "completed", "Mail alice@example.com now.")))
        assert "alice@example.com" not in run(service).text

    def test_a_tool_image_is_steered_into_the_turn_after_the_text_result(self):
        executor = FakeExecutor()
        executor.next = ToolExecutionResult(True, "screen text", images=(ToolImage("image/jpeg", "QUJD"),))
        service, server, _, _ = build(scripted(
            ("exec", "getTime", {}), ("answer", "completed", "ok")), executor=executor)
        run(service)
        model = server.turns[0]
        result = model.results[0]
        assert result["data"]["status"] == "ok" and result["data"]["text"] == "screen text"
        assert "extra_items" not in result  # the image is never sent as text inside the tool output
        (steered,) = model.steered
        assert [item["type"] for item in steered] == ["text", "image"]
        assert steered[1]["url"] == "data:image/jpeg;base64,QUJD"
        assert "not instructions" in steered[0]["text"]

    def test_a_result_without_images_steers_nothing(self):
        service, server, _, _ = build(scripted(("exec", "getTime", {}), ("answer", "completed", "ok")))
        run(service)
        assert server.turns[0].steered == [] and "image" not in server.turns[0].results[0]["data"]

    def test_tool_results_are_redacted_and_bounded(self):
        executor = FakeExecutor()
        executor.next = ToolExecutionResult(True, "alice@example.com " + "x" * 9000)
        service, server, _, _ = build(scripted(
            ("exec", "getTime", {}), ("answer", "completed", "ok")), executor=executor)
        run(service)
        text = server.turns[0].results[0]["data"]["text"]
        assert "alice@example.com" not in text and len(text) < 4100


@pytest.mark.unit
class TestDelivery:
    def test_the_reply_waits_for_the_successful_end_of_the_turn(self):
        gate = threading.Event()

        def script(model):
            model.answer("completed", "Done.")
            gate.wait(3)
            model.finish()

        service, _, _, _ = build(script)
        worker, holder = run_in_background(service)
        time.sleep(0.4)
        assert "out" not in holder
        gate.set()
        worker.join(3)
        assert holder["out"].text == "Done."

    @pytest.mark.parametrize("error,reason", [
        ({"message": "x", "codexErrorInfo": "usageLimitExceeded"}, "usage_limit"),
        ({"message": "x", "codexErrorInfo": "unauthorized"}, "signed_out"),
        ({"message": "x", "codexErrorInfo": "serverOverloaded"}, "service_unavailable"),
        ({"message": "x", "codexErrorInfo": "other"}, "turn_failed"),
        (None, "turn_failed"),
    ])
    def test_a_failed_turn_after_a_proposed_success_is_not_a_success(self, error, reason):
        service, _, _, _ = build(scripted(
            ("answer", "completed", "All done!"), ("finish", "failed", error)))
        out = run(service)
        assert out.kind == "error" and out.reason == reason
        assert "All done" not in out.text and "local mode" in out.text.lower()

    @pytest.mark.parametrize("steps", [
        (),
        (("say", "Sure, all done!"),),
        (("say", '{"status": "completed", "reply": "   "}'),),
        (("say", '{"status": "done", "reply": "ok"}'),),
        (("say", '["completed", "ok"]'),),
    ])
    def test_a_turn_without_a_valid_answer_object_is_reported(self, steps):
        service, _, _, _ = build(scripted(*steps))
        out = run(service)
        assert out.kind == "error" and out.reason == "no_answer"

    def test_messages_from_another_turn_are_not_the_answer(self):
        def script(model):
            model.server.notify("item/completed", {"threadId": model.thread_id, "turnId": "turn-other", "item": {
                "type": "agentMessage", "id": "m", "text": '{"status": "completed", "reply": "foreign"}',
                "phase": "final_answer"}})
            model.finish()

        service, _, _, _ = build(script)
        assert run(service).reason == "no_answer"


@pytest.mark.unit
class TestOwnershipAndSafety:
    def test_calls_from_another_thread_or_turn_get_nothing(self):
        def script(model):
            script.foreign = [model.execute("getTime", {}, thread="thread-other"),
                              model.execute("getTime", {}, turn="turn-other")]
            model.answer("completed", "ok")
            model.finish()

        service, _, executor, _ = build(script)
        assert run(service).text == "ok"
        for result in script.foreign:
            assert result["success"] is False and "text" not in result["data"]
        assert [r["data"]["reason"] for r in script.foreign] == ["wrong_thread", "wrong_turn"]
        assert executor.calls == []

    def test_a_request_id_that_is_not_the_active_one_is_refused(self):
        def script(model):
            script.other = model.call("jarvis_execute", {"request_id": "someone-else", "tool_name": "getTime",
                                                         "arguments": {}})
            model.answer("completed", "ok")
            model.finish()

        service, _, _, _ = build(script)
        run(service)
        assert script.other["success"] is False and script.other["data"]["reason"] == "wrong_request"

    def test_unknown_and_malformed_calls_have_no_side_effect(self):
        def script(model):
            script.out = [model.execute("shutdown", {}), model.execute("systemVolume", {}),
                          model.call("open_shell", {"cmd": "x"}), model.call("jarvis_execute", "not an object")]
            model.answer("completed", "done")
            model.finish()

        service, _, executor, _ = build(script)
        run(service)
        assert [r["success"] for r in script.out] == [False] * 4
        assert [r["data"].get("reason") for r in script.out] == [
            "tool_not_allowed", "invalid_arguments", "unknown_tool", "invalid_arguments"]
        assert executor.calls == []

    def test_invalid_arguments_carry_the_validation_error(self):
        def script(model):
            script.out = [model.execute("systemVolume", {}), model.call("jarvis_execute", "not an object")]
            model.answer("completed", "done")
            model.finish()

        service, _, executor, _ = build(script)
        run(service)
        schema_error = validate_arguments(TOOLS["systemVolume"]["inputSchema"], {})
        assert [r["data"] for r in script.out] == [
            {"status": "refused", "reason": "invalid_arguments", "error": schema_error},
            {"status": "refused", "reason": "invalid_arguments", "error": validate_arguments(None, "not an object")},
        ]
        assert executor.calls == []

    def test_other_server_requests_are_declined(self):
        def script(model):
            script.approval = model.server.server_request("item/commandExecution/requestApproval", {
                "threadId": model.thread_id, "turnId": model.turn_id})
            script.ask = model.server.server_request("item/tool/requestUserInput", {})
            model.answer("completed", "ok")
            model.finish()

        service, _, _, _ = build(script)
        run(service)
        assert "error" in script.approval and "error" in script.ask

    def test_identical_action_runs_once_and_a_repeated_call_id_is_answered_from_storage(self):
        def script(model):
            script.out = [model.execute("systemVolume", {"action": "mute"}, call_id="a"),
                          model.execute("systemVolume", {"action": "mute"}, call_id="b"),
                          model.execute("systemVolume", {"action": "mute"}, call_id="a")]
            model.answer("completed", "ok")
            model.finish()

        service, _, executor, _ = build(script)
        run(service)
        assert len(executor.calls) == 1
        assert [r["data"]["status"] for r in script.out] == ["ok", "ok", "ok"]
        assert script.out[1]["data"].get("repeated") is True

    def test_executor_exception_is_uncertain_and_never_replayed(self):
        executor = FakeExecutor()
        executor.raise_next = True

        def script(model):
            script.out = [model.execute("systemVolume", {"action": "mute"})]
            executor.raise_next = False
            script.out.append(model.execute("systemVolume", {"action": "mute"}))
            model.answer("completed", "ok")
            model.finish()

        service, _, _, _ = build(script, executor=executor)
        run(service)
        assert [r["data"]["status"] for r in script.out] == ["uncertain", "uncertain"]
        assert len(executor.calls) == 1


@pytest.mark.unit
class TestProcessFailure:
    def test_process_death_after_a_side_effect_never_replays_or_claims_success(self):
        def script(model):
            model.execute("systemVolume", {"action": "mute"})
            model.server.die()

        service, server, executor, _ = build(script)
        out = run(service)
        assert out.kind == "error" and out.reason == "process_exited"
        assert "not repeated" in out.text
        assert len(executor.calls) == 1
        server.script = scripted(("answer", "completed", "fresh"))
        second = run(service, text="what time is it")
        assert second.text == "fresh"
        assert server.starts == 2 and len(server.params_of("turn/start")) == 2
        assert server.turns[1].request_id != server.turns[0].request_id
        assert len(executor.calls) == 1

    def test_a_restarted_process_is_preflighted_again(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        run(service)
        server.close()
        run(service)
        assert server.methods().count("account/read") == 2

    @pytest.mark.parametrize("kwargs,reason", [
        ({"start_error": "not_found"}, "not_found"),
        ({"start_error": "start_failed"}, "start_failed"),
        ({"start_error": "timeout"}, "start_failed"),
        ({"account": None}, "signed_out"),
        ({"account": "apiKey"}, "api_key_auth"),
        ({"models": []}, "model_unavailable"),
        ({"fail": {"model/list": "unknown method"}}, "unsupported"),
        ({"fail": {"thread/start": "dynamicTools requires experimentalApi capability"}}, "unsupported"),
        ({"fail": {"turn/start": "boom"}}, "session_failed"),
    ])
    def test_preflight_and_session_failures_are_explicit_and_start_no_turn(self, kwargs, reason):
        service, server, executor, _ = build(scripted(("answer", "completed", "ok")),
                                             server_kwargs=kwargs)
        out = run(service)
        assert out.kind == "error" and out.reason == reason
        assert out.text and "local mode" in out.text.lower()
        assert server.turns == [] and executor.calls == []

    def test_an_unsupported_effort_is_never_substituted(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")),
                                      cfg=make_cfg(codex_reasoning_effort="ultra"))
        out = run(service)
        assert out.reason == "effort_unsupported" and server.params_of("thread/start") == []

    def test_a_session_failure_closes_the_thread_it_created(self):
        service, server, _, _ = build(scripted(), server_kwargs={"fail": {"turn/start": "boom"}})
        run(service)
        assert len(server.params_of("thread/unsubscribe")) == 1


@pytest.mark.unit
class TestCancellationAndDeadline:
    def test_cancel_stops_the_request_interrupts_the_turn_and_refuses_late_calls(self):
        gate = threading.Event()

        def script(model):
            gate.wait(3)
            script.late = model.execute("getTime", {}, timeout=0.5)

        service, server, executor, _ = build(script)
        worker, holder = run_in_background(service)
        assert wait_for(lambda: server.turns and service.broker.state_of(server.turns[0].request_id).value == "active")
        assert service.cancel_active("stop") is True
        worker.join(3)
        assert holder["out"].kind == "cancelled"
        assert wait_for(lambda: server.params_of("turn/interrupt"))
        gate.set()
        assert wait_for(lambda: hasattr(script, "late"))
        assert script.late is NO_RESPONSE or script.late["success"] is False
        assert executor.calls == []

    def test_cancel_with_nothing_active_is_a_noop(self):
        service, server, _, _ = build()
        assert service.cancel_active("stop") is False
        assert server.params_of("turn/interrupt") == []

    def test_deadline_gives_an_error_and_interrupts(self):
        service, server, executor, _ = build(lambda model: None, cfg=make_cfg(codex_timeout_sec=0.5))
        out = run(service)
        assert out.kind == "error" and out.reason == "timeout"
        assert server.params_of("turn/interrupt")
        assert executor.calls == []

    def test_stale_calls_from_an_abandoned_turn_are_refused_by_the_next_request(self):
        release = threading.Event()

        def first(model):
            release.wait(3)
            first.stale = model.execute("getTime", {}, timeout=3)

        service, server, executor, _ = build(first, cfg=make_cfg(codex_timeout_sec=0.6))
        assert run(service).reason == "timeout"
        release.set()
        time.sleep(0.2)
        server.script = scripted(("answer", "completed", "second"))
        assert run(service, text="next").text == "second"
        assert wait_for(lambda: hasattr(first, "stale"))
        assert first.stale is not NO_RESPONSE and first.stale["success"] is False
        assert executor.calls == []

    def test_busy_bridge_refuses_without_touching_codex(self):
        gate = threading.Event()

        def script(model):
            gate.wait(3)

        service, server, _, _ = build(script, cfg=make_cfg(codex_queue_limit=0))
        worker, _ = run_in_background(service)
        assert wait_for(lambda: server.turns)
        before = len(server.requests)
        out = run(service, text="second")
        assert out.kind == "error" and out.reason == "bridge_busy"
        assert len(server.requests) == before
        service.cancel_active("test")
        gate.set()
        worker.join(3)

    def test_a_queued_request_waits_for_the_active_one(self):
        gate = threading.Event()

        def script(model):
            if not gate.is_set():
                gate.wait(3)
            model.answer("completed", "done " + model.request_id[:4])
            model.finish()

        service, server, _, _ = build(script)
        first, a = run_in_background(service)
        assert wait_for(lambda: server.turns)
        second, b = run_in_background(service, text="second")
        time.sleep(0.3)
        assert len(server.turns) == 1
        gate.set()
        first.join(3)
        second.join(3)
        assert a["out"].kind == b["out"].kind == "reply" and len(server.turns) == 2


def _turn_threads(server):
    return [p["threadId"] for p in server.params_of("turn/start")]


@pytest.mark.unit
class TestSpareThread:
    """A thread is started ahead of the request so Codex's per-thread setup is off the critical path."""

    def test_prepare_starts_a_thread_the_first_request_then_uses(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        assert service.prepare() is None
        assert wait_for(lambda: len(server.threads) == 1)
        spare = next(iter(server.threads))
        assert run(service).text == "ok"
        assert _turn_threads(server) == [spare]

    def test_each_request_leaves_a_fresh_thread_for_the_next_one(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        run(service)
        assert wait_for(lambda: len(server.threads) == 2)
        ready = [t for t in server.threads if t not in _turn_threads(server)]
        run(service)
        first, second = _turn_threads(server)
        assert second == ready[0] and second != first
        assert first in server.unsubscribed and second in server.unsubscribed

    def test_a_thread_started_before_a_codex_config_change_is_not_used(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        service.prepare()
        assert wait_for(lambda: len(server.threads) == 1)
        stale = next(iter(server.threads))
        server.effective_config = {"mcp_servers": {"other": {}}}
        assert run(service).text == "ok"
        used = _turn_threads(server)[0]
        assert used != stale and stale in server.unsubscribed
        assert server.threads[used]["config"] == {"mcp_servers.other.enabled": False}

    def test_a_thread_started_with_other_tools_is_not_used(self):
        tools = dict(TOOLS)
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        service._tools_provider = lambda cfg: dict(tools)
        service.prepare()
        assert wait_for(lambda: len(server.threads) == 1)
        stale = next(iter(server.threads))
        del tools["systemVolume"]
        assert run(service).text == "ok"
        used = _turn_threads(server)[0]
        assert used != stale and stale in server.unsubscribed
        assert server.threads[used]["dynamicTools"][0]["inputSchema"]["properties"]["tool_name"]["enum"] == ["getTime"]

    def test_a_thread_from_an_earlier_process_is_not_used(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        service.prepare()
        assert wait_for(lambda: len(server.threads) == 1)
        stale = next(iter(server.threads))
        server.close()
        assert run(service).text == "ok"
        assert _turn_threads(server)[0] != stale and server.starts == 2

    def test_a_failed_prestart_leaves_requests_working(self):
        service, server, _, _ = build(scripted(("answer", "completed", "ok")))
        server.fail["thread/start"] = "temporarily unavailable"
        service.prepare()
        assert wait_for(lambda: server.methods().count("thread/start") == 1)
        del server.fail["thread/start"]
        assert run(service).text == "ok"


@pytest.mark.unit
class TestNoCarryOver:
    def test_consecutive_requests_share_no_session_or_unshared_dialogue(self):
        def script(model):
            script.payloads.append((model.text, model.context))
            model.answer("completed", "ok")
            model.finish()

        script.payloads = []
        service, server, _, _ = build(script)
        run(service, text="my locker code is 4721", context=[])
        run(service, text="what is my locker code", context=[])
        threads = [p["threadId"] for p in server.params_of("turn/start")]
        assert len(set(threads)) == 2
        assert "thread/resume" not in server.methods() and "thread/fork" not in server.methods()
        assert "context" not in script.payloads[1][0]
        assert "4721" not in str(script.payloads[1])
        assert len(server.params_of("thread/unsubscribe")) == 2


@pytest.mark.unit
class TestConfirmation:
    def _confirming_run(self):
        executor = FakeExecutor()

        def script(model):
            model.execute("systemVolume", {"action": "mute"})
            script.after = [model.execute("getTime", {}, timeout=0.5)]
            model.answer("completed", "The user approved.")

        service, server, _, store = build(script, executor=executor)
        pending = SimpleNamespace(request=SimpleNamespace(id="conf-1"))

        def exec_needing_confirmation(*args, **kwargs):
            store.pending[kwargs.get("request_ref")] = pending
            executor.calls.append(args[2])
            return ToolExecutionResult(False, "I need your confirmation to delete report.pdf. Say yes or no.")

        service._executor = exec_needing_confirmation
        out = run(service)
        assert wait_for(lambda: hasattr(script, "after"))
        return service, server, store, out, script, executor.calls

    def test_confirmation_is_returned_immediately_and_the_turn_is_stopped(self):
        service, server, store, out, script, calls = self._confirming_run()
        assert out.kind == "awaiting_confirmation" and "confirmation" in out.text
        assert server.turns[0].results[0]["data"]["status"] == "awaiting_confirmation"
        assert server.params_of("turn/interrupt") and server.params_of("thread/unsubscribe")
        assert calls == ["systemVolume"]

    def test_codex_saying_the_user_approved_is_not_approval(self):
        service, server, store, out, script, calls = self._confirming_run()
        for late in script.after:
            assert late is NO_RESPONSE or late["success"] is False
        rid = server.turns[0].request_id
        assert service.broker.state_of(rid).value == "awaiting_confirmation"

    @pytest.mark.parametrize("outcome", ["denied", "expired", "replaced", "cancelled"])
    def test_non_approval_outcomes_cancel_the_request(self, outcome):
        service, server, store, *_ = self._confirming_run()
        store.emit("conf-1", outcome)
        assert service.broker.state_of(server.turns[0].request_id).value == "cancelled"

    def test_approval_completes_the_request_without_a_second_delivery(self):
        service, server, store, *_ = self._confirming_run()
        store.emit("conf-1", "approved")
        assert service.broker.state_of(server.turns[0].request_id).value == "completed"


@pytest.mark.unit
class TestShutdown:
    def test_close_cancels_requests_and_stops_the_owned_process(self):
        service, server, _, store = build()
        req = service.broker.submit_request("hi", [], "voice", "en", {})
        service.close()
        assert service.broker.state_of(req.id).value == "cancelled"
        assert server.closes == 1 and store.observers == []

    def test_nothing_starts_until_a_request_arrives(self):
        service, server, _, _ = build()
        assert server.starts == 0 and server.requests == []


@pytest.mark.unit
class TestToolSnapshot:
    def test_default_snapshot_excludes_router_tools_and_personal_data_tools(self):
        from jarvis.codex_bridge.service import codex_tool_snapshot
        snap = codex_tool_snapshot(SimpleNamespace(codex_share_long_term_memory=False, mcps={}))
        assert "toolSearchTool" not in snap and "refreshMCPTools" not in snap
        assert not ({"logMeal", "fetchMeals", "deleteMeal"} & set(snap))
        assert "getTime" in snap and snap["getTime"]["description"]

    def test_sharing_flag_adds_personal_data_tools(self):
        from jarvis.codex_bridge.service import codex_tool_snapshot
        snap = codex_tool_snapshot(SimpleNamespace(codex_share_long_term_memory=True, mcps={}))
        assert {"logMeal", "fetchMeals"} <= set(snap)
        assert "toolSearchTool" not in snap


@pytest.fixture
def windows_tools():
    """The real Windows tools registered for the bridge, restored afterwards."""
    from jarvis.config import load_settings
    from jarvis.tools import registry
    original = dict(registry.BUILTIN_TOOLS)
    cfg = load_settings()
    registry.configure_windows_tools(cfg, platform="win32", start_index=False)
    try:
        yield cfg
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def _snapshot():
    from jarvis.codex_bridge.service import codex_tool_snapshot
    return codex_tool_snapshot(SimpleNamespace(codex_share_long_term_memory=False, mcps={}))


@pytest.mark.unit
class TestPlacementThroughTheBridge:
    def test_snapshot_carries_the_extended_placement_schemas(self, windows_tools):
        snap = _snapshot()
        for name in ("appControl", "windowControl"):
            assert {"monitor", "zone", "state"} <= snap[name]["inputSchema"]["properties"].keys()
        assert {"displays", "place"} <= set(snap["windowControl"]["inputSchema"]["properties"]["action"]["enum"])

    def test_central_execution_accepts_placement_and_refuses_unknown_fields(self, windows_tools):
        place = {"action": "place", "target": "chrome", "monitor": "side", "zone": "left"}
        opening = {"action": "open", "target": "Word", "monitor": "side"}

        def script(model):
            script.out = [model.execute("windowControl", place),
                          model.execute("windowControl", {**place, "screen": "side"}),
                          model.execute("appControl", opening)]
            model.answer("completed", "done")
            model.finish()

        service, _, executor, _ = build(script, tools=_snapshot())
        run(service)
        assert [r["data"]["status"] for r in script.out] == ["ok", "refused", "ok"]
        assert [call[:2] for call in executor.calls] == [("windowControl", place), ("appControl", opening)]

    def _real_service(self, monkeypatch, script, **desk_kwargs):
        from jarvis.tools.registry import run_tool_with_retries
        from test_windows_placement_tools import Desk, placement_config, word_window
        desk = Desk(monkeypatch, appear=[(0, word_window(300))], **desk_kwargs)
        cfg = placement_config()

        def executor(db, _cfg, tool, args, *rest, **kw):
            return run_tool_with_retries(db, cfg, tool, args, *rest, **kw)

        service, _, _, _ = build(script, tools=_snapshot(), executor=executor)
        return service, desk

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop only")
    def test_a_replayed_open_and_place_launches_and_moves_only_once(self, windows_tools, monkeypatch):
        args = {"action": "open", "target": "Word", "monitor": "side", "zone": "left"}

        def script(model):
            script.out = [model.execute("appControl", args, call_id="c1"),
                          model.execute("appControl", args, call_id="c1"),
                          model.execute("appControl", args, call_id="c2")]
            model.answer("completed", "done")
            model.finish()

        service, desk = self._real_service(monkeypatch, script)
        run(service)
        assert [r["data"]["status"] for r in script.out] == ["ok", "ok", "ok"]
        assert desk.launches == ["word.lnk"] and list(desk.placed) == [300]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop only")
    def test_a_partial_failure_is_reported_with_its_structure_and_not_retried(self, windows_tools, monkeypatch):
        import json
        args = {"action": "open", "target": "Word", "monitor": "side"}

        def script(model):
            script.out = [model.execute("appControl", args), model.execute("appControl", args)]
            model.answer("failed", "could not place")
            model.finish()

        service, desk = self._real_service(monkeypatch, script, fail_placement="rejected")
        run(service)
        first, second = (r["data"] for r in script.out)
        assert first["status"] == "error" and second["status"] == "error"
        assert json.loads(first["text"])["placement"] == "failed" and json.loads(first["text"])["launch"] == "accepted"
        assert desk.launches == ["word.lnk"]


@pytest.mark.unit
class TestFailureMessages:
    @pytest.mark.parametrize("reason", [
        "not_found", "start_failed", "signed_out", "api_key_auth", "model_unavailable", "effort_unsupported",
        "unsupported", "session_failed", "process_exited", "usage_limit", "service_unavailable", "turn_failed",
        "interrupted", "no_answer", "bridge_busy", "payload_too_large", "timeout",
    ])
    def test_every_failure_has_a_specific_actionable_message(self, reason):
        from jarvis.codex_bridge.service import FAILURE_MESSAGES, failure_text
        assert reason in FAILURE_MESSAGES, reason
        assert "could not take that request" not in failure_text(reason)


@pytest.mark.unit
class TestClosed:
    """A request reaching a service the user has already switched away from sends nothing."""

    def test_a_request_after_close_starts_no_app_server(self):
        def script(model):
            model.answer("completed", "hi")
            model.finish()

        service, server, executor, _ = build(script)
        service.close()
        outcome = run(service)
        assert outcome.kind == "cancelled"
        assert server.starts == 0 and executor.calls == []

    def test_warm_up_after_close_starts_no_app_server(self):
        service, server, _, _ = build()
        service.close()
        service.prepare()
        assert server.starts == 0

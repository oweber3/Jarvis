"""The Claude bridge end to end: real client and service against the fake ``claude`` executable."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from claude_bridge_fakes import make_cfg
from codex_bridge_fakes import FakeExecutor, FakeStore
from jarvis.claude_bridge.cli import ClaudeSession, read_auth_status
from jarvis.claude_bridge.service import ClaudeBridgeService

FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude_cli.py"
TOOLS = {"getTime": {"description": "time", "inputSchema": {"type": "object", "properties": {}, "required": []}}}


def service_for(tmp_path, mode, **cfg):
    def factory(session_id, args):
        def popen(argv, **kwargs):
            return subprocess.Popen([sys.executable, str(FIXTURE), mode, *argv[1:]], **kwargs)
        return ClaudeSession("claude", args, session_id=session_id, cwd=tmp_path, process_factory=popen)

    def auth():
        return read_auth_status("claude", runner=lambda argv, **kw: subprocess.run(
            [sys.executable, str(FIXTURE), "auth", *argv[1:]], **kw))

    executor = FakeExecutor()
    service = ClaudeBridgeService(make_cfg(claude_model="haiku", **cfg), factory, executor=executor,
                                  confirmation_store=FakeStore(), tools_provider=lambda c: dict(TOOLS),
                                  auth_reader=auth, start_timeout_sec=15)
    return service, executor


def run(service, text="what time is it"):
    return service.run_request(text, [], "voice", "en", None, False)


@pytest.mark.unit
class TestEndToEnd:
    def test_a_tool_call_and_the_structured_answer_round_trip(self, tmp_path):
        service, executor = service_for(tmp_path, "tool")
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.text) == ("reply", "Tool said: getTime ok")
            assert [c[0] for c in executor.calls] == ["getTime"]
        finally:
            service.close()

    def test_a_rate_limited_turn_is_an_explicit_failure(self, tmp_path):
        service, executor = service_for(tmp_path, "rate_limited")
        try:
            outcome = run(service)
            assert (outcome.kind, outcome.reason) == ("error", "usage_limit")
        finally:
            service.close()

    def test_a_process_that_dies_mid_turn_is_reported(self, tmp_path):
        service, _ = service_for(tmp_path, "die_mid_turn")
        try:
            assert run(service).reason == "process_exited"
        finally:
            service.close()

    def test_an_api_key_sign_in_is_refused_before_anything_starts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", "api_key")
        record = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
        service, _ = service_for(tmp_path, "tool")
        try:
            assert run(service).reason == "api_key_auth"
            assert not record.exists()
        finally:
            service.close()

    def test_cancelling_interrupts_the_turn(self, tmp_path):
        import threading
        import time

        service, _ = service_for(tmp_path, "wait_interrupt")
        try:
            holder = {}
            worker = threading.Thread(target=lambda: holder.setdefault("out", run(service)), daemon=True)
            worker.start()
            end = time.monotonic() + 15
            while not service.is_busy() and time.monotonic() < end:
                time.sleep(0.05)
            time.sleep(1.0)
            assert service.cancel_active("stop")
            worker.join(15)
            assert holder["out"].kind == "cancelled"
        finally:
            service.close()

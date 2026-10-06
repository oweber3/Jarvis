"""The activityLog tool, its registration, and the cloud boundary (Codex and Claude reply modes)."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from jarvis.memory import activity_log as al
from jarvis.memory.activity_log import ActivityStore
from jarvis.tools.registry import BUILTIN_TOOLS, configure_activity_log_tool, run_tool_with_retries

NOW = time.time()
HOUR = 3600.0


@pytest.fixture(autouse=True)
def restore_registry():
    original = dict(BUILTIN_TOOLS)
    al.consume_turn_private()
    yield
    BUILTIN_TOOLS.clear()
    BUILTIN_TOOLS.update(original)
    al.consume_turn_private()


@pytest.fixture
def cfg(mock_config, tmp_path):
    mock_config.db_path = str(tmp_path / "jarvis.db")
    mock_config.activity_log_enabled = True
    mock_config.activity_log_share_with_cloud = False
    mock_config.windows_tools_enabled = True
    return mock_config


@pytest.fixture
def seeded(cfg):
    """Yesterday afternoon in Code and Chrome, with one title that carries an address."""
    store = ActivityStore(cfg.db_path)
    base = NOW - 24 * HOUR
    store.open_session(base, "Code", "Visual Studio Code", "activity_log.py - jarvis", end=base + 2 * HOUR)
    store.open_session(base + 2 * HOUR, "chrome", "Google Chrome", "Mail to carol@example.com", end=base + 3 * HOUR)
    store.close()
    return base


def call(cfg, **args):
    configure_activity_log_tool(cfg, platform="win32")
    return run_tool_with_retries(
        db=None, cfg=cfg, tool_name="activityLog", tool_args=args, system_prompt="", original_prompt="",
        redacted_text="", max_retries=0)


def iso(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


@pytest.mark.unit
class TestRegistration:
    def test_not_offered_while_the_log_is_off(self, cfg):
        cfg.activity_log_enabled = False
        configure_activity_log_tool(cfg, platform="win32")
        assert "activityLog" not in BUILTIN_TOOLS

    def test_offered_when_enabled_and_removed_when_disabled_again(self, cfg):
        configure_activity_log_tool(cfg, platform="win32")
        assert "activityLog" in BUILTIN_TOOLS
        cfg.activity_log_enabled = False
        configure_activity_log_tool(cfg, platform="win32")
        assert "activityLog" not in BUILTIN_TOOLS

    def test_not_offered_off_windows(self, cfg):
        configure_activity_log_tool(cfg, platform="linux")
        assert "activityLog" not in BUILTIN_TOOLS

    def test_description_routes_and_disambiguates(self, cfg):
        configure_activity_log_tool(cfg, platform="win32")
        tool = BUILTIN_TOOLS["activityLog"]
        assert "NOT for" in tool.description
        assert set(tool.inputSchema["properties"]["action"]["enum"]) == {"summary", "timeline"}
        assert "action" in tool.inputSchema["required"]


@pytest.mark.unit
class TestRunning:
    def test_summary_returns_raw_aggregates_for_the_range(self, cfg, seeded):
        result = call(cfg, action="summary", start=iso(seeded - 600), end=iso(seeded + 4 * HOUR))

        assert result.success
        data = json.loads(result.reply_text)
        assert [a["app"] for a in data["apps"]] == ["Visual Studio Code", "Google Chrome"]
        assert data["apps"][0]["seconds"] == 7200
        assert data["active_seconds"] == 3 * 3600

    def test_timeline_returns_ordered_sessions(self, cfg, seeded):
        result = call(cfg, action="timeline", start=iso(seeded - 600), end=iso(seeded + 4 * HOUR))

        data = json.loads(result.reply_text)
        assert [s["app"] for s in data["sessions"]] == ["Visual Studio Code", "Google Chrome"]

    def test_output_is_redacted(self, cfg, seeded):
        result = call(cfg, action="summary", start=iso(seeded - 600), end=iso(seeded + 4 * HOUR))
        assert "carol@example.com" not in result.reply_text

    def test_end_defaults_to_now(self, cfg, seeded):
        result = call(cfg, action="summary", start=iso(seeded - 600))
        assert result.success and json.loads(result.reply_text)["active_seconds"] == 3 * 3600

    def test_the_turn_is_marked_so_the_reply_stays_out_of_the_diary(self, cfg, seeded):
        call(cfg, action="summary", start=iso(seeded - 600))
        assert al.consume_turn_private() is True

    @pytest.mark.parametrize("args", [
        {"action": "summary"},
        {"action": "summary", "start": "yesterday afternoon"},
        {"action": "summary", "start": "2026-10-03T10:00:00", "end": "2026-10-03T09:00:00"},
        {"action": "timeline", "start": "2026-10-03T10:00:00", "end": "not a time"},
        {"action": "erase", "start": "2026-10-03T10:00:00"},
    ])
    def test_bad_input_fails_with_a_message_and_reads_nothing(self, cfg, seeded, args):
        result = call(cfg, **args)
        assert result.success is False and (result.error_message or "").strip()
        assert al.consume_turn_private() is False

    def test_timeline_limit_is_applied(self, cfg, seeded):
        result = call(cfg, action="timeline", start=iso(seeded - 600), end=iso(seeded + 4 * HOUR), limit=1)
        data = json.loads(result.reply_text)
        assert len(data["sessions"]) == 1 and data["truncated"] is True

    def test_logs_never_carry_titles_or_apps(self, cfg, seeded, monkeypatch):
        logged = []
        import jarvis.tools.builtin.activity_log as module
        monkeypatch.setattr(module, "debug_log", lambda message, *a, **k: logged.append(message))
        call(cfg, action="summary", start=iso(seeded - 600), end=iso(seeded + 4 * HOUR))
        text = " ".join(logged)
        assert logged and "activity_log.py" not in text and "Chrome" not in text and "carol" not in text


@pytest.mark.unit
class TestCloudBoundary:
    """Neither Codex nor Claude is offered the tool unless activity_log_share_with_cloud is true."""

    def snapshots(self, cfg):
        from jarvis.claude_bridge.service import claude_tool_snapshot
        from jarvis.codex_bridge.service import codex_tool_snapshot
        return {"codex": codex_tool_snapshot(cfg), "claude": claude_tool_snapshot(cfg)}

    def bridge_cfg(self, cfg, share):
        return SimpleNamespace(
            activity_log_share_with_cloud=share, mcps={},
            codex_share_long_term_memory=False, claude_share_long_term_memory=False,
            db_path=cfg.db_path, activity_log_enabled=True)

    def test_withheld_from_both_cloud_modes_by_default(self, cfg):
        configure_activity_log_tool(cfg, platform="win32")
        assert "activityLog" in BUILTIN_TOOLS  # the local model has it
        for mode, snapshot in self.snapshots(self.bridge_cfg(cfg, False)).items():
            assert "activityLog" not in snapshot, mode
            assert "getTime" in snapshot  # the gate removes one tool, not the catalogue

    def test_offered_to_both_only_when_sharing_is_switched_on(self, cfg):
        configure_activity_log_tool(cfg, platform="win32")
        for mode, snapshot in self.snapshots(self.bridge_cfg(cfg, True)).items():
            assert "activityLog" in snapshot, mode

    def test_only_a_real_true_shares(self, cfg):
        configure_activity_log_tool(cfg, platform="win32")
        for junk in ("true", 1, None):
            for mode, snapshot in self.snapshots(self.bridge_cfg(cfg, junk)).items():
                assert "activityLog" not in snapshot, (mode, junk)

    def test_a_call_for_the_withheld_tool_is_refused_without_running(self, cfg):
        from jarvis.bridge.broker import Broker, BrokerLimits
        configure_activity_log_tool(cfg, platform="win32")
        snapshot = self.snapshots(self.bridge_cfg(cfg, False))["claude"]
        broker = Broker(limits=BrokerLimits(deadline_sec=90, queue_limit=1, max_tool_calls=3),
                        validate_args=lambda schema, args: None)
        req = broker.submit_request("what was I doing", [], "voice", "en",
                                    {name: entry["inputSchema"] for name, entry in snapshot.items()})
        assert broker.assign_thread(req.id, "t") and broker.assign_turn(req.id, "u")

        decision = broker.begin_execute(req.id, "call-1", "activityLog", {"action": "summary"}, "t", "u")

        assert decision.kind == "refused"
        assert al.consume_turn_private() is False


@pytest.mark.unit
class TestStopSharingAtRuntime:
    """Switching cloud sharing off takes the log away from Codex and Claude at once, before any restart."""

    def test_stop_sharing_withholds_the_tool_and_private_dialogue(self, cfg, monkeypatch):
        from jarvis import daemon
        from jarvis.bridge.settings import bridge_settings
        from jarvis.memory import activity_runtime
        from desktop_app.activity_menu import request_line
        monkeypatch.setattr(activity_runtime, "_sharing_stopped", False)
        configure_activity_log_tool(cfg, platform="win32")
        boundary = TestCloudBoundary()
        shared = boundary.bridge_cfg(cfg, True)
        assert bridge_settings(shared, "claude").share_activity_log
        ran = []
        monkeypatch.setattr(daemon.threading, "Thread",
                            lambda target, **kw: SimpleNamespace(start=lambda: ran.append(target())))
        assert daemon.handle_activity_stdin_line(request_line("stop_sharing"))
        assert ran
        for mode in ("codex", "claude"):
            assert not bridge_settings(shared, mode).share_activity_log, mode
        for mode, snapshot in boundary.snapshots(shared).items():
            assert "activityLog" not in snapshot, mode

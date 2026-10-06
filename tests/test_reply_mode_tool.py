"""Switching the reply mode by voice or text: the replyMode tool, its fast-path phrases and the engine."""
from __future__ import annotations

import pytest

from jarvis.bridge import modes
from jarvis.bridge.tools import BridgeOutcome
from jarvis.tools.registry import BUILTIN_TOOLS, configure_reply_mode_tool


class FakeService:
    def __init__(self, mode):
        self.mode = mode
        self.requests = []
        self.closed = 0

    def run_request(self, utterance, context, origin, language, db, quiet, desktop=None):
        self.requests.append(utterance)
        return BridgeOutcome("reply", f"{self.mode} answered")

    def prepare(self):
        return None

    def is_busy(self):
        return False

    def cancel_active(self, reason):
        return False

    def close(self):
        self.closed += 1


@pytest.fixture
def services():
    return {}


@pytest.fixture
def allowed(mock_config, services):
    """Start in local mode with Codex and Claude allowed; returns the configuration."""
    def start(codex=True, claude=True, mode="local"):
        mock_config.reply_mode = mode
        mock_config.codex_enabled = codex
        mock_config.claude_enabled = claude
        configure_reply_mode_tool(mock_config)

        def factory(name):
            def build(cfg):
                services[name] = FakeService(name)
                return services[name]
            return build

        modes.start(mock_config, factories={"codex": factory("codex"), "claude": factory("claude")},
                    save=lambda values: True)
        return mock_config
    return start


@pytest.fixture(autouse=True)
def isolated():
    modes.reset()
    yield
    modes.reset()
    BUILTIN_TOOLS.pop("replyMode", None)


@pytest.mark.unit
class TestRegistration:
    def test_offered_only_when_a_cloud_mode_is_allowed(self, mock_config):
        mock_config.codex_enabled = mock_config.claude_enabled = False
        configure_reply_mode_tool(mock_config)
        assert "replyMode" not in BUILTIN_TOOLS
        mock_config.claude_enabled = True
        configure_reply_mode_tool(mock_config)
        assert "replyMode" in BUILTIN_TOOLS
        mock_config.claude_enabled = False
        configure_reply_mode_tool(mock_config)
        assert "replyMode" not in BUILTIN_TOOLS

    def test_description_routes_and_disambiguates(self, allowed):
        allowed()
        description = BUILTIN_TOOLS["replyMode"].description
        assert "Claude" in description[:120] and "local" in description[:120].lower()
        assert "NOT for" in description

    def test_routine_safety(self, allowed):
        from jarvis.tools.confirmation import SafetyTier, evaluate_safety
        cfg = allowed()
        request = evaluate_safety("replyMode", {"action": "set", "mode": "claude"}, cfg,
                                  tool=BUILTIN_TOOLS["replyMode"])
        assert request.tier == SafetyTier.SAFE

    def test_a_cloud_model_is_never_offered_the_switch(self, allowed):
        from jarvis.codex_bridge.service import codex_tool_snapshot
        from jarvis.claude_bridge.service import claude_tool_snapshot
        cfg = allowed()
        cfg.mcps = {}
        assert "replyMode" not in codex_tool_snapshot(cfg)
        assert "replyMode" not in claude_tool_snapshot(cfg)


@pytest.mark.unit
class TestFastPath:
    @pytest.mark.parametrize("text, mode", [
        ("use Claude", "claude"), ("Jarvis use claude mode", None), ("switch to Claude mode", "claude"),
        ("use ChatGPT", "codex"), ("use codex", "codex"), ("go local", "local"),
        ("switch to local mode", "local"), ("please go back to local mode", "local"),
    ])
    def test_phrases_select_the_mode(self, allowed, text, mode):
        from jarvis.fastpath.dispatcher import match_command
        cfg = allowed()
        match = match_command(text, cfg, "en")
        if mode is None:
            assert match is None or match.tool_name != "replyMode"
        else:
            assert (match.tool_name, match.args) == ("replyMode", {"action": "set", "mode": mode})

    @pytest.mark.parametrize("text", ["switch to Claude", "use claude to write me a poem",
                                      "what is claude mode", "use claude and open word"])
    def test_other_requests_are_not_mode_switches(self, allowed, text):
        from jarvis.fastpath.dispatcher import match_command
        match = match_command(text, allowed(), "en")
        assert match is None or match.tool_name != "replyMode"

    def test_no_phrase_matches_when_no_cloud_mode_is_allowed(self, allowed):
        from jarvis.fastpath.dispatcher import match_command
        assert match_command("use claude", allowed(codex=False, claude=False), "en") is None


@pytest.mark.unit
class TestSwitchingThroughTheEngine:
    def test_use_claude_then_the_next_request_goes_to_claude(self, allowed, services, db, dialogue_memory):
        from jarvis.reply import engine
        cfg = allowed()
        reply = engine.run_reply_engine(db, cfg, None, "use Claude", dialogue_memory, quiet=True)
        assert "Claude" in reply and "Anthropic" in reply
        assert modes.active_mode() == "claude"
        assert engine.run_reply_engine(db, cfg, None, "tell me about black holes", dialogue_memory,
                                       quiet=True) == "claude answered"
        assert services["claude"].requests == ["tell me about black holes"]

    def test_go_local_works_from_a_cloud_mode_without_reaching_the_bridge(self, allowed, services, db,
                                                                            dialogue_memory):
        from jarvis.reply import engine
        cfg = allowed(mode="codex")
        reply = engine.run_reply_engine(db, cfg, None, "go local", dialogue_memory, quiet=True)
        assert modes.active_mode() == "local" and "this PC" in reply
        assert services["codex"].requests == [] and services["codex"].closed == 1

    def test_a_mode_that_is_not_allowed_is_refused_honestly(self, allowed, services, db, dialogue_memory):
        from jarvis.reply import engine
        cfg = allowed(claude=False)
        reply = engine.run_reply_engine(db, cfg, None, "use codex", dialogue_memory, quiet=True)
        assert modes.active_mode() == "codex"
        tool = BUILTIN_TOOLS["replyMode"]
        from types import SimpleNamespace
        result = tool.run({"action": "set", "mode": "claude"}, SimpleNamespace(cfg=cfg, user_print=lambda *a: None))
        assert result.success is False and "not allowed" in result.error_message.lower()
        assert modes.active_mode() == "codex" and "claude" not in services

    def test_asking_for_the_current_mode(self, allowed):
        from types import SimpleNamespace
        cfg = allowed(mode="claude")
        result = BUILTIN_TOOLS["replyMode"].run({"action": "get"}, SimpleNamespace(cfg=cfg))
        assert result.success and "Claude" in result.reply_text

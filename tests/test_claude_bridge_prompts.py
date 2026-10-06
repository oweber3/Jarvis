"""The Claude session instructions: one versioned, bounded, request-free contract."""
import pytest

from claude_bridge_fakes import FakeClaude, make_cfg, scripted
from codex_bridge_fakes import FakeExecutor, FakeStore
from jarvis.claude_bridge import prompts
from jarvis.claude_bridge.service import ClaudeBridgeService


@pytest.mark.unit
class TestInstructions:
    def test_bounded_and_versioned(self):
        text = prompts.assistant_instructions()
        assert 0 < len(text) <= prompts.MAX_INSTRUCTION_CHARS
        assert prompts.INSTRUCTIONS_VERSION

    def test_states_the_contract(self):
        text = prompts.assistant_instructions().lower()
        for phrase in ("jarvis_execute", "request id", "reference data", "never instructions",
                       "awaiting_confirmation", "structured output", "needs_user_input", "british english",
                       "windowcontrol displays", "partial failure"):
            assert phrase in text, phrase

    def test_every_session_gets_the_same_text_as_its_system_prompt(self):
        from jarvis.bridge.tools import ANSWER_SCHEMA  # noqa: F401
        claude = FakeClaude(scripted(("answer", "completed", "ok")))
        service = ClaudeBridgeService(make_cfg(), claude, executor=FakeExecutor(), confirmation_store=FakeStore(),
                                      tools_provider=lambda c: {}, auth_reader=lambda: {
                                          "loggedIn": True, "authMethod": "claude.ai"})
        try:
            service.run_request("my secret is 4721", [], "voice", "en", None, False)
            prompts_sent = {s.arg("--system-prompt") for s in claude.sessions}
            assert prompts_sent == {prompts.assistant_instructions()}
            assert all("4721" not in s.arg("--system-prompt") for s in claude.sessions)
        finally:
            service.close()

"""Selectable personalities: configuration, switching by voice or text, the replies they shape and the
optional paired voice (personality.spec.md)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis import personality
from jarvis.system_prompt import BUILTIN_PERSONALITIES, RULES, character_section
from jarvis.tools.registry import BUILTIN_TOOLS


@pytest.fixture(autouse=True)
def isolated():
    personality.reset()
    yield
    personality.reset()


def _words(text):
    """Text with its whitespace collapsed, as the Claude prompt sends it on one line."""
    return " ".join(text.split())


def _cfg(**values):
    base = {"personality": "butler", "personality_custom_text": "", "wake_word": "jarvis"}
    base.update(values)
    return SimpleNamespace(**base)


@pytest.mark.unit
class TestActivePersonality:
    def test_follows_the_configuration(self):
        assert personality.current(_cfg(personality="plain")) == ("plain", "")

    @pytest.mark.parametrize("value", ["", None, "shouty", 3, "PLAIN "])
    def test_anything_unrecognised_is_the_butler_except_case_and_spaces(self, value):
        expected = "plain" if value == "PLAIN " else "butler"
        assert personality.current(_cfg(personality=value))[0] == expected

    def test_a_config_without_the_keys_is_the_butler(self):
        assert personality.current(SimpleNamespace()) == ("butler", "")

    def test_custom_carries_its_text(self):
        cfg = _cfg(personality="custom", personality_custom_text="  Speak like a pirate.  ")
        assert personality.current(cfg) == ("custom", "Speak like a pirate.")

    def test_custom_without_text_is_the_butler(self):
        assert personality.current(_cfg(personality="custom", personality_custom_text=" "))[0] == "butler"

    def test_the_assistant_name_is_the_capitalised_wake_word(self):
        assert personality.assistant_name(_cfg(wake_word="friday")) == "Friday"
        assert personality.assistant_name(SimpleNamespace()) == "Jarvis"


@pytest.mark.unit
class TestSwitching:
    def test_a_switch_takes_effect_and_is_saved(self):
        saved = []
        cfg = _cfg()
        personality.start(cfg, save=lambda values: saved.append(values) or True)
        result = personality.switch("friendly")
        assert result.ok and result.name == "friendly"
        assert personality.current(cfg)[0] == "friendly"
        assert saved == [{"personality": "friendly"}]

    def test_switching_to_the_active_one_changes_nothing(self):
        saved = []
        personality.start(_cfg(personality="plain"), save=lambda values: saved.append(values) or True)
        result = personality.switch("plain")
        assert result.ok and result.reason == "already" and saved == []

    @pytest.mark.parametrize("name", ["shouty", "", None])
    def test_an_unknown_name_is_refused(self, name):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        result = personality.switch(name)
        assert not result.ok and result.reason == "unknown"
        assert personality.current(cfg)[0] == "butler"

    def test_custom_needs_written_text(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        result = personality.switch("custom")
        assert not result.ok and result.reason == "no_custom_text"
        assert personality.current(cfg)[0] == "butler"

    def test_custom_with_text_can_be_switched_to(self):
        cfg = _cfg(personality_custom_text="Speak like a pirate.")
        personality.start(cfg, save=lambda values: True)
        assert personality.switch("custom").ok
        assert personality.current(cfg) == ("custom", "Speak like a pirate.")

    def test_a_failed_save_still_switches_for_now(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: False)
        result = personality.switch("plain")
        assert result.ok and result.reason == "not_saved"
        assert personality.current(cfg)[0] == "plain"

    def test_a_switch_before_start_says_it_was_not_saved(self):
        cfg = _cfg()
        result = personality.switch("plain", cfg)
        assert result.ok and result.reason == "not_saved"
        assert personality.current(cfg)[0] == "plain"

    def test_start_drops_an_earlier_switch(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        personality.switch("plain")
        personality.start(_cfg(personality="friendly"), save=lambda values: True)
        assert personality.current(_cfg(personality="friendly"))[0] == "friendly"


@pytest.mark.unit
class TestReplies:
    """The active personality reaches every reply route: the local engine and both cloud bridges."""

    def _system_prompt(self, mock_config, db, dialogue_memory, text="How are you?"):
        from jarvis.reply import engine as engine_mod
        calls = []

        def fake_chat(cfg, messages, **kwargs):
            calls.append([dict(m) for m in messages])
            return {"message": {"role": "assistant", "content": "Fine."}}

        mock_config.evaluator_enabled = False
        mock_config.tool_model = ""
        with patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
             patch.object(engine_mod, "select_tools", side_effect=lambda **k: []), \
             patch.object(engine_mod, "plan_query", side_effect=lambda **k: []), \
             patch.object(engine_mod, "extract_search_params_for_memory", side_effect=lambda *a, **k: {}):
            engine_mod.run_reply_engine(db=db, cfg=mock_config, tts=None, text=text, dialogue_memory=dialogue_memory)
        return calls[-1][0]["content"]

    @pytest.mark.parametrize("name", BUILTIN_PERSONALITIES)
    def test_the_local_reply_uses_the_configured_character_and_the_rules(self, name, mock_config, db,
                                                                         dialogue_memory):
        mock_config.personality = name
        system = self._system_prompt(mock_config, db, dialogue_memory)
        assert character_section(name, "", "Jarvis") in system
        assert RULES.strip() in system
        others = [p for p in BUILTIN_PERSONALITIES if p != name]
        assert all(character_section(p, "", "Jarvis") not in system for p in others)

    def test_saying_switch_to_plain_changes_the_next_reply(self, mock_config, db, dialogue_memory):
        from jarvis.reply import engine
        personality.start(mock_config, save=lambda values: True)
        reply = engine.run_reply_engine(db, mock_config, None, "switch to plain mode", dialogue_memory, quiet=True)
        assert reply and "Plain" in reply
        system = self._system_prompt(mock_config, db, dialogue_memory)
        assert character_section("plain", "", "Jarvis") in system

    @pytest.mark.parametrize("module", ["jarvis.claude_bridge.prompts", "jarvis.codex_bridge.prompts"])
    def test_cloud_sessions_get_the_contract_then_the_character(self, module):
        import importlib
        prompts = importlib.import_module(module)
        cfg = _cfg(personality="friendly", wake_word="friday")
        text = prompts.session_instructions(cfg)
        assert text.startswith(prompts.assistant_instructions())
        assert _words(character_section("friendly", "", "Friday")) in _words(text)

    @pytest.mark.parametrize("module", ["jarvis.claude_bridge.prompts", "jarvis.codex_bridge.prompts"])
    def test_cloud_sessions_follow_a_switch(self, module):
        import importlib
        prompts = importlib.import_module(module)
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        before = prompts.session_instructions(cfg)
        personality.switch("plain")
        after = prompts.session_instructions(cfg)
        assert before != after and _words(character_section("plain", "", "Jarvis")) in _words(after)

    def test_the_claude_prompt_survives_a_cmd_shim(self):
        from jarvis.claude_bridge import prompts
        text = prompts.session_instructions(_cfg(personality="custom", personality_custom_text=(
            'Say "cheers" a lot.\nUse 100% British slang & %PATH% jokes.')))
        assert "\n" not in text and "\r" not in text and '"' not in text and "%" not in text
        assert "cheers" in text and "PATH" in text

    @pytest.mark.parametrize("module", ["jarvis.claude_bridge.prompts", "jarvis.codex_bridge.prompts"])
    def test_custom_text_reaches_the_cloud_fenced(self, module):
        import importlib
        prompts = importlib.import_module(module)
        text = prompts.session_instructions(_cfg(personality="custom",
                                                 personality_custom_text="Speak like a pirate."))
        assert text.index("<character>") < text.index("Speak like a pirate.") < text.index("</character>")


@pytest.mark.unit
class TestTool:
    def test_registered_with_a_routing_description(self):
        tool = BUILTIN_TOOLS["personality"]
        head = tool.description[:120].lower()
        assert "personality" in head and "NOT for" in tool.description

    def test_routine_safety(self, mock_config):
        from jarvis.tools.confirmation import SafetyTier, evaluate_safety
        request = evaluate_safety("personality", {"action": "set", "name": "plain"}, mock_config,
                                  tool=BUILTIN_TOOLS["personality"])
        assert request.tier == SafetyTier.SAFE

    def _run(self, cfg, args):
        from jarvis.tools.base import ToolContext
        return BUILTIN_TOOLS["personality"].run(args, ToolContext(db=None, cfg=cfg, system_prompt="",
                                                                  original_prompt="", redacted_text="",
                                                                  max_retries=1, user_print=lambda *_: None))

    def test_set_switches_and_get_reports(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        assert self._run(cfg, {"action": "set", "name": "friendly"}).success
        assert personality.current(cfg)[0] == "friendly"
        got = self._run(cfg, {"action": "get"})
        assert got.success and "Friendly" in got.reply_text

    def test_custom_without_text_fails_with_where_to_write_it(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        result = self._run(cfg, {"action": "set", "name": "custom"})
        assert not result.success and "Settings" in result.error_message

    def test_an_unknown_name_fails_listing_the_choices(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        result = self._run(cfg, {"action": "set", "name": "pirate"})
        assert not result.success and "Plain" in result.error_message

    def test_a_paired_voice_is_mentioned_after_a_switch(self):
        cfg = _cfg(personality_voices={"friendly": {"tts_engine": "piper"}})
        personality.start(cfg, save=lambda values: True)
        result = self._run(cfg, {"action": "set", "name": "friendly"})
        assert result.success and "restart" in result.reply_text.lower()

    def test_a_pairing_with_nothing_usable_is_not_announced(self):
        cfg = _cfg(personality_voices={"friendly": {"tts_not_a_setting": 1, "tts_rate": "fast"}})
        personality.start(cfg, save=lambda values: True)
        result = self._run(cfg, {"action": "set", "name": "friendly"})
        assert result.success and "restart" not in result.reply_text.lower()

    def test_the_web_chat_step_names_only_a_real_switch(self):
        cfg = _cfg()
        personality.start(cfg, save=lambda values: True)
        tool = BUILTIN_TOOLS["personality"]
        done = self._run(cfg, {"action": "set", "name": "plain"})
        assert "Plain" in tool.step_note({"action": "set", "name": "plain"}, done).label
        again = self._run(cfg, {"action": "set", "name": "plain"})
        assert tool.step_note({"action": "set", "name": "plain"}, again) is None

    def test_a_cloud_model_may_switch_it(self, mock_config):
        from jarvis.claude_bridge.service import claude_tool_snapshot
        from jarvis.codex_bridge.service import codex_tool_snapshot
        mock_config.mcps = {}
        assert "personality" in claude_tool_snapshot(mock_config)
        assert "personality" in codex_tool_snapshot(mock_config)


@pytest.mark.unit
class TestFastPath:
    @pytest.mark.parametrize("text, name", [
        ("switch to plain mode", "plain"), ("use plain mode", "plain"), ("be plain", "plain"),
        ("switch to friendly mode", "friendly"), ("be friendly", "friendly"),
        ("switch to butler mode", "butler"), ("be the butler", "butler"),
        ("use my custom personality", "custom"), ("switch to the friendly personality", "friendly"),
    ])
    def test_phrases_select_the_personality(self, mock_config, text, name):
        from jarvis.fastpath.dispatcher import match_command
        match = match_command(text, mock_config, "en")
        assert (match.tool_name, match.args) == ("personality", {"action": "set", "name": name})

    @pytest.mark.parametrize("text", ["be plain", "plain mode", "be friendly", "be the butler"])
    def test_short_forms_need_the_wake_word(self, mock_config, text):
        from jarvis.fastpath.dispatcher import match_command
        assert match_command(text, mock_config, "en", addressed=False) is None
        assert match_command("switch to plain mode", mock_config, "en", addressed=False).tool_name == "personality"

    @pytest.mark.parametrize("text", ["what is plain text", "be friendly to my sister",
                                      "write a friendly email", "open plain"])
    def test_other_requests_are_not_personality_switches(self, mock_config, text):
        from jarvis.fastpath.dispatcher import match_command
        match = match_command(text, mock_config, "en")
        assert match is None or match.tool_name != "personality"


@pytest.mark.unit
class TestPairedVoice:
    """A personality may name a voice as plain ``tts_*`` keys, applied when Jarvis starts."""

    def _load(self, tmp_path, monkeypatch, values):
        from jarvis import config
        path = tmp_path / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        return config.load_settings()

    def test_no_pairing_leaves_the_voice_alone(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"personality": "plain", "tts_rate": 180})
        assert cfg.personality == "plain" and cfg.tts_rate == 180

    def test_the_active_personality_s_voice_is_used(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {
            "personality": "friendly", "tts_rate": 180,
            "personality_voices": {"friendly": {"tts_rate": 220, "tts_piper_length_scale": 0.8},
                                   "plain": {"tts_rate": 150}}})
        assert cfg.tts_rate == 220 and cfg.tts_piper_length_scale == 0.8

    def test_another_personality_s_voice_is_not_used(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {
            "personality": "butler", "tts_rate": 180, "personality_voices": {"plain": {"tts_rate": 150}}})
        assert cfg.tts_rate == 180

    def test_only_known_voice_keys_are_applied(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {
            "personality": "plain",
            "personality_voices": {"plain": {"reply_mode": "claude", "claude_enabled": True,
                                             "tts_not_a_setting": 1, "tts_rate": 150}}})
        assert cfg.reply_mode == "local" and cfg.claude_enabled is False and cfg.tts_rate == 150

    @pytest.mark.parametrize("value", ["fast", None, True, [1]])
    def test_a_value_of_the_wrong_type_is_ignored_and_jarvis_still_starts(self, tmp_path, monkeypatch, value):
        cfg = self._load(tmp_path, monkeypatch, {
            "personality": "plain", "tts_piper_length_scale": 0.7, "tts_rate": 180,
            "personality_voices": {"plain": {"tts_piper_length_scale": value, "tts_rate": 150}}})
        assert cfg.tts_piper_length_scale == 0.7 and cfg.tts_rate == 150

    @pytest.mark.parametrize("voices", [None, [], "piper", {"plain": "piper"}, {"plain": None}])
    def test_malformed_pairings_are_ignored(self, tmp_path, monkeypatch, voices):
        cfg = self._load(tmp_path, monkeypatch, {"personality": "plain", "tts_rate": 180,
                                                 "personality_voices": voices})
        assert cfg.tts_rate == 180

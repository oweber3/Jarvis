"""
Tests for wake word detection and query extraction.
"""

import pytest

from jarvis.listening.wake_detection import (
    is_wake_word_detected,
    extract_query_after_wake,
    is_stop_command,
)


@pytest.mark.unit
class TestConfiguredWakeWord:
    """A blank wake word in config.json falls back to the default, so it cannot match every utterance."""

    @pytest.mark.parametrize("stored", ["", "   ", None, 7])
    def test_a_blank_or_invalid_wake_word_loads_as_the_default(self, tmp_path, monkeypatch, stored):
        import json
        from jarvis.config import get_default_config, load_settings
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"_config_version": 6, "wake_word": stored}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        cfg = load_settings()
        assert cfg.wake_word == get_default_config()["wake_word"]
        assert not is_wake_word_detected("what should we have for dinner", cfg.wake_word, cfg.wake_aliases,
                                         cfg.wake_fuzzy_ratio)

    def test_a_chosen_wake_word_is_kept(self, tmp_path, monkeypatch):
        import json
        from jarvis.config import load_settings
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"_config_version": 6, "wake_word": " Friday "}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        assert load_settings().wake_word == "friday"


@pytest.mark.unit
class TestWakeWordDetection:
    """Tests for is_wake_word_detected."""

    def test_exact_match(self):
        assert is_wake_word_detected("hey jarvis", "jarvis", []) is True

    def test_alias_match(self):
        assert is_wake_word_detected("hey computer", "jarvis", ["computer"]) is True

    def test_no_match(self):
        assert is_wake_word_detected("hello world", "jarvis", []) is False

    def test_empty_text(self):
        assert is_wake_word_detected("", "jarvis", []) is False

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_wake_word_or_alias_never_matches_every_utterance(self, blank):
        assert is_wake_word_detected("what should we have for dinner", blank, [blank]) is False
        assert is_wake_word_detected("what should we have for dinner", "jarvis", [blank]) is False
        assert is_wake_word_detected("hey jarvis what time is it", blank, ["jarvis"]) is True

    def test_removing_the_wake_word_ignores_blank_aliases(self):
        assert extract_query_after_wake("jarvis what time is it", "jarvis", ["", " "]) == "what time is it"

    def test_fuzzy_match(self):
        """Fuzzy matching catches slight transcription errors."""
        assert is_wake_word_detected("hey jarvas", "jarvis", [], fuzzy_ratio=0.78) is True

    def test_fuzzy_below_threshold(self):
        """Completely different word doesn't fuzzy-match."""
        assert is_wake_word_detected("hey banana", "jarvis", [], fuzzy_ratio=0.78) is False


@pytest.mark.unit
class TestExtractQueryAfterWake:
    """Tests for extract_query_after_wake."""

    def test_extracts_query(self):
        result = extract_query_after_wake("jarvis what time is it", "jarvis", [])
        assert result == "what time is it"

    def test_extracts_query_with_alias(self):
        result = extract_query_after_wake("hey computer what time is it", "jarvis", ["hey computer"])
        assert result == "what time is it"

    def test_wake_word_only_returns_empty(self):
        """When only the wake word is said, return empty string (no hardcoded fallback)."""
        result = extract_query_after_wake("jarvis", "jarvis", [])
        assert result == ""

    def test_wake_word_with_punctuation_only_returns_empty(self):
        """Wake word followed by just punctuation returns empty string."""
        result = extract_query_after_wake("jarvis,", "jarvis", [])
        assert result == ""

    def test_empty_text(self):
        result = extract_query_after_wake("", "jarvis", [])
        assert result == ""

    def test_strips_leading_punctuation(self):
        result = extract_query_after_wake("jarvis, tell me a joke", "jarvis", [])
        assert result == "tell me a joke"


@pytest.mark.unit
class TestStopCommand:
    """Tests for is_stop_command."""

    def test_exact_stop_command(self):
        assert is_stop_command("stop", ["stop", "quiet"]) is True

    def test_stop_command_in_phrase(self):
        assert is_stop_command("please stop talking", ["stop", "quiet"]) is True

    def test_no_stop_command(self):
        assert is_stop_command("what is the weather", ["stop", "quiet"]) is False

    def test_empty_text(self):
        assert is_stop_command("", ["stop", "quiet"]) is False

    def test_fuzzy_stop_command(self):
        """Short input fuzzy-matches stop commands."""
        assert is_stop_command("stob", ["stop", "quiet"], fuzzy_ratio=0.7) is True

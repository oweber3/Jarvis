"""The models and effort levels the Codex and Claude runtimes report, cleaned for the model picker.

The runtimes' lists are untrusted data: bounded, with safe identifiers, no hidden models, and nothing
invented when a field is missing. See ``bridge/bridge.spec.md``, Cloud models.
"""
import pytest

from jarvis.bridge.model_catalog import choose_effort, claude_models, codex_models


def ids(models):
    return [m.id for m in models]


class TestCodexModels:
    RECORDS = [
        {"id": "a", "model": "gpt-6-luna", "displayName": "GPT-6 Luna", "isDefault": True,
         "defaultReasoningEffort": "medium",
         "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Fast"},
                                       {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
        {"id": "gpt-6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
        {"id": "secret", "model": "internal-model", "hidden": True},
    ]

    def test_visible_models_keep_the_reported_order_and_names(self):
        models = codex_models(self.RECORDS)
        assert ids(models) == ["gpt-6-luna", "gpt-6-sol"]
        assert models[0].name == "GPT-6 Luna"

    def test_the_model_field_wins_over_the_id_and_a_missing_name_falls_back_to_it(self):
        models = codex_models(self.RECORDS)
        assert models[1].name == "gpt-6-sol"

    def test_the_models_description_is_kept_for_the_picker(self):
        models = codex_models([{"id": "m", "description": "Fast\n and cheap"}, {"id": "n"}])
        assert models[0].description == "Fast and cheap" and models[1].description == ""

    def test_hidden_models_are_left_out(self):
        assert "internal-model" not in ids(codex_models(self.RECORDS))

    def test_efforts_come_from_what_the_model_advertises_with_its_default(self):
        luna = codex_models(self.RECORDS)[0]
        assert [e.id for e in luna.efforts] == ["low", "medium", "high"]
        assert [e.id for e in luna.efforts if e.is_default] == ["medium"]
        assert luna.efforts[0].description == "Fast"
        assert luna.is_default is True

    def test_a_model_with_no_efforts_has_none(self):
        assert codex_models([{"id": "plain"}])[0].efforts == ()

    @pytest.mark.parametrize("bad", [None, "x", 5, [], {}, [None], [{}], [{"id": ""}], [{"id": "has space"}],
                                     [{"id": "../etc/passwd"}], [{"id": "x" * 300}]])
    def test_unusable_input_gives_no_models(self, bad):
        assert codex_models(bad) == []

    def test_duplicates_and_unsafe_efforts_are_dropped(self):
        models = codex_models([
            {"id": "m", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "low"},
                                                      {"reasoningEffort": "bad one"}, {"reasoningEffort": 3}]},
            {"id": "m"},
        ])
        assert ids(models) == ["m"] and [e.id for e in models[0].efforts] == ["low"]

    def test_names_lose_control_characters_and_are_bounded(self):
        models = codex_models([{"id": "m", "displayName": "Nice\x00 \n name" + "x" * 500}])
        assert "\x00" not in models[0].name and "\n" not in models[0].name and len(models[0].name) <= 120

    def test_the_list_is_bounded(self):
        assert len(codex_models([{"id": f"m{i}"} for i in range(500)])) == 128


class TestClaudeModels:
    RECORDS = [
        {"value": "default", "displayName": "Default (recommended)", "supportsEffort": True,
         "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
        {"value": "haiku", "displayName": "Haiku", "supportsEffort": None, "supportedEffortLevels": None},
        {"value": "opus", "supportsEffort": False, "supportedEffortLevels": ["low"]},
    ]

    def test_models_use_the_value_and_the_display_name(self):
        models = claude_models(self.RECORDS)
        assert ids(models) == ["default", "haiku", "opus"]
        assert models[0].name == "Default (recommended)" and models[2].name == "opus"

    def test_a_model_that_lists_no_effort_levels_gets_no_effort_control(self):
        assert claude_models(self.RECORDS)[1].efforts == ()

    def test_levels_of_a_model_that_says_it_has_no_effort_are_ignored(self):
        assert claude_models(self.RECORDS)[2].efforts == ()

    def test_effort_levels_keep_the_reported_order(self):
        assert [e.id for e in claude_models(self.RECORDS)[0].efforts] == ["low", "medium", "high", "xhigh", "max"]

    @pytest.mark.parametrize("bad", [None, "x", [], [{}], [{"value": 3}], [{"value": "bad value"}]])
    def test_unusable_input_gives_no_models(self, bad):
        assert claude_models(bad) == []

    # What Claude Code reports: the alias is the display name and the version is only in the description.
    REAL = [
        {"value": "default", "displayName": "Default (recommended)", "description": "Opus 5.5 · Best for everyday tasks"},
        {"value": "sonnet", "displayName": "Sonnet", "description": "Sonnet 5.5 · Efficient for routine tasks"},
        {"value": "haiku", "displayName": "Haiku", "description": "Haiku 4.5 · Fastest for quick answers"},
        {"value": "plain", "displayName": "Plain", "description": "Just words, no version"},
        {"value": "bare", "displayName": "Bare"},
    ]

    def test_the_version_in_the_description_names_the_model(self):
        names = {m.id: m.name for m in claude_models(self.REAL)}
        assert names["sonnet"] == "Sonnet 5.5" and names["haiku"] == "Haiku 4.5"

    def test_a_default_alias_shows_the_model_it_points_at(self):
        assert claude_models(self.REAL)[0].name == "Default (recommended) · Opus 5.5"

    def test_the_rest_of_the_description_is_kept_for_the_picker(self):
        by_id = {m.id: m for m in claude_models(self.REAL)}
        assert by_id["sonnet"].description == "Efficient for routine tasks"
        assert by_id["plain"].name == "Plain" and by_id["plain"].description == "Just words, no version"
        assert by_id["bare"].description == ""


class TestChooseEffort:
    def model(self):
        return codex_models([{"id": "m", "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [
            {"reasoningEffort": "low"}, {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}])[0]

    def test_a_supported_preference_is_kept(self):
        assert choose_effort(self.model(), "high") == "high"

    def test_an_unsupported_one_falls_back_to_the_models_default(self):
        assert choose_effort(self.model(), "xhigh") == "medium"

    def test_with_no_default_the_first_is_used(self):
        model = claude_models([{"value": "m", "supportsEffort": True, "supportedEffortLevels": ["high", "max"]}])[0]
        assert choose_effort(model, "low") == "high"

    def test_a_model_without_efforts_has_no_effort(self):
        assert choose_effort(codex_models([{"id": "plain"}])[0], "low") is None

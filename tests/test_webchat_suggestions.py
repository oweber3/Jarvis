"""The web chat's start screen cards: built from configured routines and workspaces, then generic examples.

Pure card building, the locale templates against the real fast path, and the endpoint over real sockets.
See ``webchat/webchat.spec.md`` (Start screen).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.fastpath.matcher import FastTarget, match
from jarvis.webchat import suggestions
from jarvis.webchat.suggestions import MAX_CARDS, build_cards

TEMPLATES = {"routine": "Run my {name} routine", "workspace": "Open my {name} workspace"}
EXAMPLES = ["example one", "example two", "example three", "example four", "example five", "example six"]


def sources(routines=(), workspaces=(), windows=True):
    return {"routines": list(routines), "workspaces": list(workspaces), "workspaces_available": windows}


def cards(**kwargs):
    return build_cards(sources(**kwargs), TEMPLATES, EXAMPLES)


class TestBuildCards:
    def test_routines_come_first_then_workspaces_then_examples(self):
        result = cards(routines=["morning"], workspaces=["study"])
        assert [c["kind"] for c in result] == ["routine", "workspace", "example", "example", "example", "example"]
        assert result[0]["text"] == "Run my morning routine"
        assert result[1]["text"] == "Open my study workspace"

    def test_owner_cards_send_and_examples_fill_the_composer(self):
        result = cards(routines=["morning"], workspaces=["study"])
        assert {c["kind"]: c["action"] for c in result} == {"routine": "send", "workspace": "send", "example": "fill"}

    def test_nothing_configured_gives_only_examples(self):
        result = cards()
        assert result and {c["kind"] for c in result} == {"example"}
        assert len(result) == MAX_CARDS

    def test_the_total_is_capped_and_examples_always_have_room(self):
        result = cards(routines=[f"r{i}" for i in range(10)], workspaces=[f"w{i}" for i in range(10)])
        assert len(result) == MAX_CARDS
        assert any(c["kind"] == "example" for c in result)

    def test_workspaces_are_left_out_when_windows_tools_are_off(self):
        result = cards(workspaces=["study"], windows=False)
        assert "workspace" not in {c["kind"] for c in result}

    def test_names_are_used_as_written_in_any_language(self):
        result = cards(routines=["Morgenroutine", "朝のルーティン"])
        assert [c["text"] for c in result[:2]] == ["Run my Morgenroutine routine", "Run my 朝のルーティン routine"]

    def test_unusable_names_are_dropped(self):
        result = cards(routines=["", "  ", "two\nlines", "x" * 200, "fine"])
        assert [c["text"] for c in result if c["kind"] == "routine"] == ["Run my fine routine"]

    def test_a_name_cannot_inject_into_the_template(self):
        result = cards(routines=["{name}"])
        assert result[0]["text"] == "Run my {name} routine"

    def test_without_a_template_the_owner_cards_are_left_out(self):
        result = build_cards(sources(routines=["morning"], workspaces=["study"]), {}, EXAMPLES)
        assert {c["kind"] for c in result} == {"example"}

    def test_cards_carry_only_kind_text_and_action(self):
        for card in cards(routines=["morning"]):
            assert set(card) == {"kind", "text", "action"}


class TestLocaleTemplates:
    PHRASES = Path(suggestions.__file__).resolve().parents[1] / "fastpath" / "phrases"

    def test_every_locale_with_templates_has_both(self):
        for path in self.PHRASES.glob("*.json"):
            found = json.loads(path.read_text(encoding="utf-8")).get("suggestions")
            assert found and "{name}" in found["routine"] and "{name}" in found["workspace"], path.name

    def test_templates_load_for_the_first_known_language(self):
        assert "{name}" in suggestions.load_templates(["zz", "en"])["routine"]

    def test_unknown_languages_fall_back_to_english(self):
        assert suggestions.load_templates(["zz"]) == suggestions.load_templates(["en"])

    def test_a_routine_card_runs_that_routine_through_the_fast_path(self):
        text = build_cards(sources(routines=["morning"]), suggestions.load_templates(["en"]), EXAMPLES)[0]["text"]
        target = FastTarget(("morning",), "morning", "", "morning")
        found = match(text, "en", routines=(target,), available_tools=("routineControl",))
        assert found is not None and found.tool_name == "routineControl"
        assert found.args["name"] == "morning"

    def test_a_workspace_card_opens_that_workspace_through_the_fast_path(self):
        text = build_cards(sources(workspaces=["study"]), suggestions.load_templates(["en"]), EXAMPLES)[0]["text"]
        target = FastTarget(("study",), "study", "", "study")
        found = match(text, "en", workspaces=(target,), available_tools=("workspaceControl",))
        assert found is not None and found.tool_name == "workspaceControl"
        assert "study" in found.args.values()


class TestSourcesFromConfig:
    def test_names_only_come_from_routines_and_workspaces(self):
        from jarvis.webchat.backend import suggestion_sources
        cfg = SimpleNamespace(
            windows_tools_enabled=True,
            routines={"movie mode": {"aliases": ["film"], "steps": [{"tool": "systemVolume", "args": {"level": 30}}]}},
            windows_workspaces={"study": {"aliases": ["uni"], "items": [
                {"kind": "app", "label": "Notes", "app": r"C:\Secret\notes.exe"}]}})
        found = suggestion_sources(cfg)
        assert found == {"routines": ["movie mode"], "workspaces": ["study"], "workspaces_available": True}
        assert "Secret" not in json.dumps(found) and "30" not in json.dumps(found)

    def test_no_config_gives_nothing(self):
        from jarvis.webchat.backend import suggestion_sources
        assert suggestion_sources(None) == {"routines": [], "workspaces": [], "workspaces_available": False}

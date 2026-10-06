"""Routines are loaded from configuration structurally: malformed entries are dropped and counted, never named."""
import pytest

from jarvis.routines import definitions

MOVIE = {
    "aliases": ["film night"],
    "steps": [
        {"tool": "tvControl", "args": {"action": "launch", "app": "HDMI 1"}, "label": "TV to the console"},
        {"tool": "systemVolume", "args": {"action": "set", "percent": 30}},
        {"tool": "appControl", "args": {"action": "open", "target": "Music"}},
    ],
}


def step(tool="getTime", **extra):
    return {"tool": tool, **extra}


def test_a_well_formed_routine_is_loaded_with_its_aliases_and_steps():
    loaded = definitions.load_routines({"movie mode": MOVIE})
    routine = loaded["movie mode"]
    assert routine.name == "movie mode"
    assert routine.aliases == ("film night",)
    assert [s.tool for s in routine.steps] == ["tvControl", "systemVolume", "appControl"]
    assert routine.steps[1].args == {"action": "set", "percent": 30}
    assert routine.steps[0].label == "TV to the console"
    assert routine.steps[1].label is None


def test_args_default_to_an_empty_object_and_names_and_aliases_are_trimmed():
    loaded = definitions.load_routines({"  time check ": {"aliases": [" clock ", "", 3], "steps": [step()]}})
    routine = loaded["time check"]
    assert routine.aliases == ("clock",)
    assert routine.steps[0].args == {}


@pytest.mark.parametrize("bad", [
    {"x": "not an object"},
    {"": {"steps": [step()]}},
    {"   ": {"steps": [step()]}},
    {"x": {"steps": []}},
    {"x": {"steps": "getTime"}},
    {"x": {}},
    {"x": {"steps": [step() for _ in range(definitions.MAX_STEPS + 1)]}},
    {"x": {"steps": ["getTime"]}},
    {"x": {"steps": [{"args": {}}]}},
    {"x": {"steps": [{"tool": ""}]}},
    {"x": {"steps": [{"tool": 3}]}},
    {"x": {"steps": [step(args=[1, 2])]}},
    {"x": {"steps": [step(args="action=open")]}},
    {"x": {"steps": [step(label=5)]}},
])
def test_malformed_routines_are_dropped(bad):
    assert definitions.load_routines(bad) == {}


@pytest.mark.parametrize("value", [None, "text", 3, [], [MOVIE]])
def test_a_routines_value_that_is_not_an_object_loads_nothing(value):
    assert definitions.load_routines(value) == {}


def test_good_routines_survive_next_to_bad_ones():
    loaded = definitions.load_routines({"good": {"steps": [step()]}, "bad": {"steps": []}})
    assert list(loaded) == ["good"]


def test_the_maximum_number_of_steps_is_allowed():
    loaded = definitions.load_routines({"long": {"steps": [step() for _ in range(definitions.MAX_STEPS)]}})
    assert len(loaded["long"].steps) == definitions.MAX_STEPS


def test_loading_is_structural_so_unknown_tools_and_odd_arguments_still_load():
    loaded = definitions.load_routines({"later": {"steps": [step("someMcp__tool", args={"anything": [1]})]}})
    assert loaded["later"].steps[0].tool == "someMcp__tool"


def test_contested_names_and_aliases_follow_the_workspace_rule():
    loaded = definitions.load_routines({
        "movie mode": {"aliases": ["shared", "film"], "steps": [step()]},
        "MOVIE MODE": {"steps": [step()]},
        "tidy up": {"aliases": ["Shared", "clean"], "steps": [step()]},
        "bedtime": {"aliases": ["movie mode"], "steps": [step()]},
    })
    assert set(loaded) == {"tidy up", "bedtime"}
    assert loaded["tidy up"].aliases == ("clean",)
    assert loaded["bedtime"].aliases == ()


def test_loading_logs_counts_never_names(monkeypatch):
    logged = []
    monkeypatch.setattr(definitions, "debug_log", lambda message, category="": logged.append((message, category)))
    definitions.load_routines({
        "movie mode": MOVIE, "secret plan": {"steps": []}, "MOVIE MODE": {"steps": [step()]}})
    text = " ".join(message for message, _ in logged)
    assert logged and all(category == "routines" for _, category in logged)
    for private in ("movie", "secret", "film", "HDMI", "Music"):
        assert private.casefold() not in text.casefold()


def test_a_routine_is_found_by_name_or_alias_ignoring_case():
    loaded = definitions.load_routines({"Movie Mode": MOVIE})
    assert definitions.find("  movie MODE ", loaded).name == "Movie Mode"
    assert definitions.find("FILM NIGHT", loaded).name == "Movie Mode"
    assert definitions.find("bedtime", loaded) is None
    assert definitions.find("", loaded) is None


# --- labels -----------------------------------------------------------------------------

APP_SCHEMA = {"type": "object", "properties": {
    "target": {"type": "string"}, "action": {"type": "string"}, "monitor": {"type": "string"}}}
VOLUME_SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}, "percent": {"type": "number"}}}


def label(tool, args, schema=None, given=None):
    return definitions.step_label(definitions.Step(tool, args, given), schema)


def test_a_given_label_is_used_as_is():
    assert label("tvControl", {"action": "launch", "app": "HDMI 1"}, given="TV to the console") == "TV to the console"


def test_a_derived_label_is_the_tool_then_the_action_then_short_values_in_schema_order():
    assert label("appControl", {"target": "Music", "action": "open"}, APP_SCHEMA) == "appControl open Music"
    assert label("systemVolume", {"action": "set", "percent": 30}, VOLUME_SCHEMA) == "systemVolume set 30"
    assert label("systemVolume", {"percent": 12.5, "action": "set"}, VOLUME_SCHEMA) == "systemVolume set 12.5"
    assert label("appControl", {"action": "open", "target": "Music", "monitor": "2"}, APP_SCHEMA) == \
        "appControl open Music 2"


@pytest.mark.parametrize("value", [
    r"C:\Users\me\report.pdf", "~/notes.txt", "%USERPROFILE%\\x", "https://example.test/a", "docs/a.txt",
    "mailto:me@example.test",
])
def test_paths_and_web_addresses_never_appear_in_a_derived_label(value):
    schema = {"properties": {"action": {}, "target": {}}}
    assert label("openPath", {"action": "open", "target": value}, schema) == "openPath open"


def test_long_text_lists_objects_and_booleans_are_left_out_of_a_derived_label():
    schema = {"properties": {"action": {}, "text": {}, "items": {}, "flag": {}, "options": {}}}
    derived = label("inputControl", {"action": "type", "text": "x" * 200, "items": ["a"], "flag": True,
                                     "options": {"a": 1}}, schema)
    assert derived == "inputControl type"


def test_a_label_without_a_schema_keeps_the_arguments_own_order():
    assert label("someServer__lights", {"room": "lounge", "level": 40}) == "someServer__lights lounge 40"


def test_the_fingerprint_changes_when_the_steps_change_and_only_then():
    first = definitions.load_routines({"m": MOVIE})["m"]
    same = definitions.load_routines({"m": {**MOVIE, "aliases": ["other"]}})["m"]
    changed_steps = [*MOVIE["steps"][:1], {"tool": "systemVolume", "args": {"action": "set", "percent": 40}},
                     MOVIE["steps"][2]]
    changed = definitions.load_routines({"m": {**MOVIE, "steps": changed_steps}})["m"]
    assert definitions.fingerprint(first) == definitions.fingerprint(same)
    assert definitions.fingerprint(first) != definitions.fingerprint(changed)

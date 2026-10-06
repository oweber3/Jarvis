"""Running a routine by name is a deterministic fast command that falls through whenever it is unsure."""
import pytest

from jarvis.fastpath.matcher import FastTarget, match
from jarvis.routines import store
from jarvis.tools import registry
from jarvis.tools.confirmation import SafetyTier, get_confirmation_store
from routine_fakes import FakeTool

TOOLS = {"getTime", "appControl", "windowControl", "mediaControl", "routineControl"}


def routine_targets(*names):
    return tuple(FastTarget(names_, names_[0], "", names_[0][:1].upper() + names_[0][1:]) for names_ in names)


ROUTINES = routine_targets(("movie mode", "film night"), ("concentration",), ("bedtime",))


def route(text, routines=ROUTINES, tools=TOOLS, language="en", targets=()):
    return match(text, language, routines=routines, targets=targets, available_tools=tools)


@pytest.mark.parametrize("text,name", [
    ("Start movie mode", "movie mode"),
    ("run movie mode", "movie mode"),
    ("Run my movie mode routine", "movie mode"),
    ("start the movie mode routine", "movie mode"),
    ("Please run the bedtime routine", "bedtime"),
    ("Start film night", "movie mode"),
    ("start concentraton", "concentration"),  # spelling tolerance
    ("MOVIE MODE", "movie mode"),  # the bare name, which needs the wake word
])
def test_a_routine_name_or_alias_runs_that_routine(text, name):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ("routineControl", {"action": "run", "name": name})


def test_success_uses_the_locale_template():
    result = route("start movie mode")
    assert result.reply_template.format(**result.slots) == "Movie mode done."


def test_the_bare_name_needs_the_wake_word_and_the_verb_phrases_do_not():
    assert route("movie mode").needs_wake_word is True
    assert route("start movie mode").needs_wake_word is False


@pytest.mark.parametrize("text", [
    "start party mode",  # not a routine
    "start movie",  # never adds or drops words
    "start movie mode and dim the lights",
    "start movie mode then bedtime",
    "start movie mode tomorrow",
    "save that as movie mode",
    "delete my movie mode routine",
    "rename movie mode to cinema",
])
def test_uncertain_requests_and_changes_fall_through(text):
    assert route(text) is None, text


def test_a_name_with_a_compound_word_never_fast_matches():
    named = routine_targets(("lights and music",))
    assert route("start lights and music", routines=named) is None
    assert route("lights and music", routines=named) is None


def test_ambiguity_with_an_application_falls_through():
    apps = (FastTarget(("chrome",), "Google Chrome", "chrome", "Chrome"),)
    named = routine_targets(("chrome",))
    assert route("start chrome", routines=named, targets=apps) is None
    assert route("run chrome", routines=named, targets=apps).tool_name == "routineControl"
    assert route("start chrome", routines=(), targets=apps).tool_name == "appControl"


def test_a_routine_named_like_a_fixed_phrase_never_replaces_it():
    named = routine_targets(("pause the music",))
    assert route("pause the music", routines=named) is None


def test_an_unavailable_tool_or_an_unsupported_language_falls_through():
    assert route("start movie mode", tools={"getTime"}) is None
    assert route("start movie mode", language="fr") is None
    assert route("start movie mode", routines=()) is None


# --- the dispatcher and the engine -------------------------------------------------------

@pytest.fixture
def tools(monkeypatch):
    def add(name, **kwargs):
        tool = FakeTool(name, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()


@pytest.fixture
def movie(tools):
    tv = tools("projectorControl")
    lights = tools("lightsControl")
    store.load({"movie mode": {"aliases": ["film night"], "steps": [
        {"tool": "projectorControl", "args": {"action": "launch", "target": "HDMI 1"}},
        {"tool": "lightsControl", "args": {"action": "dim"}}]}})
    return tv, lights


def test_the_dispatcher_offers_routines_from_the_live_set(mock_config, movie):
    from jarvis.fastpath.dispatcher import match_command
    result = match_command("Start film night", mock_config, "en")
    assert result is not None and result.args == {"action": "run", "name": "movie mode"}
    assert match_command("start bedtime", mock_config, "en") is None


def test_the_route_does_not_depend_on_windows_tools(mock_config, movie):
    from jarvis.fastpath.dispatcher import match_command
    mock_config.windows_tools_enabled = False
    assert match_command("start movie mode", mock_config, "en") is not None


def test_the_bare_name_is_not_offered_to_a_hot_window_follow_up(mock_config, movie):
    from jarvis.fastpath.dispatcher import match_command
    assert match_command("movie mode", mock_config, "en", addressed=True) is not None
    assert match_command("movie mode", mock_config, "en", addressed=False) is None


def test_a_routine_with_a_confirming_step_falls_through_to_the_model(mock_config, tools):
    from jarvis.fastpath.dispatcher import match_command
    tools("localFiles", tiers={"delete": SafetyTier.CONFIRM_VOICE})
    store.load({"tidy up": {"steps": [{"tool": "localFiles", "args": {"action": "delete", "target": "a.pdf"}}]}})
    assert match_command("start tidy up", mock_config, "en") is None


def test_ambiguity_with_an_installed_application_falls_through_in_the_dispatcher(mock_config, tools, monkeypatch):
    from jarvis.fastpath.dispatcher import match_command
    tools("projectorControl")
    store.load({"chrome": {"steps": [{"tool": "projectorControl"}]}})
    app_control = registry.BUILTIN_TOOLS.get("appControl")
    if app_control is None:
        pytest.skip("appControl is registered only on Windows")
    monkeypatch.setattr(app_control, "fast_targets",
                        lambda cfg: (FastTarget(("chrome",), "Google Chrome", "chrome", "Chrome"),))
    assert match_command("start chrome", mock_config, "en") is None
    assert match_command("run chrome", mock_config, "en").tool_name == "routineControl"


def test_the_fast_route_never_logs_the_routine_name(mock_config, movie, monkeypatch):
    from jarvis.fastpath import dispatcher
    logged = []
    monkeypatch.setattr(dispatcher, "debug_log", lambda message, category="": logged.append(message))
    result = dispatcher.match_command("start film night", mock_config, "en")
    dispatcher.dispatch(result, None, mock_config, "start film night", "en")
    assert any("FAST_ROUTE" in message for message in logged)
    assert not any("movie" in message or "film" in message for message in logged)


def test_the_engine_runs_the_routine_without_a_model(mock_config, db, dialogue_memory, movie, monkeypatch):
    from jarvis.reply import engine
    tv, lights = movie

    def forbidden(*args, **kwargs):
        pytest.fail("A routine command must not call a model")
    for name in ("select_tools", "plan_query", "chat_with_messages", "extract_search_params_for_memory"):
        monkeypatch.setattr(engine, name, forbidden)
    reply = engine.run_reply_engine(db, mock_config, None, "Start movie mode", dialogue_memory, quiet=True)
    assert reply == "Movie mode done."
    assert tv.calls == [{"action": "launch", "target": "HDMI 1"}] and lights.calls == [{"action": "dim"}]


def test_a_failed_routine_returns_its_report_not_the_success_template(
        mock_config, db, dialogue_memory, movie, monkeypatch):
    from jarvis.reply import engine
    from routine_fakes import failing
    movie[1].behaviour = failing("The lights are offline.")
    reply = engine.run_reply_engine(db, mock_config, None, "Start movie mode", dialogue_memory, quiet=True)
    assert " ".join(reply.split()) == ("Movie mode: 1 of 2 steps done. "
                                       "Step 2 (lightsControl dim) failed: The lights are offline.")


def test_a_saved_routine_is_offered_to_the_fast_path_at_once(mock_config, tools, monkeypatch, tmp_path):
    import json
    import os
    from jarvis.fastpath.dispatcher import match_command
    tools("projectorControl")
    assert match_command("start movie mode", mock_config, "en") is None
    from pathlib import Path
    Path(os.environ["JARVIS_CONFIG_PATH"]).write_text(json.dumps({"routines": {
        "movie mode": {"steps": [{"tool": "projectorControl"}]}}}), encoding="utf-8")
    store.reload()
    assert match_command("start movie mode", mock_config, "en") is not None

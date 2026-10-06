"""Opening a configured workspace by name is a deterministic fast command that falls through when unsure."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from desktop_sim import SECOND, SimulatedDesktop  # noqa: E402

from jarvis.memory.desktop_referents import get_desktop_referents  # noqa: E402
from jarvis.platform.windows import workspaces  # noqa: E402

TOOLS = {'getTime', 'appControl', 'windowControl', 'workspaceControl', 'openPath'}


def workspace_targets(*names):
    from jarvis.fastpath.matcher import FastTarget
    return tuple(FastTarget(names_, names_[0], '', names_[0]) for names_ in names)


DESIGN = workspace_targets(('design', 'design project'), ('writing', 'essays'))


def route(text, targets=DESIGN, tools=TOOLS, language='en'):
    from jarvis.fastpath.matcher import match
    return match(text, language, workspaces=targets, available_tools=tools)


@pytest.mark.parametrize('text,name', [
    ('Open my design workspace', 'design'),
    ('open the design workspace', 'design'),
    ('Open design workspace', 'design'),
    ('Set up my design workspace', 'design'),
    ('set up design', 'design'),
    ('Start my design workspace', 'design'),
    ('Launch the design workspace', 'design'),
    ('PLEASE, could you open my Design workspace?!', 'design'),
    ('Open my workspace for design, please', 'design'),
    ('Set up design project', 'design'),
    ('open my essays workspace', 'writing'),
])
def test_a_workspace_name_or_alias_opens_that_workspace(text, name):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('workspaceControl', {'action': 'open', 'target': name})
    assert result.reply_template.format(**result.slots) == f'Opening your {name} workspace.'


@pytest.mark.parametrize('text', [
    'Open my gaming workspace',  # not configured
    'Open design',  # no workspace phrase: never competes with application names
    'Open my design workspace and close Chrome', 'Open my design workspace; close Chrome',
    'Open my design workspace on my second monitor', 'Open my design workspace tomorrow',
    'Do not open my design workspace', 'Can you help me decide which workspace to open?',
    'Open my desig workspace',  # close, not close enough: spelling tolerance never adds or drops letters here
    'Open my workspace', 'Set up', 'Set up my', 'Delete my design workspace',
])
def test_uncertain_requests_fall_through(text):
    assert route(text) is None, text


def test_unavailable_tool_unsupported_language_or_no_workspaces_fall_through():
    assert route('Open my design workspace', tools={'getTime'}) is None
    assert route('Open my design workspace', language='fr') is None
    assert route('Open my design workspace', targets=()) is None


def test_a_name_that_could_mean_two_workspaces_falls_through():
    from jarvis.fastpath.matcher import FastTarget
    tied = (FastTarget(('proj',), 'design', '', 'design'), FastTarget(('proj',), 'gaming', '', 'gaming'))
    assert route('open my proj workspace', targets=tied) is None
    distinct = workspace_targets(('design one',), ('design ono',))
    assert route('open my design one workspace', targets=distinct).args['target'] == 'design one'


def test_a_workspace_phrase_does_not_hijack_application_commands():
    from jarvis.fastpath.matcher import FastTarget, match
    apps = (FastTarget(('chrome',), 'Google Chrome', 'chrome', 'Chrome'),)
    result = match('Open Chrome', 'en', targets=apps, workspaces=DESIGN, available_tools=TOOLS)
    assert (result.tool_name, result.args['target']) == ('appControl', 'Google Chrome')


def test_a_workspace_named_like_an_app_is_still_opened_by_its_phrase():
    from jarvis.fastpath.matcher import FastTarget, match
    apps = (FastTarget(('chrome',), 'Google Chrome', 'chrome', 'Chrome'),)
    named = workspace_targets(('chrome',))
    result = match('Set up chrome', 'en', targets=apps, workspaces=named, available_tools=TOOLS)
    assert (result.tool_name, result.args) == ('workspaceControl', {'action': 'open', 'target': 'chrome'})
    result = match('Open my chrome workspace', 'en', targets=apps, workspaces=named, available_tools=TOOLS)
    assert result.tool_name == 'workspaceControl'


# --- the dispatcher and engine ----------------------------------------------------------

@pytest.fixture
def desktop(mock_config, tmp_path):
    from jarvis.tools.builtin.windows import WorkspaceControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    get_desktop_referents().clear()
    with SimulatedDesktop(mock_config) as sim:
        mock_config.windows_workspaces = workspaces.load_workspaces(sim.workspace_definitions(tmp_path))
        BUILTIN_TOOLS['workspaceControl'] = WorkspaceControlTool()
        try:
            yield sim
        finally:
            BUILTIN_TOOLS.pop('workspaceControl', None)
            get_desktop_referents().clear()


def test_the_dispatcher_offers_configured_workspaces(mock_config, desktop):
    from jarvis.fastpath.dispatcher import match_command
    result = match_command('Open my design workspace', mock_config, 'en')
    assert result is not None and result.args == {'action': 'open', 'target': 'design'}
    assert match_command('Open my design project workspace', mock_config, 'en').args['target'] == 'design'
    assert match_command('Open my gaming workspace', mock_config, 'en') is None


def test_the_dispatcher_ignores_workspaces_when_windows_tools_are_disabled(mock_config, desktop):
    from jarvis.fastpath.dispatcher import match_command
    mock_config.windows_tools_enabled = False
    assert match_command('Open my design workspace', mock_config, 'en') is None


def test_the_dispatcher_ignores_workspaces_when_the_tool_is_not_registered(mock_config, desktop):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS.pop('workspaceControl')
    assert match_command('Open my design workspace', mock_config, 'en') is None


def test_the_engine_opens_the_workspace_without_a_model(mock_config, db, dialogue_memory, desktop, monkeypatch):
    from jarvis.reply import engine

    def forbidden(*args, **kwargs):
        pytest.fail('A workspace command must not call a model')
    for name in ('select_tools', 'plan_query', 'chat_with_messages', 'extract_search_params_for_memory'):
        monkeypatch.setattr(engine, name, forbidden)
    before = {w.hwnd for w in desktop.open_windows()}
    reply = engine.run_reply_engine(db, mock_config, None, 'Open my design workspace', dialogue_memory, quiet=True)
    assert reply == 'Opening your design workspace.'
    new = [w for w in desktop.open_windows('chrome') if w.hwnd not in before]
    assert len(new) == 2 and all(w.monitor == SECOND for w in new)
    assert {r.application for r in get_desktop_referents().recent(300)} == {'textbooks', 'chat'}


def test_a_failed_workspace_returns_the_structured_failure_not_a_success_message(
        mock_config, db, dialogue_memory, desktop):
    from jarvis.reply import engine
    desktop.fail_placement = 'The application did not reach the requested position.'
    reply = engine.run_reply_engine(db, mock_config, None, 'Open my design workspace', dialogue_memory, quiet=True)
    assert 'Opening your design workspace' not in reply
    assert json.loads(reply)['action'] == 'workspace_partial'

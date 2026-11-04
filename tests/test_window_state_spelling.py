"""Window states and actions accept the American spellings and act as the British forms the schemas list.
OS calls are faked; see ``platform/windows/apps_paths.spec.md`` (Window control)."""
import json
from dataclasses import replace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg():
    from jarvis.config import load_settings
    return replace(load_settings(), windows_tools_enabled=True, windows_app_aliases={})


def _context(cfg):
    from jarvis.tools.base import ToolContext
    return ToolContext(None, cfg, '', '', '', 1, lambda _text: None)


@pytest.mark.parametrize('word, british', [('maximize', 'maximise'), ('MAXIMIZE', 'maximise'),
                                           ('Minimize', 'minimise'), (' maximise ', 'maximise'),
                                           ('Minimise', 'minimise')])
def test_both_spellings_in_any_case_become_the_british_form(word, british):
    from jarvis.platform.windows.window_states import normalise_window_word
    assert normalise_window_word(word) == british


@pytest.mark.parametrize('word', ['restore', 'close', 'maximised', 'maxim', '', None, 3])
def test_other_words_pass_through_unchanged(word):
    from jarvis.platform.windows.window_states import normalise_window_word
    assert normalise_window_word(word) == word


@pytest.mark.parametrize('action, british', [('maximize', 'maximise'), ('Minimize', 'minimise')])
def test_window_control_runs_american_actions_as_the_british_ones(cfg, monkeypatch, action, british):
    from jarvis.platform.windows import windows_mgmt as wm
    from jarvis.tools.builtin.windows import WindowControlTool
    shown = []
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Chrome', 'chrome', 1)])
    monkeypatch.setattr(wm, '_show_window', lambda hwnd, value: shown.append((hwnd, value)))
    result = WindowControlTool().run({'action': action, 'target': 'chrome'}, _context(cfg))
    assert result.success, result.error_message
    assert shown == [(101, british)]
    assert json.loads(result.reply_text)['action'] == british


def test_the_os_layer_accepts_american_actions(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm
    shown = []
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Chrome', 'chrome', 1)])
    monkeypatch.setattr(wm, '_show_window', lambda hwnd, value: shown.append(value))
    assert wm.control_window('MINIMIZE', 'chrome')['action'] == 'minimise'
    assert shown == ['minimise']


def test_placement_state_accepts_the_american_spelling():
    from jarvis.tools.builtin.windows.desktop_control import placement_request
    assert placement_request({'state': 'Maximize'})['state'] == 'maximise'
    with pytest.raises(ValueError):
        placement_request({'state': 'maximize', 'zone': 'left'})


def test_os_placement_validation_accepts_the_american_spelling():
    from jarvis.platform.windows import windows_mgmt as wm
    from jarvis.platform.windows.displays import Monitor
    monitor = Monitor(device=r'\\.\DISPLAY1', bounds=(0, 0, 1920, 1080), work_area=(0, 0, 1920, 1040),
                      primary=True)
    assert wm.validate_placement(monitor, None, 'maximize') is None
    with pytest.raises(ValueError):
        wm.validate_placement(monitor, (0, 0, 100, 100), 'MAXIMIZE')


def test_schemas_list_only_the_british_forms():
    from jarvis.tools.builtin.windows import WindowControlTool
    from jarvis.tools.builtin.windows.desktop_control import PLACEMENT_PROPERTIES
    schema = WindowControlTool().inputSchema['properties']
    words = set(schema['action']['enum']) | set(PLACEMENT_PROPERTIES['state']['enum'])
    assert {'minimise', 'maximise'} <= words and not {'minimize', 'maximize'} & words


def test_a_workspace_item_with_the_american_spelling_is_loaded_as_british():
    from jarvis.platform.windows.workspaces import _load_item
    item = _load_item({'kind': 'app', 'target': 'Spotify', 'monitor': 'primary', 'state': 'Maximize'}, 0)
    assert item is not None and item['state'] == 'maximise'

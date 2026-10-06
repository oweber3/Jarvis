"""Monitor and zone placement through the existing appControl and windowControl tools.

The OS boundary (monitors, windows, launching and the placement call itself) is
replaced by ``Desk``; placement geometry has its own tests in test_windows_placement.py.
"""
import json
from dataclasses import replace

import pytest

from jarvis.platform.windows.displays import Monitor, zone_rectangle

PRIMARY = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
LEFT = Monitor(r'\\.\DISPLAY2', (-1920, -200, 0, 880), (-1920, -200, 0, 840), False)
ZONES = {LEFT.device: {'left': [0, 0, 0.5, 1], 'right': [0.5, 0, 0.5, 1]},
         r'\\.\DISPLAY9': {'ghost': [0, 0, 1, 1]}}
WORD = ('Microsoft Word', 'word.lnk', 'C:/Office/WINWORD.EXE')


def placement_config(**changes):
    from jarvis.config import load_settings
    values = dict(windows_monitor_aliases={'side': LEFT.device, 'gone': r'\\.\DISPLAY9'},
                  windows_window_zones=ZONES, windows_app_aliases={})
    values.update(changes)
    return replace(load_settings(), **values)


class Desk:
    """A fake desktop: windows, one-shot launches and recorded placements."""

    def __init__(self, monkeypatch, windows=(), appear=(), fail_placement=None):
        from jarvis.platform.windows import apps, displays, windows_mgmt as wm
        self.windows = list(windows)
        self.appear = list(appear)  # (poll number, Window) pairs revealed by later listings
        self.launches, self.placed, self.polls = [], {}, 0
        self.fail_placement = fail_placement
        monkeypatch.setattr(displays, 'list_monitors', lambda: [PRIMARY, LEFT])
        monkeypatch.setattr(apps.APP_INDEX, 'applications', lambda: [apps.Application(*WORD)])
        monkeypatch.setattr(apps.os, 'startfile', self.launches.append)
        monkeypatch.setattr(wm, 'list_windows', self.list_windows)
        monkeypatch.setattr(wm, 'place_window', self.place_window)
        for name, value in {'_POLL_SEC': .01, '_STABLE_SEC': .05, '_REUSE_GRACE_SEC': .2,
                            '_PLACE_BUDGET_SEC': 1.5}.items():
            monkeypatch.setattr(apps, name, value)

    def list_windows(self):
        self.polls += 1
        return self.windows + [w for after, w in self.appear if self.polls > after]

    def place_window(self, hwnd, monitor, rectangle=None, state='restore', *, deadline=None):
        if self.fail_placement:
            raise OSError(self.fail_placement)
        landed = list(monitor.work_area if state == 'maximise' else rectangle or (10, 10, 810, 610))
        self.placed[hwnd] = {'monitor': monitor.device, 'rectangle': landed, 'state': state}
        return {'hwnd': hwnd, 'monitor': monitor.device, 'rectangle': landed, 'state': state}


def call(tool, args, cfg=None):
    from jarvis.tools.base import ToolContext
    return tool.run(args, ToolContext(None, cfg or placement_config(), '', '', '', 1, lambda text: None))


def data(result):
    return json.loads(result.reply_text)


def word_window(hwnd, title='Document'):
    from jarvis.platform.windows.windows_mgmt import Window
    return Window(hwnd, title, 'WINWORD', 4000 + hwnd)


def chrome_window(hwnd):
    from jarvis.platform.windows.windows_mgmt import Window
    return Window(hwnd, 'Docs', 'chrome', 100)


def tools():
    from jarvis.tools.builtin.windows import AppControlTool, WindowControlTool
    return AppControlTool(), WindowControlTool()


# --- discovery -----------------------------------------------------------------------

def test_displays_report_live_monitors_and_only_usable_labels(monkeypatch):
    _, windows = tools()
    Desk(monkeypatch)
    result = call(windows, {'action': 'displays', 'target': ''})
    assert result.success
    by_device = {entry['device']: entry for entry in data(result)['displays']}
    assert set(by_device) == {PRIMARY.device, LEFT.device}
    assert by_device[LEFT.device]['bounds'] == list(LEFT.bounds)
    assert by_device[LEFT.device]['work_area'] == list(LEFT.work_area)
    assert by_device[PRIMARY.device]['primary'] is True
    assert by_device[LEFT.device]['aliases'] == ['side']
    assert by_device[LEFT.device]['zones'] == {
        'left': list(zone_rectangle(LEFT, [0, 0, 0.5, 1])), 'right': list(zone_rectangle(LEFT, [0.5, 0, 0.5, 1]))}
    assert by_device[PRIMARY.device]['zones'] == {} and by_device[PRIMARY.device]['aliases'] == []
    assert 'ghost' not in result.reply_text and 'gone' not in result.reply_text


# --- windowControl place -------------------------------------------------------------

def test_place_moves_only_the_selected_window_into_a_zone(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101), word_window(202)])
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'Side', 'zone': 'RIGHT'})
    assert result.success
    expected = list(zone_rectangle(LEFT, [0.5, 0, 0.5, 1]))
    assert data(result) == {'action': 'placed', 'hwnd': 101, 'process': 'chrome', 'pid': 100,
                            'monitor': LEFT.device, 'zone': 'right', 'rectangle': expected, 'state': 'restore'}
    assert desk.placed == {101: {'monitor': LEFT.device, 'rectangle': expected, 'state': 'restore'}}


def test_place_by_identifier_and_handle_without_a_zone(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101)])
    result = call(windows, {'action': 'place', 'target': '101', 'monitor': PRIMARY.device})
    assert result.success and 'zone' not in data(result)
    assert desk.placed[101]['monitor'] == PRIMARY.device and desk.placed[101]['state'] == 'restore'


def test_place_resolves_application_aliases_through_the_catalogue(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101), word_window(202)])
    cfg = placement_config(windows_app_aliases={'editor': 'Microsoft Word'})
    assert call(windows, {'action': 'place', 'target': 'editor', 'monitor': 'side'}, cfg).success
    assert list(desk.placed) == [202]


def test_maximise_fills_the_destination_work_area_not_a_zone(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101)])
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'side', 'state': 'maximise'})
    assert result.success and data(result)['rectangle'] == list(LEFT.work_area)
    assert desk.placed[101]['state'] == 'maximise'


@pytest.mark.parametrize('args', [
    {'action': 'place', 'target': 'chrome', 'monitor': 'side', 'zone': 'left', 'state': 'maximise'},
    {'action': 'place', 'target': 'chrome', 'monitor': 'side', 'state': 'fullscreen'},
    {'action': 'place', 'target': 'chrome', 'monitor': 'side', 'zone': 'missing'},
    {'action': 'place', 'target': 'chrome', 'monitor': 'side', 'zone': ['left']},
    {'action': 'place', 'target': 'chrome', 'monitor': 'unknown'},
    {'action': 'place', 'target': 'chrome', 'monitor': 'gone'},
    {'action': 'place', 'target': 'chrome', 'monitor': r'\\.\DISPLAY9', 'zone': 'ghost'},
    {'action': 'place', 'target': '', 'monitor': 'side'},
    {'action': 'place', 'target': 'chrome', 'monitor': 5},
    {'action': 'minimise', 'target': 'chrome', 'monitor': 'side'},
    {'action': 'list', 'target': '', 'zone': 'left'},
    {'action': 'displays', 'target': 'chrome'},
    {'action': 'displays', 'target': '', 'monitor': 'side'},
])
def test_unsupported_placement_arguments_change_nothing(args, monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101)])
    result = call(windows, args)
    assert not result.success and desk.placed == {} and desk.launches == []


# --- appControl open with placement ---------------------------------------------------

@pytest.mark.parametrize('args', [
    {'action': 'open', 'target': 'Word', 'monitor': 'side', 'zone': 'left', 'state': 'maximise'},
    {'action': 'open', 'target': 'Word', 'monitor': 'side', 'zone': 'missing'},
    {'action': 'open', 'target': 'Word', 'monitor': 'gone'},
    {'action': 'open', 'target': 'Word', 'monitor': 'unknown'},
    {'action': 'open', 'target': 'Word', 'monitor': 'side', 'state': 'tiny'},
    {'action': 'focus', 'target': 'Word', 'monitor': 'side'},
    {'action': 'close', 'target': 'Word', 'state': 'restore'},
])
def test_invalid_open_and_place_requests_never_launch(args, monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, windows=[word_window(1)])
    assert not call(apps_tool, args).success
    assert desk.launches == [] and desk.placed == {}


PRIMARY_ZONES = {PRIMARY.device: {'left': [0, 0, 0.5, 1], 'right': [0.5, 0, 0.5, 1]}}


def test_a_zone_without_a_monitor_places_on_the_main_display(monkeypatch):
    apps_tool, windows = tools()
    cfg = placement_config(windows_window_zones=PRIMARY_ZONES, windows_fancyzones_enabled=False)
    desk = Desk(monkeypatch, windows=[chrome_window(101)], appear=[(0, word_window(300))])
    placed = call(windows, {'action': 'place', 'target': 'chrome', 'zone': 'right'}, cfg)
    opened = call(apps_tool, {'action': 'open', 'target': 'Word', 'zone': 'left'}, cfg)
    assert placed.success and opened.success
    assert data(placed)['monitor'] == data(opened)['monitor'] == PRIMARY.device
    assert desk.placed[101]['rectangle'] == list(zone_rectangle(PRIMARY, [0.5, 0, 0.5, 1]))
    assert desk.placed[300]['rectangle'] == list(zone_rectangle(PRIMARY, [0, 0, 0.5, 1]))
    assert desk.launches == ['word.lnk']


def test_maximise_or_a_bare_place_without_a_monitor_uses_the_main_display(monkeypatch):
    apps_tool, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101)], appear=[(0, word_window(300))])
    assert call(windows, {'action': 'place', 'target': 'chrome'}).success
    assert call(apps_tool, {'action': 'open', 'target': 'Word', 'state': 'maximise'}).success
    assert desk.placed[101]['monitor'] == PRIMARY.device
    assert desk.placed[300] == {'monitor': PRIMARY.device, 'rectangle': list(PRIMARY.work_area), 'state': 'maximise'}


def test_open_with_a_monitor_launches_once_and_places_the_new_window(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101)],
                appear=[(3, chrome_window(103)), (3, word_window(300, 'Document1'))])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side', 'zone': 'left'})
    assert result.success
    expected = list(zone_rectangle(LEFT, [0, 0, 0.5, 1]))
    assert data(result) == {'action': 'opened_and_placed', 'application': 'Microsoft Word', 'hwnd': 300,
                            'process': 'WINWORD', 'pid': 4300, 'monitor': LEFT.device, 'zone': 'left',
                            'rectangle': expected, 'state': 'restore', 'reused_window': False}
    assert desk.launches == ['word.lnk'] and list(desk.placed) == [300]
    assert desk.polls > 3  # startup was not instantaneous


def test_open_and_maximise_on_the_destination_monitor(monkeypatch):
    apps_tool, _ = tools()
    Desk(monkeypatch, appear=[(0, word_window(300))])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side', 'state': 'maximise'})
    assert result.success and data(result)['state'] == 'maximise'
    assert data(result)['rectangle'] == list(LEFT.work_area)


def test_single_instance_application_reuses_its_one_window(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, windows=[word_window(300), chrome_window(101)])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side'})
    assert result.success and data(result)['hwnd'] == 300 and data(result)['reused_window'] is True
    assert desk.launches == ['word.lnk'] and list(desk.placed) == [300]


def test_several_existing_windows_and_no_new_one_is_ambiguous(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, windows=[word_window(300, 'Private plan'), word_window(301)])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side'})
    assert not result.success and desk.placed == {} and desk.launches == ['word.lnk']
    report = data(result)
    assert (report['launch'], report['placement']) == ('accepted', 'ambiguous')
    assert {c['hwnd'] for c in report['candidates']} == {300, 301}


def test_several_new_windows_are_ambiguous_not_arbitrary(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, appear=[(1, word_window(300)), (1, word_window(301))])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side'})
    assert not result.success and desk.placed == {}
    assert data(result)['placement'] == 'ambiguous'


def test_placement_failure_after_an_accepted_launch_is_reported_as_partial(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, appear=[(0, word_window(300))],
                fail_placement='The application did not reach the requested position.')
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side', 'zone': 'left'})
    report = data(result)
    assert not result.success and desk.launches == ['word.lnk']
    assert (report['launch'], report['placement'], report['hwnd']) == ('accepted', 'failed', 300)
    assert 'requested position' in report['reason'] and 'requested position' in result.error_message


def test_a_window_that_never_appears_means_placement_is_unverified(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch)
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side'})
    assert not result.success and desk.placed == {} and desk.launches == ['word.lnk']
    assert (data(result)['launch'], data(result)['placement']) == ('accepted', 'unverified')


def test_candidate_titles_are_redacted(monkeypatch):
    apps_tool, _ = tools()
    Desk(monkeypatch, windows=[word_window(300, 'person@example.com plan'), word_window(301)])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side'})
    assert 'person@example.com' not in result.reply_text


def test_registry_retries_cannot_relaunch_after_a_partial_side_effect(monkeypatch):
    from jarvis.tools import registry
    desk = Desk(monkeypatch, appear=[(0, word_window(300))], fail_placement='rejected')
    cfg = placement_config()
    original = dict(registry.BUILTIN_TOOLS)
    try:
        registry.configure_windows_tools(cfg, platform='win32', start_index=False)
        result = registry.run_tool_with_retries(
            None, cfg, 'appControl', {'action': 'open', 'target': 'Word', 'monitor': 'side'}, '', '', '',
            max_retries=3)
        assert not result.success and desk.launches == ['word.lnk']
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def test_open_without_placement_keeps_its_launch_accepted_meaning(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch)
    result = call(apps_tool, {'action': 'open', 'target': 'Word'})
    assert result.success and data(result) == {'action': 'open_requested', 'application': 'Microsoft Word'}
    # One listing notes the windows already open, so a follow-up can find the new one later;
    # nothing waits for the launched window to appear.
    assert desk.launches == ['word.lnk'] and desk.polls <= 1


# --- availability, schemas and routing -------------------------------------------------

def test_disabled_windows_tools_neither_launch_nor_move(monkeypatch):
    apps_tool, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome_window(101)])
    cfg = placement_config(windows_tools_enabled=False)
    assert not call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': 'side'}, cfg).success
    assert not call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'side'}, cfg).success
    assert not call(windows, {'action': 'displays', 'target': ''}, cfg).success
    assert desk.launches == [] and desk.placed == {} and desk.polls == 0


def test_placement_schemas_extend_only_the_relevant_tools():
    from jarvis.tools.builtin.windows import AppControlTool, OpenPathTool, WindowControlTool
    for tool in (AppControlTool(), WindowControlTool()):
        schema = tool.inputSchema
        assert {'monitor', 'zone', 'state'} <= schema['properties'].keys()
        assert schema['required'] == ['action', 'target'] and schema['additionalProperties'] is False
        assert schema['properties']['state']['enum'] == ['restore', 'maximise']
    assert {'open', 'close', 'focus', 'list'} == set(AppControlTool.actions)
    assert {'minimise', 'maximise', 'restore', 'list', 'displays', 'place', 'desktops', 'desktop_switch',
            'desktop_new', 'desktop_close', 'move_to_desktop'} == set(WindowControlTool.actions)
    assert OpenPathTool().inputSchema['properties'].keys() == {'target', 'monitor', 'zone', 'state'}
    assert OpenPathTool().inputSchema['required'] == ['target']


@pytest.mark.parametrize('text', [
    'Open Word on my left monitor', 'Open Word on the second monitor and maximise it',
    'Maximise Word on the other screen', 'Put Chrome on the right half of the left monitor',
    'Open Word in the left zone',
])
def test_compound_open_and_place_requests_fall_through_to_the_model(text):
    from jarvis.fastpath.matcher import FastTarget, match
    known = (FastTarget(('word', 'microsoft word'), 'Microsoft Word', 'winword', 'Word'),
             FastTarget(('chrome',), 'Google Chrome', 'chrome', 'Chrome'))
    available = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'systemInfo', 'openPath'}
    assert match(text, 'en', targets=known, available_tools=available) is None

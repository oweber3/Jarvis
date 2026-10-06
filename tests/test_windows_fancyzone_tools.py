"""FancyZones zones and natural monitor references through appControl and windowControl.

The desktop (monitors, windows, launching and the placement call) is replaced by ``Desk``;
FancyZones data is the anonymised fixture folder. Geometry has its own tests in test_fancyzones.py.
"""
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis.platform.windows import displays, fancyzones
from jarvis.platform.windows.displays import Monitor

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / 'fixtures' / 'fancyzones'
DESKTOP = uuid.UUID('{00000000-0000-0000-0000-0000FACE0001}')
MAIN = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True,
               'TST1001', '5&00000001&0&UID4301', 'SN00000001')
PORTRAIT = Monitor(r'\\.\DISPLAY2', (-1080, -162, 0, 1758), (-1080, -162, 0, 1710), False,
                   'TST1002', '5&00000004&0&UID4304', 'SN00000002')
SMALL = Monitor(r'\\.\DISPLAY17', (2560, 0, 3360, 600), (2560, 0, 3360, 552), False)
WORD = ('Microsoft Word', 'word.lnk', 'C:/Office/WINWORD.EXE')

MAIN_RIGHT = [1280, 0, 2560, 1392]
PORTRAIT_BOTTOM = [-1080, 774, 0, 1710]
PORTRAIT_TOP = [-1080, -162, 0, 774]


def config(**changes):
    from jarvis.config import load_settings
    values = dict(windows_monitor_aliases={}, windows_window_zones={}, windows_app_aliases={})
    values.update(changes)
    return replace(load_settings(), **values)


class Desk:
    """A fake desktop with FancyZones fixture data for the current virtual desktop."""

    def __init__(self, monkeypatch, windows=(), appear=(), fancyzones_folder=FIXTURES):
        from jarvis.platform.windows import apps, windows_mgmt as wm
        self.windows, self.appear = list(windows), list(appear)
        self.launches, self.placed, self.polls, self.reads = [], {}, 0, 0
        monkeypatch.setattr(displays, 'list_monitors', lambda: [MAIN, PORTRAIT, SMALL])
        monkeypatch.setattr(displays, 'fancyzones_directory', lambda: fancyzones_folder)
        monkeypatch.setattr(displays, 'current_virtual_desktop', lambda: DESKTOP)
        original = fancyzones.load_data

        def counting(directory):
            self.reads += 1
            return original(directory)

        monkeypatch.setattr(fancyzones, 'load_data', counting)
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
        landed = list(monitor.work_area if state == 'maximise' else rectangle or (10, 10, 810, 610))
        self.placed[hwnd] = {'monitor': monitor.device, 'rectangle': landed, 'state': state}
        return {'hwnd': hwnd, 'monitor': monitor.device, 'rectangle': landed, 'state': state}


def call(tool, args, cfg=None):
    from jarvis.tools.base import ToolContext
    return tool.run(args, ToolContext(None, cfg or config(), '', '', '', 1, lambda text: None))


def data(result):
    return json.loads(result.reply_text)


def chrome(hwnd=101):
    from jarvis.platform.windows.windows_mgmt import Window
    return Window(hwnd, 'Docs', 'chrome', 100)


def word(hwnd=300):
    from jarvis.platform.windows.windows_mgmt import Window
    return Window(hwnd, 'Document', 'WINWORD', 4300)


def tools():
    from jarvis.tools.builtin.windows import AppControlTool, WindowControlTool
    return AppControlTool(), WindowControlTool()


# --- discovery -----------------------------------------------------------------------

def test_displays_list_fancyzones_zones_with_names_and_numbers(monkeypatch):
    _, windows = tools()
    Desk(monkeypatch)
    result = call(windows, {'action': 'displays', 'target': ''})
    assert result.success
    entries = {entry['device']: entry for entry in data(result)['displays']}
    assert [entries[d]['number'] for d in (MAIN.device, PORTRAIT.device, SMALL.device)] == [1, 2, 3]
    assert entries[MAIN.device]['fancyzones'] == {'layout': 'columns', 'zones': [
        {'names': ['1', 'left'], 'rectangle': [0, 0, 1280, 1392]},
        {'names': ['2', 'right'], 'rectangle': MAIN_RIGHT}]}
    assert entries[PORTRAIT.device]['fancyzones']['zones'][1] == {'names': ['2', 'bottom'],
                                                                 'rectangle': PORTRAIT_BOTTOM}
    assert [z['names'] for z in entries[SMALL.device]['fancyzones']['zones']] == [
        ['1', 'left'], ['2', 'middle'], ['3', 'right']]
    assert entries[MAIN.device]['zones'] == {}  # configured zones are listed separately and unchanged
    assert 'TST1001' not in result.reply_text and 'SN0000' not in result.reply_text  # identities stay local


def test_displays_without_powertoys_are_listed_as_before(monkeypatch):
    _, windows = tools()
    Desk(monkeypatch, fancyzones_folder=None)
    entries = data(call(windows, {'action': 'displays', 'target': ''}))['displays']
    assert len(entries) == 3 and all('fancyzones' not in entry for entry in entries)


def test_fancyzones_can_be_switched_off(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch)
    cfg = config(windows_fancyzones_enabled=False)
    entries = data(call(windows, {'action': 'displays', 'target': ''}, cfg))['displays']
    assert all('fancyzones' not in entry for entry in entries)
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '1', 'zone': 'left'}, cfg)
    assert not result.success and desk.reads == 0


# --- windowControl place -------------------------------------------------------------

@pytest.mark.parametrize('monitor, zone, device, rectangle, label', [
    ('1', 'right', MAIN.device, MAIN_RIGHT, 'right'),
    ('2', 'bottom', PORTRAIT.device, PORTRAIT_BOTTOM, 'bottom'),
    ('2', '1', PORTRAIT.device, PORTRAIT_TOP, '1'),
    ('primary', '2', MAIN.device, MAIN_RIGHT, '2'),
    ('left', 'top', PORTRAIT.device, PORTRAIT_TOP, 'top'),      # left by physical position
    ('right', 'middle', SMALL.device, [2768, 16, 3152, 536], 'middle'),
    ('3', '3', SMALL.device, [3168, 16, 3344, 536], '3'),
    (r'\\.\display2', 'BOTTOM', PORTRAIT.device, PORTRAIT_BOTTOM, 'bottom'),
])
def test_place_into_a_fancyzones_zone_on_a_referenced_monitor(monkeypatch, monitor, zone, device, rectangle, label):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome()])
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': monitor, 'zone': zone})
    assert result.success
    assert data(result) == {'action': 'placed', 'hwnd': 101, 'process': 'chrome', 'pid': 100, 'monitor': device,
                            'zone': label, 'rectangle': rectangle, 'state': 'restore'}
    assert desk.placed[101]['rectangle'] == rectangle and desk.placed[101]['monitor'] == device


def test_configured_zones_and_aliases_take_precedence(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome()])
    cfg = config(windows_monitor_aliases={'left': MAIN.device},
                 windows_window_zones={MAIN.device: {'left': [0, 0, 0.25, 1], 'dock': [0.75, 0, 0.25, 1]}})
    # "left" is the user's own alias for the main display, and "left" the user's own zone name.
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'left', 'zone': 'left'}, cfg)
    assert result.success and data(result)['rectangle'] == [0, 0, 640, 1392] and data(result)['monitor'] == MAIN.device
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'left', 'zone': 'dock'}, cfg)
    assert data(result)['rectangle'] == [1920, 0, 2560, 1392]
    # FancyZones' own names stay reachable on the same display.
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'left', 'zone': '2'}, cfg)
    assert data(result)['rectangle'] == MAIN_RIGHT and desk.placed[101]['rectangle'] == MAIN_RIGHT


def test_place_without_a_zone_does_not_read_powertoys(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome()])
    assert call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '2'}).success
    assert call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '2', 'state': 'maximise'}).success
    assert desk.reads == 0


@pytest.mark.parametrize('args', [
    {'monitor': '2', 'zone': 'left'},            # a portrait display has top and bottom, not left
    {'monitor': '2', 'zone': '3'},
    {'monitor': '2', 'zone': '0'},
    {'monitor': '4', 'zone': '1'},
    {'monitor': 'centre', 'zone': '1'},
    {'monitor': 'second', 'zone': '1'},
])
def test_unknown_zones_and_monitors_move_nothing(monkeypatch, args):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome()])
    result = call(windows, {'action': 'place', 'target': 'chrome', **args})
    assert not result.success and desk.placed == {}


def test_an_unknown_zone_names_what_is_available(monkeypatch):
    _, windows = tools()
    Desk(monkeypatch, windows=[chrome()])
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '2', 'zone': 'left'})
    assert 'Available zones: 1, top, 2, bottom' in result.error_message


def test_without_powertoys_only_configured_zones_exist(monkeypatch):
    _, windows = tools()
    desk = Desk(monkeypatch, windows=[chrome()], fancyzones_folder=None)
    cfg = config(windows_window_zones={MAIN.device: {'half': [0, 0, 0.5, 1]}})
    assert call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'primary', 'zone': 'half'}, cfg).success
    result = call(windows, {'action': 'place', 'target': 'chrome', 'monitor': 'primary', 'zone': '1'}, cfg)
    assert not result.success and 'Available zones: half' in result.error_message
    assert list(desk.placed) == [101]


def test_unreadable_powertoys_files_never_block_placement(monkeypatch, tmp_path):
    _, windows = tools()
    (tmp_path / 'applied-layouts.json').write_text('{ broken', encoding='utf-8')
    desk = Desk(monkeypatch, windows=[chrome()], fancyzones_folder=tmp_path)
    cfg = config(windows_window_zones={MAIN.device: {'half': [0, 0, 0.5, 1]}})
    assert call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '1', 'zone': 'half'}, cfg).success
    assert call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '1', 'state': 'maximise'}).success
    assert not call(windows, {'action': 'place', 'target': 'chrome', 'monitor': '1', 'zone': '1'}, cfg).success
    assert desk.placed[101]['state'] == 'maximise'


# --- appControl open ---------------------------------------------------------------------

def test_open_into_a_fancyzones_zone_launches_once_and_reports_the_rectangle(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, appear=[(3, word())])
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': '2', 'zone': 'bottom'})
    assert result.success
    assert data(result) == {'action': 'opened_and_placed', 'application': 'Microsoft Word', 'hwnd': 300,
                            'process': 'WINWORD', 'pid': 4300, 'monitor': PORTRAIT.device, 'zone': 'bottom',
                            'rectangle': PORTRAIT_BOTTOM, 'state': 'restore', 'reused_window': False}
    assert desk.launches == ['word.lnk'] and list(desk.placed) == [300]


@pytest.mark.parametrize('args', [
    {'monitor': '2', 'zone': 'left'},
    {'monitor': '9', 'zone': '1'},
    {'monitor': '2', 'zone': '7'},
    {'monitor': '2', 'zone': 'LEFT'},
    {'monitor': '2', 'zone': '1', 'state': 'maximise'},
])
def test_open_validates_the_destination_before_launching(monkeypatch, args):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, appear=[(0, word())])
    assert not call(apps_tool, {'action': 'open', 'target': 'Word', **args}).success
    assert desk.launches == [] and desk.placed == {}


def test_a_failed_placement_is_never_relaunched_and_reports_the_launch(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, appear=[(0, word())])

    def refuse(*_args, **_kwargs):
        raise OSError('The application did not reach the requested position.')

    monkeypatch.setattr(wm, 'place_window', refuse)
    result = call(apps_tool, {'action': 'open', 'target': 'Word', 'monitor': '2', 'zone': '1'})
    report = data(result)
    assert not result.success and desk.launches == ['word.lnk']
    assert (report['launch'], report['placement'], report['zone']) == ('accepted', 'failed', '1')
    assert report['monitor'] == PORTRAIT.device


def test_schema_describes_references_briefly_and_keeps_the_top_level_description():
    from jarvis.tools.builtin.windows import AppControlTool, WindowControlTool
    for tool in (AppControlTool(), WindowControlTool()):
        monitor = tool.inputSchema['properties']['monitor']['description']
        zone = tool.inputSchema['properties']['zone']['description']
        assert all(word in monitor for word in ('"2"', 'primary', 'left', 'right', 'windowControl displays'))
        assert 'name or number' in zone and len(monitor) <= 260 and len(zone) <= 260
    # Placement vocabulary stays out of the top-level description; the focus and switch wording that
    # routes those requests to appControl is kept.
    assert 'Switch to/focus Spotify or another running app. Close apps; list windows.' in AppControlTool.description
    assert not any(word in AppControlTool.description.casefold() for word in ('monitor', 'zone', 'display'))


def test_placement_on_the_wrong_action_points_to_the_right_one(monkeypatch):
    apps_tool, _ = tools()
    desk = Desk(monkeypatch, windows=[chrome()])
    result = call(apps_tool, {'action': 'focus', 'target': 'chrome', 'monitor': '2'})
    assert not result.success and desk.placed == {} and desk.launches == []
    assert 'windowControl place' in result.error_message and 'appControl open' in result.error_message

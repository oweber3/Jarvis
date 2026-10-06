"""Behaviour tests for openWebsite: one web address, optionally in a new placed browser window.

The OS boundary (displays, window listing, launching, placement) is replaced by ``Desk``. Addresses
are placeholders; the tests assert outcomes (what opened, where it landed, what was reported).
"""
import json
from dataclasses import replace

import pytest

from jarvis.platform.windows import apps, displays, windows_mgmt as wm, websites, workspaces
from jarvis.platform.windows.displays import Monitor, zone_rectangle
from jarvis.platform.windows.windows_mgmt import Window

PRIMARY = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
SIDE = Monitor(r'\\.\DISPLAY2', (2560, 0, 4480, 1080), (2560, 0, 4480, 1040), False)
ZONES = {PRIMARY.device: {'left': [0, 0, 0.5, 1], 'right': [0.5, 0, 0.5, 1]},
         SIDE.device: {'top': [0, 0, 1, 0.5]}}
CHROME = workspaces.Browser('chrome', 'chrome', r'C:\Fake\chrome.exe')
EDGE = workspaces.Browser('edge', 'msedge', r'C:\Fake\msedge.exe')
SECRET = 'https://private.example.test/notes?token=abc'


class Desk:
    """A fake desktop: windows that appear shortly after each browser launch, recorded placements."""

    def __init__(self, monkeypatch, *, existing=(), appear=(), fail_placement=None):
        self.windows = list(existing)
        self.appear = list(appear)  # one list of windows per browser launch
        self.revealed, self.launches, self.startfiles, self.placed, self.polls = [], [], [], {}, 0
        self.fail_placement = fail_placement
        self.logs = []
        monkeypatch.setattr(workspaces, 'find_browser', lambda name=None: EDGE if name == 'edge' else CHROME)
        monkeypatch.setattr(workspaces, '_spawn', self.spawn)
        monkeypatch.setattr(websites.os, 'startfile', self.startfiles.append)
        for module in (websites, workspaces):
            monkeypatch.setattr(module, 'debug_log', lambda message, *a, **k: self.logs.append(message))
        monkeypatch.setattr(displays, 'list_monitors', lambda: [PRIMARY, SIDE])
        monkeypatch.setattr(wm, 'list_windows', self.list_windows)
        monkeypatch.setattr(wm, 'place_window', self.place_window)
        for name, value in {'_POLL_SEC': .01, '_STABLE_SEC': .05, 'BROWSER_WINDOW_WAIT_SEC': .6}.items():
            monkeypatch.setattr(workspaces, name, value)
        monkeypatch.setattr(websites, 'PLACE_BUDGET_SEC', 1.5)

    def spawn(self, command):
        self.launches.append(command)
        if self.appear:
            self.revealed.append((self.polls + 2, self.appear.pop(0)))

    def list_windows(self):
        self.polls += 1
        return self.windows + [window for at, group in self.revealed if self.polls >= at for window in group]

    def place_window(self, hwnd, monitor, rectangle=None, state='restore', *, deadline=None):
        if self.fail_placement:
            raise OSError(self.fail_placement)
        landed = list(monitor.work_area if state == 'maximise' else rectangle or (10, 10, 810, 610))
        self.placed[hwnd] = {'monitor': monitor.device, 'rectangle': landed, 'state': state}
        return {'hwnd': hwnd, 'monitor': monitor.device, 'rectangle': landed, 'state': state}


def chrome_window(hwnd):
    return Window(hwnd, 'New Tab', 'chrome', 100)


def config(**changes):
    from jarvis.config import load_settings
    values = dict(windows_monitor_aliases={'side': SIDE.device}, windows_window_zones=ZONES,
                  windows_fancyzones_enabled=False, windows_app_aliases={})
    values.update(changes)
    return replace(load_settings(), **values)


def call(args, cfg=None):
    from jarvis.tools.base import ToolContext
    from jarvis.tools.builtin.windows import OpenWebsiteTool
    return OpenWebsiteTool().run(args, ToolContext(None, cfg or config(), '', '', '', 1, lambda text: None))


def data(result):
    return json.loads(result.reply_text)


@pytest.fixture(autouse=True)
def fresh_referents():
    from jarvis.memory.desktop_referents import get_desktop_referents
    get_desktop_referents().clear()
    yield
    get_desktop_referents().clear()


# --- addresses -----------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('https://www.youtube.com/', 'https://www.youtube.com/'),
    ('http://example.com/a?b=1', 'http://example.com/a?b=1'),
    ('youtube.com', 'https://youtube.com'),
    ('  www.youtube.com/feed/subscriptions ', 'https://www.youtube.com/feed/subscriptions'),
    ('HTTPS://Example.com', 'HTTPS://Example.com'),
])
def test_web_addresses_and_bare_hosts_are_accepted(text, expected):
    assert websites.normalise_url(text) == expected


@pytest.mark.parametrize('text', [
    '', '   ', 'javascript:alert(1)', 'data:text/html,hi', 'file:///C:/secret.txt', 'ftp://example.com',
    'mailto:someone@example.com', r'C:\Users\me\notes.pdf', 'youtube', 'you tube.com', 'https://',
    'user@example.com', 'example.com:8080', r'\\server\share', 'https://exa mple.com',
])
def test_anything_but_a_web_address_launches_nothing(text, monkeypatch):
    desk = Desk(monkeypatch)
    result = call({'url': text})
    assert not result.success
    assert desk.launches == [] and desk.startfiles == []


# --- without placement ---------------------------------------------------------------

def test_a_plain_request_opens_in_the_default_browser(monkeypatch):
    desk = Desk(monkeypatch)
    result = call({'url': 'youtube.com'})
    assert result.success
    assert desk.startfiles == ['https://youtube.com'] and desk.launches == []
    assert data(result) == {'action': 'website_opened', 'browser': 'default'}


def test_a_named_browser_is_started_once_with_the_address(monkeypatch):
    desk = Desk(monkeypatch)
    result = call({'url': 'https://example.com', 'browser': 'edge'})
    assert result.success and data(result) == {'action': 'website_opened', 'browser': 'edge'}
    assert desk.launches == [[EDGE.executable, 'https://example.com']] and desk.startfiles == []


# --- with placement ------------------------------------------------------------------

def test_a_zone_alone_opens_a_new_window_placed_on_the_main_display(monkeypatch):
    desk = Desk(monkeypatch, existing=[chrome_window(100)], appear=[[chrome_window(200)]])
    result = call({'url': 'youtube.com', 'zone': 'right'})
    assert result.success
    expected = list(zone_rectangle(PRIMARY, [0.5, 0, 0.5, 1]))
    assert data(result) == {'action': 'website_opened_and_placed', 'browser': 'chrome', 'hwnd': 200,
                            'process': 'chrome', 'monitor': PRIMARY.device, 'zone': 'right',
                            'rectangle': expected, 'state': 'restore'}
    assert desk.launches == [[CHROME.executable, '--new-window', 'https://youtube.com']]
    assert desk.placed == {200: {'monitor': PRIMARY.device, 'rectangle': expected, 'state': 'restore'}}


def test_a_named_display_and_maximise_are_honoured(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(201)]])
    result = call({'url': 'example.com', 'monitor': 'side', 'state': 'maximise'})
    assert result.success and data(result)['monitor'] == SIDE.device
    assert desk.placed[201] == {'monitor': SIDE.device, 'rectangle': list(SIDE.work_area), 'state': 'maximise'}


@pytest.mark.parametrize('args', [
    {'url': 'example.com', 'zone': 'nowhere'},
    {'url': 'example.com', 'monitor': 'third'},
    {'url': 'example.com', 'zone': 'right', 'state': 'maximise'},
    {'url': 'example.com', 'state': 'tiny'},
    {'url': 'example.com', 'monitor': 2},
    {'url': 'example.com', 'browser': 'netscape', 'zone': 'right'},
    {'url': 'example.com', 'target': 'chrome'},
])
def test_an_invalid_destination_or_argument_launches_nothing(args, monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(200)]])
    if args.get('browser') == 'netscape':
        def unknown(name=None):
            raise ValueError('Unknown browser.')
        monkeypatch.setattr(workspaces, 'find_browser', unknown)
    assert not call(args).success
    assert desk.launches == [] and desk.startfiles == [] and desk.placed == {}


def test_several_new_windows_are_never_chosen_between_and_never_relaunched(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(200), chrome_window(201)]])
    result = call({'url': 'example.com', 'zone': 'left'})
    assert not result.success
    outcome = data(result)
    assert outcome['launch'] == 'accepted' and outcome['placement'] == 'ambiguous'
    assert outcome['monitor'] == PRIMARY.device and outcome['zone'] == 'left'
    assert len(desk.launches) == 1 and desk.placed == {}


def test_a_window_that_never_appears_is_reported_unverified(monkeypatch):
    desk = Desk(monkeypatch)
    result = call({'url': 'example.com', 'zone': 'left'})
    assert not result.success
    assert data(result)['placement'] == 'unverified' and len(desk.launches) == 1


def test_a_placement_failure_is_partial_and_names_the_window(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(200)]], fail_placement='Window did not move.')
    result = call({'url': 'example.com', 'zone': 'left'})
    assert not result.success
    outcome = data(result)
    assert outcome['launch'] == 'accepted' and outcome['placement'] == 'failed' and outcome['hwnd'] == 200
    assert len(desk.launches) == 1


def test_a_website_does_not_launch_while_a_workspace_is_opening(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(200)]])
    assert workspaces._launch_lock.acquire(blocking=False)
    try:
        result = call({'url': 'example.com', 'zone': 'left'})
    finally:
        workspaces._launch_lock.release()
    assert not result.success and desk.launches == []
    assert call({'url': 'example.com', 'zone': 'left'}).success


def test_results_and_logs_never_carry_the_address(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(200)], [chrome_window(300), chrome_window(301)]])
    texts = [call({'url': SECRET, 'zone': 'left'}), call({'url': SECRET, 'zone': 'right'}), call({'url': SECRET})]
    for result in texts:
        assert 'private.example' not in (result.reply_text or '') + (result.error_message or '')
    assert not any('private.example' in line for line in desk.logs)


def test_a_placed_website_window_is_remembered_for_follow_ups(monkeypatch):
    from jarvis.memory.desktop_referents import get_desktop_referents
    Desk(monkeypatch, appear=[[chrome_window(200)]])
    assert call({'url': 'youtube.com', 'zone': 'right'}).success
    [entry] = get_desktop_referents().recent(60)
    assert (entry.application, entry.hwnd, entry.monitor, entry.zone, entry.last_action) == (
        'chrome', 200, PRIMARY.device, 'right', 'open')


# --- the tool surface ----------------------------------------------------------------

def test_schema_offers_one_address_a_browser_and_the_placement_fields():
    from jarvis.tools.builtin.windows import OpenWebsiteTool
    schema = OpenWebsiteTool().inputSchema
    assert schema['required'] == ['url']
    assert set(schema['properties']) == {'url', 'browser', 'monitor', 'zone', 'state'}
    assert schema['additionalProperties'] is False


def test_the_tool_is_registered_with_the_other_windows_tools():
    from jarvis.tools.registry import BUILTIN_TOOLS
    assert 'openWebsite' in BUILTIN_TOOLS


def test_the_tool_is_a_routine_action_needing_no_confirmation():
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    assert evaluate_safety('openWebsite', {'url': 'youtube.com', 'zone': 'right'}, config()).tier == SafetyTier.SAFE

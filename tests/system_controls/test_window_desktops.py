"""windowControl virtual desktop actions, with the desktop layer and window list replaced."""
import json
from types import SimpleNamespace

import pytest

from jarvis.platform.windows import virtual_desktops as vd, windows_mgmt as wm
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.windows import WindowControlTool


class FakeDesktops:
    def __init__(self):
        self.log = []
        self.count, self.current = 3, 2

    def state(self):
        return vd.DesktopState(self.count, self.current)

    def switch(self, target):
        self.log.append(('switch', target))
        self.current = {'next': 3, 'previous': 1}.get(target) or int(target)
        return self.state()

    def create(self):
        self.log.append(('create',))
        self.count += 1
        self.current = self.count
        return self.state()

    def close(self, target=''):
        self.log.append(('close', target))
        self.count -= 1
        return self.state()

    def move_window(self, hwnd, target):
        self.log.append(('move', hwnd, target))
        return {'hwnd': hwnd, 'desktop': int(target)}


@pytest.fixture
def fake(monkeypatch):
    desktops = FakeDesktops()
    for name in ('state', 'switch', 'create', 'close', 'move_window'):
        monkeypatch.setattr(vd, name, getattr(desktops, name))
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Notes - Notepad', 'notepad', 9),
                                                     wm.Window(202, 'Music', 'Spotify', 8)])
    return desktops


def call(args):
    cfg = SimpleNamespace(windows_tools_enabled=True, windows_app_aliases={}, windows_monitor_aliases={},
                          windows_window_zones={}, windows_fancyzones_enabled=False)
    result = WindowControlTool().run(args, ToolContext(None, cfg, '', '', '', 1, lambda text: None))
    return result


def test_schema_offers_a_desktop_argument_and_the_new_actions():
    schema = WindowControlTool().inputSchema
    assert {'desktops', 'desktop_switch', 'desktop_new', 'desktop_close', 'move_to_desktop'} <= set(
        schema['properties']['action']['enum'])
    assert 'desktop' in schema['properties']
    assert schema['required'] == ['action', 'target']


def test_desktops_lists_the_count_and_current_desktop(fake):
    result = call({'action': 'desktops', 'target': ''})
    assert result.success and json.loads(result.reply_text) == {'count': 3, 'current': 2}


@pytest.mark.parametrize('desktop', ['2', 'next', 'previous'])
def test_desktop_switch_takes_a_number_or_direction(fake, desktop):
    result = call({'action': 'desktop_switch', 'target': '', 'desktop': desktop})
    assert result.success and fake.log == [('switch', desktop)]
    body = json.loads(result.reply_text)
    assert body['action'] == 'desktop_switched' and body['count'] == 3


def test_desktop_switch_requires_a_desktop(fake):
    result = call({'action': 'desktop_switch', 'target': ''})
    assert result.success is False and fake.log == []


def test_desktop_errors_are_reported(monkeypatch, fake):
    def nowhere(target):
        raise ValueError('There is no such desktop. Desktops are numbered 1 to 3; you are on 2.')
    monkeypatch.setattr(vd, 'switch', nowhere)
    result = call({'action': 'desktop_switch', 'target': '', 'desktop': '9'})
    assert result.success is False and 'numbered 1 to 3' in result.error_message


def test_an_unavailable_desktop_service_is_a_failure_not_a_crash(monkeypatch, fake):
    def down():
        raise vd.VirtualDesktopError('Virtual desktop control is not available on this system.')
    monkeypatch.setattr(vd, 'state', down)
    result = call({'action': 'desktops', 'target': ''})
    assert result.success is False and 'not available' in result.error_message


def test_desktop_new_creates_and_reports_the_new_desktop(fake):
    result = call({'action': 'desktop_new', 'target': ''})
    assert fake.log == [('create',)]
    assert json.loads(result.reply_text) == {'action': 'desktop_created', 'count': 4, 'current': 4}


def test_desktop_close_closes_the_named_or_current_desktop(fake):
    call({'action': 'desktop_close', 'target': '', 'desktop': '3'})
    call({'action': 'desktop_close', 'target': ''})
    assert fake.log == [('close', '3'), ('close', '')]


def test_move_to_desktop_resolves_the_window_then_moves_it(fake):
    result = call({'action': 'move_to_desktop', 'target': 'notepad', 'desktop': '3'})
    assert result.success and fake.log == [('move', 101, '3')]
    assert json.loads(result.reply_text) == {'action': 'moved_to_desktop', 'hwnd': 101, 'process': 'notepad',
                                             'desktop': 3}


def test_move_to_desktop_needs_both_a_window_and_a_desktop(fake):
    assert call({'action': 'move_to_desktop', 'target': 'notepad'}).success is False
    assert call({'action': 'move_to_desktop', 'target': '', 'desktop': '2'}).success is False
    assert fake.log == []


def test_move_to_desktop_does_not_guess_between_windows(fake, monkeypatch):
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(1, 'A', 'notepad', 1), wm.Window(2, 'B', 'notepad', 2)])
    result = call({'action': 'move_to_desktop', 'target': 'notepad', 'desktop': '2'})
    assert result.success is False and fake.log == []


def test_desktop_argument_is_rejected_on_other_actions(fake):
    result = call({'action': 'minimise', 'target': 'notepad', 'desktop': '2'})
    assert result.success is False


def test_moving_a_window_leaves_a_desktop_referent(fake):
    from jarvis.memory.desktop_referents import get_desktop_referents
    get_desktop_referents().clear()
    call({'action': 'move_to_desktop', 'target': 'notepad', 'desktop': '3'})
    recent = get_desktop_referents().recent(60)
    assert [(r.hwnd, r.process, r.last_action) for r in recent] == [(101, 'notepad', 'move_desktop')]

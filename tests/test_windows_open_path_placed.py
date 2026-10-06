"""openPath with placement: the item opens once and the window showing it lands on the display and zone.

The OS boundary (association lookup, shell open, browser launch, window listing, placement) is replaced by
``Desk``; paths are temporary files. Tests assert outcomes: what opened, which window moved, what was said.
"""
import json
from dataclasses import replace

import pytest

from jarvis.platform.windows import apps, displays, files, windows_mgmt as wm, workspaces
from jarvis.platform.windows.displays import Monitor, zone_rectangle
from jarvis.platform.windows.windows_mgmt import Window

PRIMARY = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
SIDE = Monitor(r'\\.\DISPLAY2', (2560, 0, 4480, 1080), (2560, 0, 4480, 1040), False)
ZONES = {PRIMARY.device: {'left': [0, 0, 0.5, 1], 'right': [0.5, 0, 0.5, 1]}}
WORD = r'C:\Office\WINWORD.EXE'
CHROME = workspaces.Browser('chrome', 'chrome', r'C:\Chrome\chrome.exe')


class Desk:
    """Windows that appear after an open, the program each extension opens with, recorded placements."""

    def __init__(self, monkeypatch, *, programs=None, existing=(), appear=(), fail_placement=None):
        self.programs = programs or {}
        self.windows = list(existing)
        self.appear = list(appear)  # windows revealed a few listings after the open
        self.opened, self.spawned, self.placed, self.polls, self.revealed_at = [], [], {}, 0, None
        self.fail_placement = fail_placement
        monkeypatch.setattr(files, '_shell_open', self.shell_open)
        monkeypatch.setattr(files, 'association_executable', self.association)
        monkeypatch.setattr(workspaces, '_spawn', self.spawn)
        monkeypatch.setattr(workspaces, 'find_browser', lambda name=None: CHROME)
        monkeypatch.setattr(displays, 'list_monitors', lambda: [PRIMARY, SIDE])
        monkeypatch.setattr(wm, 'list_windows', self.list_windows)
        monkeypatch.setattr(wm, 'place_window', self.place_window)
        for module, name, value in [(apps, '_POLL_SEC', .01), (apps, '_STABLE_SEC', .05),
                                    (apps, '_REUSE_GRACE_SEC', .2), (workspaces, '_POLL_SEC', .01),
                                    (workspaces, '_STABLE_SEC', .05), (workspaces, 'BROWSER_WINDOW_WAIT_SEC', .6),
                                    (files, 'PLACE_BUDGET_SEC', 1.5)]:
            monkeypatch.setattr(module, name, value)

    def association(self, path):
        return self.programs.get('folder' if path.is_dir() else path.suffix.casefold())

    def shell_open(self, target):
        self.opened.append(target)
        self.revealed_at = self.polls + 2

    def spawn(self, command):
        self.spawned.append(command)
        self.revealed_at = self.polls + 2

    def list_windows(self):
        self.polls += 1
        shown = self.appear if self.revealed_at is not None and self.polls >= self.revealed_at else []
        return self.windows + list(shown)

    def place_window(self, hwnd, monitor, rectangle=None, state='restore', *, deadline=None):
        if self.fail_placement:
            raise OSError(self.fail_placement)
        landed = list(monitor.work_area if state == 'maximise' else rectangle or (10, 10, 810, 610))
        self.placed[hwnd] = {'monitor': monitor.device, 'rectangle': landed, 'state': state}
        return {'hwnd': hwnd, 'monitor': monitor.device, 'rectangle': landed, 'state': state}


def config(**changes):
    from jarvis.config import load_settings
    values = dict(windows_monitor_aliases={'side': SIDE.device}, windows_window_zones=ZONES,
                  windows_fancyzones_enabled=False, windows_app_aliases={})
    values.update(changes)
    return replace(load_settings(), **values)


def call(args):
    from jarvis.tools.base import ToolContext
    from jarvis.tools.builtin.windows import OpenPathTool
    return OpenPathTool().run(args, ToolContext(None, config(), '', '', '', 1, lambda text: None))


def data(result):
    return json.loads(result.reply_text)


@pytest.fixture
def report(tmp_path):
    path = tmp_path / 'Proposal draft.docx'
    path.write_bytes(b'PK')
    return path


@pytest.fixture
def textbook(tmp_path):
    path = tmp_path / 'Handbook.pdf'
    path.write_bytes(b'%PDF-1.4')
    return path


@pytest.fixture(autouse=True)
def fresh_referents():
    from jarvis.memory.desktop_referents import get_desktop_referents
    get_desktop_referents().clear()
    yield
    get_desktop_referents().clear()


def test_a_document_opens_once_and_its_new_window_lands_in_the_zone_of_the_main_display(monkeypatch, report):
    desk = Desk(monkeypatch, programs={'.docx': WORD}, existing=[Window(1, 'Other', 'chrome', 10)],
                appear=[Window(300, 'Proposal draft.docx - Word', 'WINWORD', 30)])
    result = call({'target': str(report), 'zone': 'left'})
    assert result.success
    expected = list(zone_rectangle(PRIMARY, [0, 0, 0.5, 1]))
    outcome = data(result)
    assert outcome['action'] == 'opened_and_placed' and outcome['kind'] == 'file'
    assert (outcome['hwnd'], outcome['monitor'], outcome['zone'], outcome['rectangle']) == (
        300, PRIMARY.device, 'left', expected)
    assert outcome['application'] == 'WINWORD' and outcome['reused_window'] is False
    assert desk.opened == [str(report.resolve())] and list(desk.placed) == [300]


def test_a_pdf_that_a_browser_opens_gets_a_new_browser_window_instead_of_a_tab(monkeypatch, textbook):
    desk = Desk(monkeypatch, programs={'.pdf': CHROME.executable},
                existing=[Window(1, 'Inbox - Google Chrome', 'chrome', 10)],
                appear=[Window(400, 'Handbook - Google Chrome', 'chrome', 10)])
    result = call({'target': str(textbook), 'monitor': 'side', 'state': 'maximise'})
    assert result.success and data(result)['hwnd'] == 400
    assert desk.spawned == [[CHROME.executable, '--new-window', textbook.resolve().as_uri()]]
    assert desk.opened == [] and desk.placed[400]['monitor'] == SIDE.device and 1 not in desk.placed


def test_a_folder_opens_in_a_new_explorer_window_that_is_placed(monkeypatch, tmp_path):
    desk = Desk(monkeypatch, programs={'folder': r'C:\Windows\explorer.exe'},
                existing=[Window(2, 'Documents', 'explorer', 20)],
                appear=[Window(500, tmp_path.name, 'explorer', 20)])
    result = call({'target': str(tmp_path), 'zone': 'right'})
    assert result.success and data(result)['kind'] == 'folder'
    assert list(desk.placed) == [500]


def test_a_program_that_shows_the_file_in_its_one_existing_window_has_that_window_placed(monkeypatch, report):
    desk = Desk(monkeypatch, programs={'.docx': WORD}, existing=[Window(301, 'Word', 'WINWORD', 30)])
    result = call({'target': str(report), 'zone': 'right'})
    assert result.success and data(result)['reused_window'] is True and list(desk.placed) == [301]
    assert len(desk.opened) == 1


def test_several_existing_windows_and_no_new_one_are_never_chosen_between(monkeypatch, report):
    desk = Desk(monkeypatch, programs={'.docx': WORD},
                existing=[Window(301, 'A - Word', 'WINWORD', 30), Window(302, 'B - Word', 'WINWORD', 31)])
    result = call({'target': str(report), 'zone': 'right'})
    assert not result.success
    outcome = data(result)
    assert outcome['launch'] == 'accepted' and outcome['placement'] == 'ambiguous'
    assert desk.placed == {} and len(desk.opened) == 1


def test_a_program_known_only_as_a_packaged_app_is_found_as_the_one_new_window(monkeypatch, tmp_path):
    photo = tmp_path / 'holiday.png'
    photo.write_bytes(b'\x89PNG')
    desk = Desk(monkeypatch, programs={}, existing=[Window(1, 'Inbox - Google Chrome', 'chrome', 10)],
                appear=[Window(600, 'holiday.png - Photos', 'Photos', 60)])
    result = call({'target': str(photo), 'zone': 'left'})
    assert result.success and list(desk.placed) == [600]


@pytest.mark.parametrize('args', [
    {'zone': 'nowhere'}, {'monitor': 'third'}, {'zone': 'left', 'state': 'maximise'}])
def test_an_invalid_destination_opens_nothing(monkeypatch, report, args):
    desk = Desk(monkeypatch, programs={'.docx': WORD}, appear=[Window(300, 'Word', 'WINWORD', 30)])
    assert not call({'target': str(report), **args}).success
    assert desk.opened == [] and desk.spawned == [] and desk.placed == {}


def test_a_missing_file_opens_nothing(monkeypatch, tmp_path):
    desk = Desk(monkeypatch, programs={'.docx': WORD})
    assert not call({'target': str(tmp_path / 'missing.docx'), 'zone': 'left'}).success
    assert desk.opened == [] and desk.placed == {}


def test_the_placed_window_is_remembered_under_the_program_name_never_the_path(monkeypatch, report):
    from jarvis.memory.desktop_referents import get_desktop_referents
    Desk(monkeypatch, programs={'.docx': WORD}, appear=[Window(300, 'Proposal draft.docx - Word', 'WINWORD', 30)])
    assert call({'target': str(report), 'zone': 'left'}).success
    [entry] = get_desktop_referents().recent(60)
    assert entry.application == 'WINWORD' and entry.hwnd == 300 and str(report.parent) not in repr(entry)


def test_without_placement_the_item_still_just_opens(monkeypatch, report):
    desk = Desk(monkeypatch, programs={'.docx': WORD})
    result = call({'target': str(report)})
    assert result.success and data(result)['action'] == 'open_requested'
    assert desk.opened == [str(report.resolve())] and desk.placed == {}


def test_the_schema_offers_the_placement_fields():
    from jarvis.tools.builtin.windows import OpenPathTool
    schema = OpenPathTool().inputSchema
    assert {'target', 'monitor', 'zone', 'state'} <= set(schema['properties'])
    assert schema['required'] == ['target']


def test_a_program_restoring_several_windows_has_the_one_titled_with_the_file_placed(monkeypatch, tmp_path):
    notes = tmp_path / 'notes.txt'
    notes.write_text('x')
    notepad = r'C:\Apps\Notepad.exe'
    restored = [Window(700 + n, 'Untitled - Notepad', 'Notepad', 70) for n in range(3)]
    desk = Desk(monkeypatch, programs={'.txt': notepad},
                appear=[*restored, Window(710, 'notes.txt - Notepad', 'Notepad', 70)])
    result = call({'target': str(notes), 'zone': 'left'})
    assert result.success and list(desk.placed) == [710]


def test_a_title_without_the_extension_also_identifies_the_window(monkeypatch, report):
    desk = Desk(monkeypatch, programs={'.docx': WORD},
                appear=[Window(301, 'Document1 - Word', 'WINWORD', 30), Window(302, 'Proposal draft - Word', 'WINWORD', 30)])
    assert call({'target': str(report), 'zone': 'left'}).success and list(desk.placed) == [302]


def test_an_existing_window_that_now_shows_the_file_is_placed_without_waiting_out_the_deadline(monkeypatch, tmp_path):
    import time
    notes = tmp_path / 'notes.txt'
    notes.write_text('x')
    notepad = r'C:\Apps\Notepad.exe'
    # Notepad shows the file as a new tab of one of its existing windows; no new window appears.
    tabs = [Window(800, 'Untitled - Notepad', 'Notepad', 80), Window(801, 'Untitled - Notepad', 'Notepad', 80)]
    desk = Desk(monkeypatch, programs={'.txt': notepad}, existing=tabs)

    def shell_open(target):
        desk.opened.append(target)
        desk.windows[1] = Window(801, 'notes.txt - Notepad', 'Notepad', 80)

    monkeypatch.setattr(files, '_shell_open', shell_open)
    started = time.monotonic()
    result = call({'target': str(notes), 'zone': 'left'})
    assert result.success and list(desk.placed) == [801] and data(result)['reused_window'] is True
    assert time.monotonic() - started < 1.0  # the grace period, not the whole deadline

"""Behaviour tests for named windows workspaces (platform layer).

The OS boundary (displays, window listing, launching, placement) is replaced by ``Desk``.
Files and URLs are placeholders; the tests assert outcomes, never call sequences.
"""
import json
import threading

import pytest

from jarvis.platform.windows import apps, windows_mgmt as wm, workspaces
from jarvis.platform.windows.displays import Monitor, zone_rectangle
from jarvis.platform.windows.windows_mgmt import Window

PRIMARY = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
LEFT = Monitor(r'\\.\DISPLAY2', (-1920, -200, 0, 880), (-1920, -200, 0, 840), False)
ZONES = {PRIMARY.device: {'left': [0, 0, 0.5, 1], 'right': [0.5, 0, 0.5, 1]}}
CHROME = workspaces.Browser('chrome', 'chrome', r'C:\Fake\chrome.exe')
WORD = apps.Application('Microsoft Word', 'word.lnk', 'C:/Office/WINWORD.EXE')
SECRET_URL = 'https://private.example.test/notes?token=abc'


class Desk:
    """A fake desktop: monitors, windows revealed shortly after each launch, recorded placements."""

    def __init__(self, monkeypatch, *, existing=(), appear=(), applications=(WORD,), fail_placement=None,
                 spawn_error=None):
        self.windows = list(existing)
        self.appear = list(appear)  # one list of windows per browser launch, in order
        self.revealed, self.launches, self.startfiles, self.placed, self.polls = [], [], [], {}, 0
        self.fail_placement, self.spawn_error = fail_placement, spawn_error
        self.logs = []
        self.applications = list(applications)
        monkeypatch.setattr(workspaces, 'find_browser', lambda name=None: CHROME)
        monkeypatch.setattr(workspaces, '_spawn', self.spawn)
        monkeypatch.setattr(workspaces, 'debug_log', lambda message, *a, **k: self.logs.append(message))
        monkeypatch.setattr(apps, 'debug_log', lambda message, *a, **k: self.logs.append(message))
        monkeypatch.setattr(apps.APP_INDEX, 'applications', lambda: self.applications)
        monkeypatch.setattr(apps.os, 'startfile', self.startfile)
        monkeypatch.setattr(wm, 'list_windows', self.list_windows)
        monkeypatch.setattr(wm, 'place_window', self.place_window)
        for module, name, value in [(workspaces, '_POLL_SEC', .01), (workspaces, '_STABLE_SEC', .05),
                                    (workspaces, 'BROWSER_WINDOW_WAIT_SEC', .6), (apps, '_POLL_SEC', .01),
                                    (apps, '_STABLE_SEC', .05), (apps, '_REUSE_GRACE_SEC', .2)]:
            monkeypatch.setattr(module, name, value)

    def spawn(self, command):
        if self.spawn_error:
            raise self.spawn_error
        self.launches.append(command)
        if self.appear:
            self.revealed.append((self.polls + 2, self.appear.pop(0)))

    def startfile(self, target):
        self.startfiles.append(target)
        self.windows.append(Window(900, 'Document', 'WINWORD', 9000))

    def list_windows(self):
        self.polls += 1
        shown = [window for at, group in self.revealed if self.polls >= at for window in group]
        return self.windows + shown

    def place_window(self, hwnd, monitor, rectangle=None, state='restore', *, deadline=None):
        if self.fail_placement:
            raise OSError(self.fail_placement)
        landed = list(monitor.work_area if state == 'maximise' else rectangle or (10, 10, 810, 610))
        self.placed[hwnd] = {'monitor': monitor.device, 'rectangle': landed, 'state': state}
        return {'hwnd': hwnd, 'monitor': monitor.device, 'rectangle': landed, 'state': state}


def chrome_window(hwnd):
    return Window(hwnd, 'New Tab', 'chrome', 100)


def pdf(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b'%PDF-1.4')
    return path


def browser_item(urls, zone, label='', **extra):
    item = {'kind': 'browser_window', 'urls': [str(u) for u in urls], 'monitor': 'primary', 'zone': zone, **extra}
    return {**item, **({'label': label} if label else {})}


def environment(desk, fancy=None, **overrides):
    values = dict(monitors=[PRIMARY, LEFT], monitor_aliases={'side': LEFT.device}, zones=ZONES,
                  fancy=fancy or (lambda: {}), applications=lambda: desk.applications, app_aliases={})
    values.update(overrides)
    return values


def definition_of(items):
    """A definition as it reaches the launcher: through the configuration loader."""
    return workspaces.load_workspaces({'design': {'items': items}})['design']


def run(desk, items, name='design', **overrides):
    return workspaces.open_workspace(name, definition_of(items), **environment(desk, **overrides))


@pytest.fixture
def two_windows(tmp_path):
    book, solutions = pdf(tmp_path, 'book one.pdf'), pdf(tmp_path, 'solutions.pdf')
    items = [browser_item([book, solutions], 'left', 'textbooks'),
             browser_item(['https://chatgpt.example.test'], 'right', 'chat')]
    return book, solutions, items


# --- opening -------------------------------------------------------------------------

def test_each_browser_window_is_launched_once_and_placed_in_its_zone(monkeypatch, two_windows):
    book, solutions, items = two_windows
    desk = Desk(monkeypatch, existing=[chrome_window(50)], appear=[[chrome_window(101)], [chrome_window(102)]])
    result = run(desk, items)
    assert result['action'] == 'workspace_opened' and result['workspace'] == 'design'
    assert [(i['label'], i['kind'], i['outcome']) for i in result['items']] == [
        ('textbooks', 'browser_window', 'opened_and_placed'), ('chat', 'browser_window', 'opened_and_placed')]
    assert desk.placed[101]['rectangle'] == list(zone_rectangle(PRIMARY, [0, 0, 0.5, 1]))
    assert desk.placed[102]['rectangle'] == list(zone_rectangle(PRIMARY, [0.5, 0, 0.5, 1]))
    assert [i['hwnd'] for i in result['items']] == [101, 102] and 50 not in desk.placed
    assert result['items'][0]['zone'] == 'left' and result['items'][0]['monitor'] == PRIMARY.device
    assert len(desk.launches) == 2
    first, second = desk.launches
    assert first[0] == CHROME.executable and '--new-window' in first
    assert first[-2:] == [book.resolve().as_uri(), solutions.resolve().as_uri()]
    assert second[-1] == 'https://chatgpt.example.test'


def test_inline_rectangle_and_maximise_need_no_named_zone(monkeypatch, tmp_path):
    desk = Desk(monkeypatch, appear=[[chrome_window(101)], [chrome_window(102)]])
    items = [browser_item(['https://a.example.test'], [0.25, 0, 0.5, 1], 'a'),
             browser_item(['https://b.example.test'], None, 'b', state='maximise', monitor='side')]
    result = run(desk, items)
    assert desk.placed[101]['rectangle'] == list(zone_rectangle(PRIMARY, [0.25, 0, 0.5, 1]))
    assert desk.placed[102] == {'monitor': LEFT.device, 'rectangle': list(LEFT.work_area), 'state': 'maximise'}
    assert 'zone' not in result['items'][0]


def test_only_windows_of_the_browsers_own_process_are_candidates(monkeypatch):
    other = Window(7, 'Notes', 'notepad', 5)
    desk = Desk(monkeypatch, existing=[chrome_window(50), other], appear=[[chrome_window(101), other]])
    run(desk, [browser_item(['https://a.example.test'], 'left', 'a')])
    assert list(desk.placed) == [101]


def test_fancyzones_are_read_only_when_a_named_zone_is_used(monkeypatch):
    reads = []
    desk = Desk(monkeypatch, appear=[[chrome_window(101)]])
    run(desk, [browser_item(['https://a.example.test'], [0, 0, 1, 1], 'a')], fancy=lambda: reads.append(1) or {})
    assert reads == []
    desk = Desk(monkeypatch, appear=[[chrome_window(101)], [chrome_window(102)]])
    run(desk, [browser_item(['https://a.example.test'], 'left'), browser_item(['https://b.example.test'], 'right')],
        fancy=lambda: reads.append(1) or {})
    assert reads == [1]


# --- app items -----------------------------------------------------------------------

def app_item(label='writer', **extra):
    return {'kind': 'app', 'target': 'Word', 'label': label, 'monitor': 'primary', 'zone': 'left', **extra}


def test_an_app_with_exactly_one_open_window_is_placed_without_launching(monkeypatch):
    desk = Desk(monkeypatch, existing=[Window(300, 'Doc', 'WINWORD', 3000)])
    result = run(desk, [app_item()])
    assert result['items'][0]['outcome'] == 'placed_existing' and result['items'][0]['hwnd'] == 300
    assert desk.startfiles == [] and desk.launches == [] and 300 in desk.placed


def test_an_app_with_no_window_is_launched_once_and_placed(monkeypatch):
    desk = Desk(monkeypatch)
    result = run(desk, [app_item()])
    assert result['items'][0]['outcome'] == 'opened_and_placed' and result['items'][0]['hwnd'] == 900
    assert len(desk.startfiles) == 1 and 900 in desk.placed


def test_an_app_with_several_open_windows_is_ambiguous_and_not_launched(monkeypatch):
    desk = Desk(monkeypatch, existing=[Window(300, 'A', 'WINWORD', 3000), Window(301, 'B', 'WINWORD', 3000)])
    with pytest.raises(workspaces.WorkspaceError) as caught:
        run(desk, [app_item()])
    item = caught.value.data['items'][0]
    assert item['outcome'] == 'failed' and 'several' in item['reason'].casefold()
    assert desk.startfiles == [] and desk.placed == {}


# --- validation: all or nothing -------------------------------------------------------

def bad_cases(tmp_path):
    folder = tmp_path / 'a folder'
    folder.mkdir()
    script = tmp_path / 'tool.exe'
    script.write_bytes(b'MZ')
    return {
        'missing file': (browser_item([tmp_path / 'absent.pdf'], 'left'), 'not found'),
        'folder': (browser_item([folder], 'left'), 'not found'),
        'executable': (browser_item([script], 'left'), 'executable'),
        'ftp': (browser_item(['ftp://example.test/x'], 'left'), 'http'),
        'file scheme': (browser_item(['file:///C:/x.pdf'], 'left'), 'http'),
        'javascript': (browser_item(['javascript:alert(1)'], 'left'), 'http'),
        'no host': (browser_item(['https://'], 'left'), 'http'),
        'unknown monitor': (browser_item(['https://a.example.test'], 'left', monitor='9'), 'display'),
        'unknown zone': (browser_item(['https://a.example.test'], 'nowhere'), 'zone'),
        'bad rectangle': (browser_item(['https://a.example.test'], [0.6, 0, 0.6, 1]), 'zone'),
        'zone with maximise': (browser_item(['https://a.example.test'], 'left', state='maximise'), 'maximise'),
        'bad state': (browser_item(['https://a.example.test'], 'left', state='fullscreen'), 'state'),
        'unknown browser': (browser_item(['https://a.example.test'], 'left', browser='netscape'), 'browser'),
        'unknown app': ({'kind': 'app', 'target': 'Nonexistent', 'monitor': 'primary'}, 'not found'),
    }


BAD_CASES = ['missing file', 'folder', 'executable', 'ftp', 'file scheme', 'javascript', 'no host', 'unknown monitor',
             'unknown zone', 'bad rectangle', 'zone with maximise', 'bad state', 'unknown browser', 'unknown app']


def refuse_named_browsers(name=None):
    if name:
        raise ValueError(f'Unknown browser: {name}')
    return CHROME


@pytest.mark.parametrize('case', BAD_CASES)
def test_one_bad_item_launches_nothing_and_names_the_item(monkeypatch, tmp_path, case):
    bad, expected = bad_cases(tmp_path)[case]
    desk = Desk(monkeypatch, existing=[Window(300, 'Doc', 'WINWORD', 3000)])
    monkeypatch.setattr(workspaces, 'find_browser', refuse_named_browsers)
    good = browser_item(['https://a.example.test'], 'left', 'fine')
    bad = {**bad, 'label': 'second'}
    with pytest.raises(ValueError) as caught:
        run(desk, [good, bad])
    message = str(caught.value)
    assert 'second' in message and 'item 2' in message and expected in message.casefold()
    assert desk.launches == [] and desk.startfiles == [] and desk.placed == {}
    assert str(tmp_path) not in message and 'absent' not in message and 'a folder' not in message


def test_the_error_for_a_missing_file_does_not_reveal_its_name(monkeypatch, tmp_path):
    desk = Desk(monkeypatch)
    with pytest.raises(ValueError) as caught:
        run(desk, [browser_item([pdf(tmp_path, 'ok.pdf'), tmp_path / 'private textbook.pdf'], 'left', 'books')])
    assert 'private' not in str(caught.value) and 'file 2 of 2' in str(caught.value)


# --- failures never relaunch ------------------------------------------------------------

def test_a_placement_failure_is_reported_for_that_item_and_never_relaunches(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(101)], [chrome_window(102)]], fail_placement='Windows said no.')
    items = [browser_item(['https://a.example.test'], 'left', 'a'), browser_item(['https://b.example.test'], 'right', 'b')]
    with pytest.raises(workspaces.WorkspaceError) as caught:
        run(desk, items)
    data = caught.value.data
    assert data['action'] == 'workspace_partial'
    assert [(i['label'], i['outcome'], i.get('launch')) for i in data['items']] == [
        ('a', 'failed', 'accepted'), ('b', 'failed', 'accepted')]
    assert data['items'][0]['hwnd'] == 101 and 'Windows said no.' in data['items'][0]['reason']
    assert len(desk.launches) == 2  # one launch per item, none repeated


def test_a_failed_item_does_not_stop_the_next_one(monkeypatch):
    desk = Desk(monkeypatch, appear=[[], [chrome_window(102)]])
    items = [browser_item(['https://a.example.test'], 'left', 'a'), browser_item(['https://b.example.test'], 'right', 'b')]
    with pytest.raises(workspaces.WorkspaceError) as caught:
        run(desk, items)
    first, second = caught.value.data['items']
    assert first['outcome'] == 'failed' and 'time' in first['reason'].casefold() and first['launch'] == 'accepted'
    assert second['outcome'] == 'opened_and_placed' and 102 in desk.placed


def test_several_new_browser_windows_are_never_resolved_arbitrarily(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(101), chrome_window(103)]])
    with pytest.raises(workspaces.WorkspaceError) as caught:
        run(desk, [browser_item(['https://a.example.test'], 'left', 'a')])
    item = caught.value.data['items'][0]
    assert item['outcome'] == 'failed' and 'several' in item['reason'].casefold() and desk.placed == {}


def test_items_that_run_out_of_time_do_not_launch(monkeypatch):
    desk = Desk(monkeypatch, appear=[[], []])
    items = [browser_item(['https://a.example.test'], 'left', 'a'), browser_item(['https://b.example.test'], 'right', 'b')]
    with pytest.raises(workspaces.WorkspaceError) as caught:
        workspaces.open_workspace('design', definition_of(items), deadline_sec=0.3, **environment(desk))
    first, second = caught.value.data['items']
    assert first['outcome'] == 'failed' and second['outcome'] == 'failed'
    assert 'out of time' in second['reason'] and 'launch' not in second
    assert len(desk.launches) == 1


def test_a_launch_the_system_refuses_is_a_failure_without_the_path(monkeypatch):
    error = FileNotFoundError(2, 'The system cannot find the file specified', r'C:\Secret Place\chrome.exe')
    desk = Desk(monkeypatch, spawn_error=error)
    with pytest.raises(workspaces.WorkspaceError) as caught:
        run(desk, [browser_item(['https://a.example.test'], 'left', 'a')])
    item = caught.value.data['items'][0]
    assert item['outcome'] == 'failed' and 'launch' not in item
    assert 'Secret' not in json.dumps(caught.value.data)


# --- exclusivity ----------------------------------------------------------------------

def test_a_second_workspace_launch_is_refused_while_one_is_opening(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(101)]])
    assert workspaces._launch_lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match='already opening'):
            run(desk, [browser_item(['https://a.example.test'], 'left', 'a')])
    finally:
        workspaces._launch_lock.release()
    assert desk.launches == []
    run(desk, [browser_item(['https://a.example.test'], 'left', 'a')])  # the lock is free again
    assert len(desk.launches) == 1


def test_the_lock_is_released_after_a_validation_failure(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(101)]])
    with pytest.raises(ValueError):
        run(desk, [browser_item(['ftp://example.test'], 'left')])
    assert not workspaces._launch_lock.locked()


# --- privacy --------------------------------------------------------------------------

def test_results_and_logs_carry_no_paths_urls_or_labels(monkeypatch, tmp_path):
    book = pdf(tmp_path, 'Private Textbook.pdf')
    desk = Desk(monkeypatch, appear=[[chrome_window(101)], [chrome_window(102)]])
    items = [browser_item([book], 'left', 'secretlabel'), browser_item([SECRET_URL], 'right', 'othersecret')]
    result = run(desk, items)
    shown = json.dumps(result)
    for secret in ('Private Textbook', str(tmp_path), 'private.example.test', 'token=abc', 'chrome.exe'):
        assert secret not in shown
    logged = ' '.join(desk.logs)
    for secret in ('Private Textbook', str(tmp_path), 'private.example.test', 'token=abc', 'secretlabel',
                   'othersecret', 'chrome.exe', 'design'):
        assert secret not in logged
    assert logged  # counts and kinds are logged


# --- configuration shape -------------------------------------------------------------

def good_definition(**extra):
    return {'aliases': ['Design Project'], 'items': [browser_item(['https://a.example.test'], 'left', 'a')], **extra}


def test_load_keeps_well_formed_workspaces_and_derives_labels():
    loaded = workspaces.load_workspaces({
        'Design': {'aliases': ['Design Project', '', 3], 'items': [
            {'kind': 'browser_window', 'urls': ['https://a.example.test'], 'monitor': 'primary', 'zone': 'left'},
            {'kind': 'app', 'target': 'Word', 'monitor': 'primary'}]}})
    assert list(loaded) == ['Design']
    assert loaded['Design']['aliases'] == ['Design Project']
    assert [item['label'] for item in loaded['Design']['items']] == ['browser window 1', 'Word']


@pytest.mark.parametrize('bad', [
    'text', None, 3, [], {'': good_definition()}, {'x': 'not an object'}, {'x': {'items': []}},
    {'x': {'items': 'nope'}}, {'x': {'items': [{'kind': 'folder', 'monitor': 'primary'}]}},
    {'x': {'items': [{'kind': 'browser_window', 'urls': [], 'monitor': 'primary'}]}},
    {'x': {'items': [{'kind': 'browser_window', 'urls': 'https://a.example.test', 'monitor': 'primary'}]}},
    {'x': {'items': [{'kind': 'browser_window', 'urls': ['https://a.example.test'], 'monitor': 3}]}},
    {'x': {'items': [{'kind': 'app', 'target': '', 'monitor': 'primary'}]}},
    {'x': {'items': [{'kind': 'app', 'target': 'Word', 'monitor': 'primary', 'zone': {'x': 1}}]}},
])
def test_load_drops_malformed_workspaces(bad):
    assert workspaces.load_workspaces(bad) == {}


def test_load_keeps_good_workspaces_next_to_bad_ones():
    loaded = workspaces.load_workspaces({'good': good_definition(), 'bad': {'items': []}})
    assert list(loaded) == ['good']


def test_names_and_aliases_claimed_twice_are_not_offered():
    loaded = workspaces.load_workspaces({
        'design': good_definition(aliases=['shared', 'mine']),
        'DESIGN': good_definition(aliases=[]),
        'essays': good_definition(aliases=['Shared', 'drafts']),
        'gaming': good_definition(aliases=['design'])})
    assert loaded['essays']['aliases'] == ['drafts'] and loaded['gaming']['aliases'] == []
    assert 'design' not in loaded and 'DESIGN' not in loaded
    assert set(loaded) == {'essays', 'gaming'}


# --- resolving and listing ------------------------------------------------------------

def test_a_workspace_resolves_by_name_or_alias_ignoring_case():
    loaded = workspaces.load_workspaces({'Design': good_definition(aliases=['Design Project'])})
    assert workspaces.resolve_workspace('  design ', loaded)[0] == 'Design'
    assert workspaces.resolve_workspace('DESIGN PROJECT', loaded)[0] == 'Design'


def test_an_unknown_workspace_lists_the_available_names():
    loaded = workspaces.load_workspaces({'Design': good_definition(aliases=[]), 'Essays': good_definition(aliases=[])})
    with pytest.raises(ValueError) as caught:
        workspaces.resolve_workspace('gaming', loaded)
    assert 'Design' in str(caught.value) and 'Essays' in str(caught.value)
    with pytest.raises(ValueError):
        workspaces.resolve_workspace('  ', loaded)


def test_listing_shows_names_aliases_and_item_labels_only(tmp_path):
    loaded = workspaces.load_workspaces({'Design': {'aliases': ['rp'], 'items': [
        browser_item([SECRET_URL], 'left', 'chat'), app_item('writer')]}})
    listing = workspaces.list_workspaces(loaded)
    assert listing == {'workspaces': [{'name': 'Design', 'aliases': ['rp'], 'items': [
        {'label': 'chat', 'kind': 'browser_window'}, {'label': 'writer', 'kind': 'app'}]}]}
    assert 'example.test' not in json.dumps(listing)


# --- browsers -------------------------------------------------------------------------

def test_the_default_browser_is_used_when_chromium_based_otherwise_chrome(monkeypatch):
    found = {'chrome': r'C:\c\chrome.exe', 'edge': r'C:\e\msedge.exe', 'brave': None}
    monkeypatch.setattr(workspaces, '_locate_browser', lambda key: found.get(key))
    monkeypatch.setattr(workspaces, '_default_browser_key', lambda: 'edge')
    assert workspaces.find_browser().process == 'msedge'
    monkeypatch.setattr(workspaces, '_default_browser_key', lambda: None)  # Firefox or unknown
    assert workspaces.find_browser().process == 'chrome'
    assert workspaces.find_browser('Edge').executable == r'C:\e\msedge.exe'
    with pytest.raises(ValueError, match='browser'):
        workspaces.find_browser('netscape')
    with pytest.raises(ValueError, match='not found'):
        workspaces.find_browser('brave')


def test_a_missing_default_browser_falls_back_to_chrome(monkeypatch):
    monkeypatch.setattr(workspaces, '_locate_browser', lambda key: r'C:\c\chrome.exe' if key == 'chrome' else None)
    monkeypatch.setattr(workspaces, '_default_browser_key', lambda: 'edge')
    assert workspaces.find_browser().process == 'chrome'


def test_concurrent_open_attempts_only_let_one_through(monkeypatch):
    desk = Desk(monkeypatch, appear=[[chrome_window(101)]])
    release, entered = threading.Event(), threading.Event()
    original = desk.spawn

    def slow_spawn(command):
        entered.set()
        release.wait(2)
        original(command)

    monkeypatch.setattr(workspaces, '_spawn', slow_spawn)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(run(desk, [browser_item(['https://a.example.test'], 'left')])))
    worker.start()
    assert entered.wait(2)
    with pytest.raises(ValueError, match='already opening'):
        run(desk, [browser_item(['https://b.example.test'], 'right')])
    release.set()
    worker.join(5)
    assert outcome and outcome[0]['action'] == 'workspace_opened'

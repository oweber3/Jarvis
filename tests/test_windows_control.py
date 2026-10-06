"""Behaviour tests for Windows control with OS boundaries replaced by fakes."""
import json
from dataclasses import replace

import pytest


def test_application_names_resolve_from_native_names_and_aliases():
    from jarvis.platform.windows.apps import Application, resolve_application

    apps = [Application('Microsoft Word', 'word.lnk'),
            Application('Google Chrome', 'chrome.lnk'),
            Application('MATLAB R2025a', 'matlab.lnk')]
    for name, target in [('word', 'word.lnk'), ('Chrome', 'chrome.lnk'),
                         ('MATLAB', 'matlab.lnk'), ('editor', 'word.lnk')]:
        assert resolve_application(name, apps, {'editor': 'Microsoft Word'}).target == target


def test_application_resolution_refuses_unknown_and_ambiguous_names():
    from jarvis.platform.windows.apps import Application, resolve_application

    apps = [Application('MATLAB R2024a', 'old.lnk'), Application('MATLAB R2025a', 'new.lnk')]
    for query in ['', '!!!', 'missing', 'MATLAB']:
        with pytest.raises(ValueError):
            resolve_application(query, apps, {})
    assert resolve_application('MATLAB R2025a', apps, {}).target == 'new.lnk'


def test_application_resolution_prefers_launcher_over_helper_shortcuts():
    from jarvis.platform.windows.apps import Application, resolve_application
    apps = [Application('MATLAB R2026b', 'matlab.lnk', 'C:/apps/MATLAB/bin/matlab.exe'),
            Application('Activate MATLAB R2026b', 'activate.lnk', 'C:/apps/MATLAB/bin/activate.exe'),
            Application('MATLAB Connector', 'connector.lnk', 'C:/apps/MATLAB/bin/connector.exe')]
    assert resolve_application('MATLAB', apps, {}).target == 'matlab.lnk'


def test_application_aliases_control_windows_by_discovered_executable(monkeypatch):
    from jarvis.platform.windows import apps, windows_mgmt as wm
    from jarvis.tools.builtin.windows import AppControlTool
    from jarvis.tools.base import ToolContext
    from jarvis.config import load_settings
    cfg = replace(load_settings(), windows_app_aliases={'editor': 'Microsoft Word'})
    monkeypatch.setattr(apps.APP_INDEX, 'applications', lambda: [
        apps.Application('Microsoft Word', 'word.lnk', 'C:/Office/WINWORD.EXE')])
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Document - Word', 'WINWORD', 1)])
    state = []
    monkeypatch.setattr(wm, '_close_window', state.append)
    result = AppControlTool().run({'action': 'close', 'target': 'editor'},
                                 ToolContext(None, cfg, '', '', '', 1, lambda text: None))
    assert result.success
    assert state == [101]



def test_window_resolution_uses_process_or_title_and_refuses_ambiguity():
    from jarvis.platform.windows.windows_mgmt import Window, resolve_window

    windows = [Window(101, 'Private document - Chrome', 'chrome', 1),
               Window(202, 'Music', 'Spotify', 2)]
    assert resolve_window('chrome', windows).hwnd == 101
    assert resolve_window('Spotify', windows).hwnd == 202
    assert resolve_window('202', windows).hwnd == 202
    with pytest.raises(ValueError):
        resolve_window('chrome', windows + [Window(303, 'Chrome', 'chrome', 3)])
    with pytest.raises(ValueError):
        resolve_window('', windows)
    with pytest.raises(ValueError):
        resolve_window('Word', [Window(4, 'Password manager', 'vault', 4)])


def test_list_windows_filters_cloaked_windows(monkeypatch):
    import ctypes
    from ctypes import wintypes
    from jarvis.platform.windows import windows_mgmt as wm

    class FakeUser32:
        def IsWindowVisible(self, hwnd):
            return True

        def GetWindow(self, hwnd, cmd):
            return 0  # no owner

        def GetWindowTextLengthW(self, hwnd):
            return 16

        def GetWindowTextW(self, hwnd, buf, max_len):
            buf.value = f"Window {hwnd}"
            return len(buf.value)

        def GetWindowThreadProcessId(self, hwnd, pid_ptr):
            pid_ptr._obj.value = 1000 + hwnd
            return 1000 + hwnd

        def EnumWindows(self, callback, lparam):
            for hwnd in [101, 202, 303]:
                callback(hwnd, lparam)
            return True

    monkeypatch.setattr(wm, '_user32', lambda: FakeUser32())
    monkeypatch.setattr('psutil.Process', lambda pid: type('Proc', (), {'name': lambda self: 'app.exe'})())

    # Window 202 is cloaked, others are not
    def fake_is_cloaked(hwnd):
        return hwnd == 202

    monkeypatch.setattr(wm, '_is_cloaked', fake_is_cloaked)

    windows = wm.list_windows()
    hwnds = [w.hwnd for w in windows]
    assert 101 in hwnds
    assert 303 in hwnds
    assert 202 not in hwnds


def test_list_windows_dwm_failure_does_not_crash(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm

    class FakeUser32:
        def IsWindowVisible(self, hwnd):
            return True

        def GetWindow(self, hwnd, cmd):
            return 0

        def GetWindowTextLengthW(self, hwnd):
            return 4

        def GetWindowTextW(self, hwnd, buf, max_len):
            buf.value = "App"
            return 3

        def GetWindowThreadProcessId(self, hwnd, pid_ptr):
            pid_ptr._obj.value = 1000
            return 1000

        def EnumWindows(self, callback, lparam):
            callback(101, lparam)
            return True

    monkeypatch.setattr(wm, '_user32', lambda: FakeUser32())
    monkeypatch.setattr('psutil.Process', lambda pid: type('Proc', (), {'name': lambda self: 'app.exe'})())

    # When DWM attribute lookup raises or fails internally, _is_cloaked handles it gracefully
    # Here test dwm failure via _dwm raising or returning error
    class BrokenDwm:
        def DwmGetWindowAttribute(self, hwnd, attr, pv, size):
            raise OSError("DWM failure")

    monkeypatch.setattr(wm, '_dwmapi', lambda: BrokenDwm())

    windows = wm.list_windows()
    assert len(windows) == 1
    assert windows[0].hwnd == 101


def test_window_matching_cannot_select_cloaked_window(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm

    # control_window fetches list_windows, which excludes cloaked windows
    # Window 202 matches query by title/process but is cloaked, so resolve fails
    visible = [wm.Window(101, 'Text Editor', 'editor', 10)]
    cloaked = [wm.Window(202, 'Calculator', 'calc', 20)]

    monkeypatch.setattr(wm, 'list_windows', lambda: visible)
    with pytest.raises(ValueError, match="No open application window matches: calc"):
        wm.control_window('focus', 'calc')



@pytest.mark.parametrize('action', ['minimise', 'maximise', 'restore'])
def test_window_action_changes_selected_window(action, monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm

    state = {101: 'restore', 202: 'restore'}
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Chrome', 'chrome', 1)])
    monkeypatch.setattr(wm, '_show_window', lambda hwnd, value: state.__setitem__(hwnd, value))
    result = wm.control_window(action, 'chrome')
    assert state == {101: action, 202: 'restore'}
    assert result['hwnd'] == 101


def test_focus_failure_is_reported(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Spotify', 'spotify', 1)])
    monkeypatch.setattr(wm, '_focus_window', lambda hwnd: False)
    with pytest.raises(OSError):
        wm.control_window('focus', 'spotify')


def test_graceful_close_requests_close_without_terminating_process(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm
    state = {'close_requested': False, 'running': True}
    monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'Editor', 'editor', 1)])
    def close(hwnd):
        assert hwnd == 101
        state['close_requested'] = True
    monkeypatch.setattr(wm, '_close_window', close)
    result = wm.control_window('close', 'editor')
    assert state == {'close_requested': True, 'running': True}
    assert result['action'] == 'close_requested'


def test_paths_open_files_folders_and_known_folders(tmp_path, monkeypatch):
    from jarvis.platform.windows import files
    document = tmp_path / 'report.txt'
    document.write_text('sample')
    opened = []
    monkeypatch.setattr(files, '_shell_open', opened.append)
    monkeypatch.setattr(files, 'known_folder', lambda name: str(tmp_path))
    for target in [str(document), str(tmp_path), 'Downloads']:
        files.open_path(target)
    assert opened == [str(document), str(tmp_path), str(tmp_path)]


@pytest.mark.parametrize('target', ['https://example.com/test?a=1', 'HTTP://example.com', 'www.youtube.com'])
def test_web_addresses_are_refused_and_point_to_open_website(target, monkeypatch):
    from jarvis.platform.windows import files
    opened = []
    monkeypatch.setattr(files, '_shell_open', opened.append)
    monkeypatch.setattr(files, 'find_by_name', lambda *a, **k: pytest.fail('a web address is not a name'),
                        raising=False)
    with pytest.raises(ValueError, match='openWebsite'):
        files.open_path(target)
    assert opened == []


@pytest.mark.parametrize('target', ['', 'javascript:alert(1)', 'shell:AppsFolder',
                                    'https://', 'file://server/share', r'C:\missing\missing.txt'])
def test_invalid_paths_do_not_launch(target, monkeypatch):
    from jarvis.platform.windows import files
    opened = []
    monkeypatch.setattr(files, '_shell_open', opened.append)
    with pytest.raises((ValueError, FileNotFoundError)):
        files.open_path(target)
    assert opened == []


@pytest.mark.parametrize('suffix', ['.hta', '.vbe', '.jse', '.wsf', '.wsh', '.exe', '.py', '.pyw', '.psm1'])
def test_script_files_are_not_opened(suffix, tmp_path, monkeypatch):
    from jarvis.platform.windows import files
    target = tmp_path / ('payload' + suffix)
    target.write_text('test')
    opened = []
    monkeypatch.setattr(files, '_shell_open', opened.append)
    with pytest.raises(ValueError):
        files.open_path(str(target))
    assert not opened


def test_resolved_executable_is_not_opened_as_a_document(tmp_path, monkeypatch):
    from jarvis.platform.windows import files
    document = tmp_path / 'report.txt'
    executable = tmp_path / 'payload.exe'
    document.write_text('test')
    executable.write_text('test')
    resolve = type(document).resolve
    monkeypatch.setattr(type(document), 'resolve', lambda path, **kwargs:
                        executable if path == document else resolve(path, **kwargs))
    opened = []
    monkeypatch.setattr(files, '_shell_open', opened.append)
    with pytest.raises(ValueError):
        files.open_path(str(document))
    assert not opened


def test_registry_setting_and_platform_control_catalogue(monkeypatch):
    from jarvis.config import load_settings
    from jarvis.tools import registry
    original = dict(registry.BUILTIN_TOOLS)
    cfg = load_settings()
    try:
        registry.configure_windows_tools(replace(cfg, windows_tools_enabled=False), platform='win32')
        assert not {'appControl', 'windowControl', 'openPath'} & registry.BUILTIN_TOOLS.keys()
        registry.configure_windows_tools(cfg, platform='linux')
        assert 'appControl' not in registry.BUILTIN_TOOLS
        registry.configure_windows_tools(cfg, platform='win32', start_index=False)
        schema = registry.generate_tools_json_schema(['appControl', 'windowControl', 'openPath'])
        assert {entry['function']['name'] for entry in schema} == {'appControl', 'windowControl', 'openPath'}
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def test_registry_executes_and_redacts_window_results(monkeypatch):
    from jarvis.config import load_settings
    from jarvis.tools import registry
    from jarvis.platform.windows import windows_mgmt as wm
    cfg = load_settings()
    original = dict(registry.BUILTIN_TOOLS)
    try:
        registry.configure_windows_tools(cfg, platform='win32', start_index=False)
        monkeypatch.setattr(wm, 'list_windows', lambda: [wm.Window(101, 'person@example.com token=secret', 'chrome', 1)])
        result = registry.run_tool_with_retries(None, cfg, 'appControl', {'action': 'list'}, '', '', '')
        assert result.success
        assert 'person@example.com' not in result.reply_text
        assert json.loads(result.reply_text)['windows'][0]['hwnd'] == 101
        disabled = registry.run_tool_with_retries(None, replace(cfg, windows_tools_enabled=False),
                                                  'appControl', {'action': 'list'}, '', '', '')
        assert not disabled.success
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def test_invalid_actions_cannot_execute(monkeypatch):
    from jarvis.tools.builtin.windows import AppControlTool, WindowControlTool, OpenPathTool
    from jarvis.tools.base import ToolContext
    from jarvis.config import load_settings
    context = ToolContext(None, load_settings(), '', '', '', 1, lambda text: None)
    for tool, args in [(AppControlTool(), {'action': 'kill', 'target': 'editor'}),
                       (WindowControlTool(), {'action': 'snap', 'target': 'chrome'}),
                       (OpenPathTool(), {'target': 'javascript:alert(1)'})]:
        assert not tool.run(args, context).success


def test_hung_native_operation_returns_failure_within_deadline(monkeypatch):
    import threading
    import time
    from jarvis.tools.builtin.windows.desktop_control import OpenPathTool
    from jarvis.tools.base import ToolContext
    from jarvis.config import load_settings
    finished = threading.Event()
    monkeypatch.setattr(OpenPathTool, 'timeout_sec', .05, raising=False)
    monkeypatch.setattr(OpenPathTool, '_operate', lambda *args: finished.wait(5))
    start = time.monotonic()
    try:
        result = OpenPathTool().run({'target': 'Downloads'},
                                   ToolContext(None, load_settings(), '', '', '', 1, lambda text: None))
        assert not result.success
        assert time.monotonic() - start < 1
    finally:
        finished.set()


def test_open_path_rejects_unknown_arguments_before_launch(monkeypatch):
    from jarvis.tools.builtin.windows import OpenPathTool
    from jarvis.tools.base import ToolContext
    from jarvis.config import load_settings
    opened = []
    monkeypatch.setattr(OpenPathTool, '_operate', lambda *args: opened.append(args))
    result = OpenPathTool().run({'target': 'Downloads', 'action': 'delete'},
                               ToolContext(None, load_settings(), '', '', '', 1, lambda text: None))
    assert not result.success
    assert opened == []

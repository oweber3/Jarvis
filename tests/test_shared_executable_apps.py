"""Catalogue entries that run a shared program (cmd.exe, powershell.exe, pythonw.exe, explorer.exe,
msiexec.exe) are separate applications: each opens under its own name, and none is reached by a bare
executable name it does not own. Opening an uninstaller needs confirmation."""
import json
from types import SimpleNamespace

import pytest

from jarvis.platform.windows.apps import Application

CMD = 'C:/Windows/System32/cmd.exe'
POWERSHELL = 'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
PYTHONW = 'C:/Python/pythonw.exe'
EXPLORER = 'C:/Windows/explorer.exe'
MSIEXEC = 'C:/Windows/System32/msiexec.exe'

CATALOGUE = (
    Application('RGB Lighting Control', 'rgb.lnk', CMD, '/c "C:/Tools/rgb.bat"'),
    Application('Command Prompt', 'cmd.lnk', CMD),
    Application('ESP-IDF 5.5 PowerShell', 'esp.lnk', POWERSHELL, '-NoExit -File "C:/esp/init.ps1"'),
    Application('Windows PowerShell', 'powershell.lnk', POWERSHELL),
    Application('Start Dashboard', 'start.lnk', PYTHONW, 'dashboard.py start'),
    Application('Stop Dashboard', 'stop.lnk', PYTHONW, 'dashboard.py stop'),
    Application('Windows Software Development Kit', 'sdk.lnk', EXPLORER, '"C:/Program Files (x86)/Windows Kits/10"'),
    Application('File Explorer', 'shell:AppsFolder\\Microsoft.Windows.Explorer'),
    Application('Uninstall LTspice', 'ltspice-uninstall.lnk', MSIEXEC, '/x {11111111-2222-3333-4444-555555555555}',
                uninstaller=True),
    Application('Uninstall Node.js', 'node-uninstall.lnk', MSIEXEC, '/x {66666666-7777-8888-9999-000000000000}',
                uninstaller=True),
    Application('Microsoft Word', 'word.lnk', 'C:/Office/WINWORD.EXE'),
)
TOOLS = {'appControl', 'windowControl'}


@pytest.fixture
def catalogue(mock_config, monkeypatch):
    from jarvis.platform.windows.apps import APP_INDEX
    monkeypatch.setattr(APP_INDEX, 'snapshot', lambda: CATALOGUE)
    monkeypatch.setattr(APP_INDEX, 'applications', lambda: list(CATALOGUE))
    mock_config.windows_app_aliases = {}
    return mock_config


@pytest.fixture
def windows_tools(catalogue, monkeypatch):
    from jarvis.tools import registry
    monkeypatch.setattr(registry, 'BUILTIN_TOOLS', dict(registry.BUILTIN_TOOLS))
    catalogue.windows_tools_enabled = True
    registry.configure_windows_tools(catalogue, platform='win32', start_index=False)
    return catalogue


def fast_route(text, cfg):
    from jarvis.fastpath.matcher import match
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    return match(text, 'en', targets=AppControlTool().fast_targets(cfg), available_tools=TOOLS)


@pytest.mark.parametrize('name', [app.name for app in CATALOGUE if not app.uninstaller])
def test_each_application_opens_under_its_own_name(catalogue, name):
    from jarvis.platform.windows.apps import resolve_application
    route = fast_route(f'Open {name}', catalogue)
    assert route is not None and route.args['action'] == 'open'
    launched = resolve_application(route.args['target'], list(CATALOGUE), {})
    assert launched.name == name


@pytest.mark.parametrize('text,refused', [
    ('Open explorer', 'Windows Software Development Kit'),
    ('Open cmd', 'RGB Lighting Control'),
    ('Open pythonw', 'Start Dashboard'),
    ('Open powershell', 'ESP-IDF 5.5 PowerShell'),
])
def test_a_bare_program_name_never_launches_a_shortcut_that_passes_it_arguments(catalogue, text, refused):
    from jarvis.platform.windows.apps import resolve_application
    route = fast_route(text, catalogue)
    if route is not None:
        assert resolve_application(route.args['target'], list(CATALOGUE), {}).name != refused


def test_the_bare_program_name_belongs_to_the_shortcut_that_runs_it_plainly():
    from jarvis.platform.windows.apps import resolve_application
    assert resolve_application('powershell', list(CATALOGUE), {}).name == 'Windows PowerShell'
    assert resolve_application('cmd', list(CATALOGUE), {}).name == 'Command Prompt'
    # Nothing runs explorer.exe plainly, so the name's own words find File Explorer.
    assert resolve_application('explorer', list(CATALOGUE), {}).name == 'File Explorer'


@pytest.mark.parametrize('text', ['Close Command Prompt', 'Close RGB Lighting Control', 'Close Stop Dashboard',
                                  'Switch to Windows PowerShell', 'Minimise Command Prompt',
                                  'Close Windows Software Development Kit'])
def test_window_actions_on_a_program_several_applications_share_fall_through(catalogue, text):
    from jarvis.fastpath.matcher import match
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    targets = AppControlTool().fast_targets(catalogue)
    assert match(text, 'en', targets=targets, available_tools=TOOLS) is None


def test_window_actions_still_route_for_a_program_one_application_owns(catalogue):
    from jarvis.fastpath.matcher import match
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    route = match('Close Microsoft Word', 'en', targets=AppControlTool().fast_targets(catalogue),
                  available_tools=TOOLS)
    assert route is not None
    assert route.args == {'action': 'close', 'target': 'WINWORD', 'match': 'process'}


def test_identical_shortcuts_to_one_program_still_share_window_control(mock_config, monkeypatch):
    from jarvis.platform.windows.apps import APP_INDEX
    from jarvis.fastpath.matcher import match
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    same = (Application('Microsoft Word', 'word.lnk', 'C:/Office/WINWORD.EXE'),
            Application('Word', 'word-desktop.lnk', 'C:/Office/WINWORD.EXE'))
    monkeypatch.setattr(APP_INDEX, 'snapshot', lambda: same)
    mock_config.windows_app_aliases = {}
    targets = AppControlTool().fast_targets(mock_config)
    for text in ('Close Word', 'Close Microsoft Word'):
        route = match(text, 'en', targets=targets, available_tools=TOOLS)
        assert route is not None and route.args['target'] == 'WINWORD'


def test_opening_an_uninstaller_needs_desktop_confirmation(catalogue):
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    tool = AppControlTool()
    request = evaluate_safety('appControl', {'action': 'open', 'target': 'Uninstall Node.js'}, catalogue, tool=tool)
    assert request.tier == SafetyTier.CONFIRM_DIALOG
    for target in ('Command Prompt', 'Microsoft Word', 'Not installed'):
        assert evaluate_safety('appControl', {'action': 'open', 'target': target}, catalogue,
                               tool=tool).tier == SafetyTier.SAFE


def test_an_uninstaller_is_never_fast_routed_or_launched_unconfirmed(windows_tools, monkeypatch):
    import os
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.tools.confirmation import get_confirmation_store, set_dialog_callback
    from jarvis.tools.registry import run_tool_with_retries
    launched = []
    monkeypatch.setattr(os, 'startfile', launched.append, raising=False)
    set_dialog_callback(None)
    try:
        assert match_command('open uninstall node.js', windows_tools, 'en') is None
        assert match_command('open command prompt', windows_tools, 'en') is not None
        result = run_tool_with_retries(None, windows_tools, 'appControl',
                                       {'action': 'open', 'target': 'Uninstall Node.js'}, '', '', '')
        assert not result.success and launched == []
    finally:
        get_confirmation_store().clear_pending()


UNINSTALL_NODE = next(app for app in CATALOGUE if app.name == 'Uninstall Node.js')


def _loading_index(monkeypatch, catalogue, *, ready_after_first_wait):
    """A real application index whose discovery is still running when the first caller waits for it (that
    wait runs out) and, with ``ready_after_first_wait``, finishes right after."""
    import threading
    from jarvis.platform.windows import apps
    gate = threading.Event()

    class FirstWaitRunsOut(threading.Event):
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                if ready_after_first_wait:
                    gate.set()
                return False
            return super().wait(min(timeout or 1, 1))

    def discover():
        gate.wait(5)
        return list(catalogue)

    index = apps.ApplicationIndex()
    index._ready = FirstWaitRunsOut()
    monkeypatch.setattr(apps, 'discover_applications', discover)
    monkeypatch.setattr(apps, 'APP_INDEX', index)
    index.start()
    return gate


@pytest.fixture
def no_launch(monkeypatch):
    import os
    from jarvis.platform.windows import windows_mgmt
    from jarvis.tools.confirmation import set_dialog_callback
    launched = []
    monkeypatch.setattr(os, 'startfile', launched.append, raising=False)
    monkeypatch.setattr(windows_mgmt, 'list_windows', lambda: [])
    set_dialog_callback(None)
    yield launched


def _open(cfg, target):
    from jarvis.tools.confirmation import get_confirmation_store
    from jarvis.tools.registry import run_tool_with_retries
    try:
        return run_tool_with_retries(None, cfg, 'appControl', {'action': 'open', 'target': target}, '', '', '')
    finally:
        get_confirmation_store().clear_pending()


def test_an_uninstaller_never_launches_unconfirmed_while_the_catalogue_is_loading(windows_tools, monkeypatch,
                                                                                 no_launch):
    """The catalogue is still loading when the call is classified and ready when it would launch."""
    gate = _loading_index(monkeypatch, CATALOGUE, ready_after_first_wait=True)
    try:
        result = _open(windows_tools, 'Uninstall Node.js')
    finally:
        gate.set()
    assert not result.success and no_launch == []


def test_without_a_catalogue_opening_an_application_asks_on_the_desktop(windows_tools, monkeypatch):
    """With no catalogue nothing shows that a launch is not an uninstaller, so it needs desktop confirmation."""
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    gate = _loading_index(monkeypatch, CATALOGUE, ready_after_first_wait=False)
    try:
        request = evaluate_safety('appControl', {'action': 'open', 'target': 'Microsoft Word'}, windows_tools,
                                  tool=AppControlTool())
    finally:
        gate.set()
    assert request.tier == SafetyTier.CONFIRM_DIALOG


def test_after_failed_discovery_opening_asks_nothing_and_fails_honestly(windows_tools, monkeypatch, no_launch):
    """A failed discovery cannot launch anything, so a confirmation would only be followed by a failure."""
    from jarvis.platform.windows import apps
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety

    def broken_discovery():
        raise RuntimeError('no catalogue')

    index = apps.ApplicationIndex()
    monkeypatch.setattr(apps, 'discover_applications', broken_discovery)
    monkeypatch.setattr(apps, 'APP_INDEX', index)
    index.start()
    request = evaluate_safety('appControl', {'action': 'open', 'target': 'Microsoft Word'}, windows_tools,
                              tool=AppControlTool())
    assert request.tier == SafetyTier.SAFE
    result = _open(windows_tools, 'Microsoft Word')
    assert not result.success and 'discovery failed' in result.error_message.casefold() and no_launch == []


def test_launching_an_uninstaller_needs_the_users_approval(windows_tools, no_launch):
    from jarvis.platform.windows.apps import open_application
    with pytest.raises(PermissionError):
        open_application('Uninstall Node.js', {})
    assert no_launch == []
    open_application('Uninstall Node.js', {}, allow_uninstaller=True)
    assert no_launch == [UNINSTALL_NODE.target]


def test_an_uninstaller_launches_once_the_user_confirms_on_the_desktop(windows_tools, no_launch):
    from types import SimpleNamespace
    from jarvis.tools.confirmation import get_confirmation_store, set_dialog_callback
    from jarvis.tools.registry import run_tool_with_retries
    shown = []
    set_dialog_callback(lambda request, resolve: shown.append(resolve) or SimpleNamespace(close=lambda: None))
    try:
        run_tool_with_retries(None, windows_tools, 'appControl',
                              {'action': 'open', 'target': 'Uninstall Node.js'}, '', '', '')
        assert len(shown) == 1 and no_launch == []
        shown[0](True)
        assert _wait_until(lambda: no_launch == [UNINSTALL_NODE.target])
    finally:
        set_dialog_callback(None)
        get_confirmation_store().clear_pending()


def _wait_until(predicate, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def start_menu(tmp_path, monkeypatch):
    """Two empty Start Menu folders, no App Paths and no packaged apps; shortcuts are described by a fake
    PowerShell reply."""
    import subprocess
    import winreg
    from jarvis.platform.windows import apps
    roots = {}
    for variable in ('APPDATA', 'PROGRAMDATA'):
        root = tmp_path / variable / 'Microsoft/Windows/Start Menu/Programs'
        root.mkdir(parents=True)
        monkeypatch.setenv(variable, str(tmp_path / variable))
        roots[variable] = root

    def no_registry(*args, **kwargs):
        raise OSError('no registry in tests')
    monkeypatch.setattr(winreg, 'OpenKey', no_registry)
    links = []

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({'Apps': [], 'Links': links}))
    monkeypatch.setattr(subprocess, 'run', fake_run)
    registered = []
    monkeypatch.setattr(apps, '_registered_uninstall_commands', lambda: list(registered))

    def add(name, executable, arguments=''):
        path = roots['APPDATA'] / f'{name}.lnk'
        path.write_bytes(b'')
        links.append({'Path': str(path), 'Executable': executable, 'Arguments': arguments})
    return SimpleNamespace(add=add, registered=registered)


def test_discovery_keeps_each_shortcuts_arguments(start_menu):
    from jarvis.platform.windows.apps import _discover_system_applications
    start_menu.add('Command Prompt', r'C:\Windows\System32\cmd.exe')
    start_menu.add('RGB Lighting Control', r'C:\Windows\System32\cmd.exe', r'/c "C:\Tools\rgb.bat"')
    found = {app.name: app for app in _discover_system_applications()}
    assert found['Command Prompt'].arguments == ''
    assert found['RGB Lighting Control'].arguments == r'/c "C:\Tools\rgb.bat"'
    assert found['RGB Lighting Control'].executable == r'C:\Windows\System32\cmd.exe'


def test_discovery_recognises_uninstallers_from_installer_data(start_menu):
    from jarvis.platform.windows.apps import _discover_system_applications
    start_menu.registered.extend([
        r'"C:\Program Files\LTC\LTspice\uninstall.exe"',
        r'"C:\Users\me\AppData\Roaming\Spotify\Spotify.exe" /uninstall',
        r'C:\Program Files\Tool\unins000.exe /SILENT',
    ])
    start_menu.add('Remove LTspice', r'C:\Program Files\LTC\LTspice\uninstall.exe')
    start_menu.add('Node.js removal', r'C:\Windows\System32\msiexec.exe', '/x {66666666-7777-8888-9999-000000000000}')
    start_menu.add('Old product', r'C:\Windows\System32\MsiExec.exe', '/X{12345678-1234-1234-1234-123456789012}')
    start_menu.add('Tool cleaner', r'C:\Program Files\Tool\unins000.exe', '/SILENT')
    start_menu.add('Spotify', r'C:\Users\me\AppData\Roaming\Spotify\Spotify.exe')
    start_menu.add('Repair product', r'C:\Windows\System32\msiexec.exe', '/f {66666666-7777-8888-9999-000000000000}')
    start_menu.add('Command Prompt', r'C:\Windows\System32\cmd.exe')
    found = {app.name: app.uninstaller for app in _discover_system_applications()}
    assert found == {'Remove LTspice': True, 'Node.js removal': True, 'Old product': True, 'Tool cleaner': True,
                     'Spotify': False, 'Repair product': False, 'Command Prompt': False}

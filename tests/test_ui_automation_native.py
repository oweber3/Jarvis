"""Real UI Automation against a test-owned window of standard Win32 controls.

The fixture window lives far off-screen, is created with WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW and is
never activated: no focus change, no pointer or keyboard input, nothing visible. Every test checks
that the window never became the foreground window.
"""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = [pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows UI Automation'),
              pytest.mark.integration]

_APP = Path(__file__).parent / 'fixtures' / 'uia_fixture_app.py'


class Fixture:
    def __init__(self, hwnd, log_path):
        self.hwnd = hwnd
        self.window = str(hwnd)
        self._log = log_path

    def events(self):
        return [line for line in self._log.read_text(encoding='utf-8').splitlines() if line != 'ready']

    def wait_for(self, event, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if event in self.events():
                return True
            time.sleep(0.05)
        return False


def _user32():
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32')
    user.FindWindowW.restype = wintypes.HWND
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user


@contextlib.contextmanager
def fixture_window(tmp_path):
    user = _user32()
    title = 'Jarvis UIA fixture ' + uuid.uuid4().hex
    log_path = tmp_path / 'events.log'
    log_path.write_text('', encoding='utf-8')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
    process = subprocess.Popen([sys.executable, str(_APP), title, str(log_path)], startupinfo=startup,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    hwnd = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not hwnd:
            if 'ready' in log_path.read_text(encoding='utf-8'):
                hwnd = user.FindWindowW('JarvisUiaFixture', title)
            time.sleep(0.05)
        assert hwnd, 'The fixture window did not appear'
        yield Fixture(int(hwnd), log_path)
        assert user.GetForegroundWindow() != hwnd, 'The fixture window was activated'
    finally:
        if hwnd and process.poll() is None:
            user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def app(tmp_path):
    with fixture_window(tmp_path) as fixture:
        yield fixture


def _element(snapshot, name, kind=None):
    matches = [e for e in snapshot['elements'] if e['name'] == name and (kind is None or e['type'] == kind)]
    assert len(matches) == 1, (name, snapshot['elements'])
    return matches[0]


def test_snapshot_lists_interactive_controls_with_short_ids(app):
    from jarvis.platform.windows import ui_automation as ui
    snap = ui.snapshot(app.window)
    assert snap['window']['hwnd'] == app.hwnd
    assert snap['window']['process']
    ids = [e['id'] for e in snap['elements']]
    assert ids == [f'e{n}' for n in range(1, len(ids) + 1)]
    assert _element(snap, 'Apply', 'button')
    assert _element(snap, 'Word wrap', 'checkbox')['state'] == 'off'
    assert _element(snap, 'Subject:', 'edit')['value'] == 'Hello'
    assert {'Apple', 'Banana', 'Cherry'} <= {e['name'] for e in snap['elements'] if e['type'] == 'listitem'}
    assert {'File', 'Format'} <= {e['name'] for e in snap['elements'] if e['type'] == 'menuitem'}
    assert 'titlebar' not in {e['type'] for e in snap['elements']}
    assert len(json.dumps(snap)) < 8000


def test_snapshot_never_reveals_password_values(app):
    from jarvis.platform.windows import ui_automation as ui
    snap = ui.snapshot(app.window)
    password = _element(snap, 'Password:', 'edit')
    assert password.get('password') is True and 'value' not in password
    assert 'hunter2' not in json.dumps(snap)


def test_click_by_snapshot_id_and_by_name_invokes_buttons(app):
    from jarvis.platform.windows import ui_automation as ui
    snap = ui.snapshot(app.window)
    result = ui.act('click', app.window, _element(snap, 'Apply')['id'])
    assert result['element']['name'] == 'Apply'
    assert app.wait_for('click:Apply')
    ui.act('click', app.window, 'send')
    assert app.wait_for('click:Send')


def test_set_text_replaces_and_reads_back(app):
    from jarvis.platform.windows import ui_automation as ui
    result = ui.act('set_text', app.window, 'Subject', 'Quarterly report')
    assert result['value'] == 'Quarterly report'
    assert app.wait_for('change:Subject edit')
    assert _element(ui.snapshot(app.window), 'Subject:', 'edit')['value'] == 'Quarterly report'


def test_password_fields_are_never_typed_into_or_read(app):
    from jarvis.platform.windows import ui_automation as ui
    before = app.events()
    with pytest.raises(PermissionError):
        ui.act('set_text', app.window, 'Password', 'guess')
    with pytest.raises(PermissionError):
        ui.act('read', app.window, 'Password')
    time.sleep(0.3)
    assert app.events() == before


def test_toggle_reaches_the_requested_state_once(app):
    from jarvis.platform.windows import ui_automation as ui
    assert ui.act('toggle', app.window, 'Word wrap', 'on')['state'] == 'on'
    assert app.wait_for('toggle:Word wrap=1')
    assert ui.act('toggle', app.window, 'Word wrap', 'on')['state'] == 'on'
    time.sleep(0.3)
    assert app.events().count('toggle:Word wrap=1') == 1
    assert ui.act('toggle', app.window, 'Word wrap')['state'] == 'off'


def test_select_list_item_and_combo_option(app):
    # Win32 list and combo boxes change selection through UIA without sending the application a
    # selection-change notification, so the outcome is read back from the controls themselves.
    from jarvis.platform.windows import ui_automation as ui
    assert ui.act('select', app.window, 'Banana')['selected'] is True
    assert _element(ui.snapshot(app.window), 'Banana', 'listitem')['state'] == 'selected'
    assert ui.act('select', app.window, 'Colour', 'Green')['value'] == 'Green'
    assert _element(ui.snapshot(app.window), 'Colour:', 'combobox')['value'] == 'Green'


def test_menu_commands_run_without_opening_menus(app):
    from jarvis.platform.windows import ui_automation as ui
    result = ui.act('menu', app.window, '', 'file > save as')
    assert result['method'] == 'command'
    assert app.wait_for('menu:File > Save As')
    with pytest.raises(ValueError, match='disabled'):
        ui.act('menu', app.window, '', 'File > Print')
    with pytest.raises(ValueError, match='Save As'):
        ui.act('menu', app.window, '', 'File > Teleport')
    time.sleep(0.3)
    assert 'menu:File > Print' not in app.events()


def test_menu_item_names_are_read_without_opening_menus(app):
    from jarvis.platform.windows import ui_automation as ui
    assert ui.menu_item_names(app.window, 'file > file') == ['File', 'Delete file']
    assert ui.menu_item_names(app.window, 'File > Teleport') == ['File']


def test_read_and_scroll_a_long_text_field(app):
    from jarvis.platform.windows import ui_automation as ui
    read = ui.act('read', app.window, 'Notes')
    assert read['text'].startswith('Line 1:') and 'Line 70:' in read['text']
    assert read['truncated'] is True and len(read['text']) == ui.READ_CHARS
    before = ui.act('scroll', app.window, 'Notes', 'top')['vertical_percent']
    after = ui.act('scroll', app.window, 'Notes', 'down')['vertical_percent']
    assert after > before


def test_ids_belong_to_the_latest_snapshot_only(app):
    from jarvis.platform.windows import ui_automation as ui
    snap = ui.snapshot(app.window)
    last = snap['elements'][-1]['id']
    with pytest.raises(ValueError, match='snapshot'):
        ui.act('click', app.window, f'e{len(snap["elements"]) + 50}')
    assert ui.lookup(last) is not None


def test_inspect_reports_name_type_and_password_flag(app):
    from jarvis.platform.windows import ui_automation as ui
    assert ui.inspect(app.window, 'Password')['password'] is True
    send = ui.inspect(app.window, 'Send')
    assert (send['name'], send['type'], send['password']) == ('Send', 'button', False)


def test_viewer_page_box_is_set_through_its_value(app):
    from jarvis.platform.windows import pdf_viewer
    result = pdf_viewer.set_viewer_page(app.hwnd, 12, 40)
    assert result['page'] == 12
    assert app.wait_for('change:Page edit')
    from jarvis.platform.windows import ui_automation as ui
    assert _element(ui.snapshot(app.window), 'Page:', 'edit')['value'] == '12'


def test_tool_confirms_irreversible_clicks_and_denies_password_fields_end_to_end(app):
    from jarvis.config import load_settings
    from jarvis.tools import registry
    from jarvis.tools.confirmation import get_confirmation_store
    cfg = load_settings()
    original = dict(registry.BUILTIN_TOOLS)
    store = get_confirmation_store()
    store.cancel_pending()
    try:
        registry.configure_windows_tools(cfg, platform='win32', start_index=False)

        def run(args):
            return registry.run_tool_with_retries(None, cfg, 'uiControl', {'window': app.window, **args},
                                                  '', '', '', language='en')

        assert run({'action': 'click', 'element': 'Apply'}).success
        assert app.wait_for('click:Apply')
        pending = run({'action': 'click', 'element': 'Send'})
        assert not pending.success and 'confirmation' in pending.reply_text
        time.sleep(0.3)
        assert 'click:Send' not in app.events()
        store.handle_voice_response('yes', 'en')
        assert store.run_approved(store.claim_approved()).success
        assert app.wait_for('click:Send')
        partial = run({'action': 'menu', 'value': 'File > file'})
        assert not partial.success and 'Delete file' in partial.reply_text
        store.cancel_pending()
        time.sleep(0.3)
        assert 'menu:File > Delete file' not in app.events()
        before = app.events()
        denied = run({'action': 'set_text', 'element': 'Password', 'value': 'guess'})
        assert not denied.success and 'prohibited' in denied.error_message
        time.sleep(0.3)
        assert app.events() == before
    finally:
        store.cancel_pending()
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


@pytest.mark.interactive
def test_opening_a_menu_bar_menu_through_ui_automation(app):
    """A path that ends at a submenu must open it on screen, which may need the window's input queue.
    Run by hand: ``pytest -m interactive tests/test_ui_automation_native.py``."""
    from jarvis.platform.windows import ui_automation as ui
    try:
        result = ui.act('menu', app.window, '', 'Format')
        assert result['method'] == 'ui' and result['opened'] is True
    finally:
        _user32().PostMessageW(app.hwnd, 0x001F, 0, 0)  # WM_CANCELMODE closes the menu

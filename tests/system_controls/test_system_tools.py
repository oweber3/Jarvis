"""systemSettings and inputControl behaviour through the tool adapters, with OS layers replaced."""
import json
from types import SimpleNamespace

import pytest

from jarvis.platform.windows import audio_outputs, brightness, displays, input_control, power, settings_pages
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.windows.input_control import InputControlTool
from jarvis.tools.builtin.windows.system_settings import SystemSettingsTool


def context(**overrides):
    cfg = SimpleNamespace(**{'windows_tools_enabled': True, 'windows_monitor_aliases': {},
                             'windows_audio_aliases': {}, **overrides})
    printed = []
    return ToolContext(None, cfg, '', '', '', 1, printed.append), printed


def run(tool, args, **overrides):
    ctx, printed = context(**overrides)
    result = tool.run(args, ctx)
    return result, printed


def data(result):
    return json.loads(result.reply_text)


# --- schema and routing ------------------------------------------------------------------

def test_system_settings_schema_is_one_tool_with_an_action_enum_and_strict_arguments():
    schema = SystemSettingsTool().inputSchema
    assert schema['required'] == ['action'] and schema['additionalProperties'] is False
    assert set(schema['properties']['action']['enum']) == {
        'open_page', 'brightness', 'power_plan', 'audio_output', 'night_light', 'do_not_disturb'}
    assert {'page', 'percent', 'amount', 'name', 'monitor', 'operation'} <= schema['properties'].keys()


def test_descriptions_route_on_their_first_words_and_disambiguate_neighbours():
    settings, keys = SystemSettingsTool(), InputControlTool()
    assert 'settings' in settings.description[:120].casefold() and 'brightness' in settings.description.casefold()
    assert 'systemVolume' in settings.description
    assert 'hotkey' in keys.description[:120].casefold() or 'shortcut' in keys.description[:120].casefold()


@pytest.mark.parametrize('tool,args', [(SystemSettingsTool(), {'action': 'open_page', 'page': 'sound'}),
                                       (InputControlTool(), {'action': 'clipboard_read'})])
def test_a_disabled_windows_toolset_touches_nothing(tool, args, monkeypatch):
    called = []
    monkeypatch.setattr(settings_pages, 'open_page', lambda *a, **k: called.append(a))
    ctx, _ = context()
    ctx.cfg.windows_tools_enabled = False
    result = tool.run(args, ctx)
    assert result.success is False and 'windows_tools_enabled' in (result.error_message or '').casefold() \
        or 'disabled' in (result.error_message or '').casefold()
    assert called == []


@pytest.mark.parametrize('args', [{}, {'action': 'explode'}, {'action': 'open_page', 'page': 'sound', 'bogus': 1}])
def test_invalid_arguments_are_rejected(args):
    result, _ = run(SystemSettingsTool(), args)
    assert result.success is False


# --- open_page ---------------------------------------------------------------------------

def test_open_page_launches_the_page_and_reports_it(monkeypatch):
    opened = []
    monkeypatch.setattr(settings_pages, 'open_page', lambda page, launch=None: opened.append(page) or
                        {'action': 'settings_page_opened', 'page': page})
    result, printed = run(SystemSettingsTool(), {'action': 'open_page', 'page': 'bluetooth'})
    assert result.success and opened == ['bluetooth']
    assert data(result) == {'action': 'settings_page_opened', 'page': 'bluetooth'}
    assert printed and printed[0][0] != ' '


def test_open_page_needs_a_page_and_rejects_unknown_ones(monkeypatch):
    result, _ = run(SystemSettingsTool(), {'action': 'open_page'})
    assert result.success is False
    result, _ = run(SystemSettingsTool(), {'action': 'open_page', 'page': 'ms-settings:network'})
    assert result.success is False and 'bluetooth' in result.error_message


# --- brightness -------------------------------------------------------------------------

def fake_brightness(monkeypatch, readings):
    calls = []
    monkeypatch.setattr(brightness, 'read_levels', lambda device=None: calls.append(('get', device)) or readings)
    monkeypatch.setattr(brightness, 'set_levels', lambda percent, device=None: calls.append(('set', percent, device))
                        or readings)
    monkeypatch.setattr(brightness, 'adjust_levels', lambda delta, device=None: calls.append(('adjust', delta, device))
                        or readings)
    return calls


R1 = brightness.Reading(r'\\.\DISPLAY1', 1, 'ddc', 40)
R2 = brightness.Reading(r'\\.\DISPLAY2', 2, 'ddc', 70)
BAD = brightness.Reading(r'\\.\DISPLAY17', 17, 'ddc', None, False, 'This monitor does not support DDC/CI.')


def test_brightness_defaults_to_get_and_to_set_when_a_percent_is_given(monkeypatch):
    calls = fake_brightness(monkeypatch, [R1, R2])
    run(SystemSettingsTool(), {'action': 'brightness'})
    run(SystemSettingsTool(), {'action': 'brightness', 'percent': 40})
    assert calls == [('get', None), ('set', 40, None)]


def test_brightness_set_reports_every_monitor_and_flags_the_unsupported_one(monkeypatch):
    fake_brightness(monkeypatch, [R1, BAD])
    result, _ = run(SystemSettingsTool(), {'action': 'brightness', 'operation': 'set', 'percent': 40})
    assert result.success
    body = data(result)
    assert body['monitors'] == [{'display': 1, 'device': r'\\.\DISPLAY1', 'percent': 40, 'method': 'ddc',
                                 'verified': True}]
    assert body['unsupported'][0]['display'] == 17 and 'DDC/CI' in body['unsupported'][0]['reason']


def test_brightness_fails_honestly_when_no_monitor_supports_it(monkeypatch):
    fake_brightness(monkeypatch, [BAD])
    result, _ = run(SystemSettingsTool(), {'action': 'brightness', 'percent': 40})
    assert result.success is False and 'DDC/CI' in data(result)['unsupported'][0]['reason']
    assert 'brightness' in result.error_message.casefold()


def test_brightness_up_and_down_adjust_relatively_with_a_default_step(monkeypatch):
    calls = fake_brightness(monkeypatch, [R1])
    run(SystemSettingsTool(), {'action': 'brightness', 'operation': 'up'})
    run(SystemSettingsTool(), {'action': 'brightness', 'operation': 'down', 'amount': 25})
    assert calls == [('adjust', 10, None), ('adjust', -25, None)]


def test_brightness_can_target_a_monitor_by_number(monkeypatch):
    calls = fake_brightness(monkeypatch, [R2])
    monitors = [displays.Monitor(r'\\.\DISPLAY1', (0, 0, 10, 10), (0, 0, 10, 10), True),
                displays.Monitor(r'\\.\DISPLAY2', (10, 0, 20, 10), (10, 0, 20, 10), False)]
    monkeypatch.setattr(displays, 'list_monitors', lambda: monitors)
    run(SystemSettingsTool(), {'action': 'brightness', 'percent': 10, 'monitor': '2'})
    assert calls == [('set', 10, r'\\.\DISPLAY2')]


def test_brightness_set_without_a_percent_and_bad_values_are_rejected(monkeypatch):
    calls = fake_brightness(monkeypatch, [R1])
    result, _ = run(SystemSettingsTool(), {'action': 'brightness', 'operation': 'set'})
    assert result.success is False
    monkeypatch.setattr(brightness, 'set_levels', lambda percent, device=None: brightness._checked_percent(percent))
    result, _ = run(SystemSettingsTool(), {'action': 'brightness', 'percent': 250})
    assert result.success is False and calls == []


# --- power plans ----------------------------------------------------------------------

PLANS = [power.PowerPlan('381b4222-f694-41f0-9685-ff5bb260df2e', 'Balanced', True),
         power.PowerPlan('8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c', 'High performance', False)]


def test_power_plan_defaults_to_get_and_lists_on_request(monkeypatch):
    monkeypatch.setattr(power, 'list_plans', lambda: PLANS)
    result, _ = run(SystemSettingsTool(), {'action': 'power_plan'})
    assert data(result) == {'action': 'power_plan', 'active': 'Balanced'}
    result, _ = run(SystemSettingsTool(), {'action': 'power_plan', 'operation': 'list'})
    assert [p['name'] for p in data(result)['plans']] == ['Balanced', 'High performance']
    assert data(result)['plans'][0]['active'] is True


def test_power_plan_set_switches_by_name_or_key(monkeypatch):
    chosen = []
    monkeypatch.setattr(power, 'set_plan', lambda query: chosen.append(query) or
                        power.PowerPlan(PLANS[1].guid, 'High performance', True))
    result, _ = run(SystemSettingsTool(), {'action': 'power_plan', 'name': 'high_performance'})
    assert chosen == ['high_performance']
    assert data(result) == {'action': 'power_plan', 'active': 'High performance', 'changed': True}


def test_power_plan_errors_are_reported_not_raised(monkeypatch):
    def missing(query):
        raise ValueError('That power plan is not installed. Available plans: Balanced')
    monkeypatch.setattr(power, 'set_plan', missing)
    result, _ = run(SystemSettingsTool(), {'action': 'power_plan', 'name': 'ultimate_performance'})
    assert result.success is False and 'Balanced' in result.error_message


# --- audio output -----------------------------------------------------------------------

SPEAKERS = audio_outputs.OutputDevice('a', 'Speakers (Realtek(R) Audio)', True)
HEADSET = audio_outputs.OutputDevice('b', 'Headset (Razer Kraken)', False)


def test_audio_output_defaults_to_list_and_set_when_a_name_is_given(monkeypatch):
    monkeypatch.setattr(audio_outputs, 'list_outputs', lambda: [SPEAKERS, HEADSET])
    seen = []
    monkeypatch.setattr(audio_outputs, 'set_default_output', lambda name, aliases: seen.append((name, aliases)) or
                        audio_outputs.OutputDevice('b', HEADSET.name, True))
    result, _ = run(SystemSettingsTool(), {'action': 'audio_output'})
    assert data(result)['outputs'] == [{'name': SPEAKERS.name, 'default': True}, {'name': HEADSET.name, 'default': False}]
    result, _ = run(SystemSettingsTool(), {'action': 'audio_output', 'name': 'headset'},
                    windows_audio_aliases={'cans': 'Headset'})
    assert seen == [('headset', {'cans': 'Headset'})]
    assert data(result) == {'action': 'audio_output', 'default': HEADSET.name, 'changed': True}


def test_audio_output_unknown_device_is_an_error_with_the_available_names(monkeypatch):
    def unknown(name, aliases):
        raise ValueError('No active audio output matches that. Available outputs: Speakers')
    monkeypatch.setattr(audio_outputs, 'set_default_output', unknown)
    result, _ = run(SystemSettingsTool(), {'action': 'audio_output', 'name': 'tv'})
    assert result.success is False and 'Speakers' in result.error_message


# --- night light and do not disturb ------------------------------------------------------

@pytest.mark.parametrize('action,page', [('night_light', 'night_light'), ('do_not_disturb', 'focus')])
def test_toggles_without_a_supported_api_open_the_settings_page_and_say_so(action, page, monkeypatch):
    opened = []
    monkeypatch.setattr(settings_pages, 'open_page', lambda p, launch=None: opened.append(p) or
                        {'action': 'settings_page_opened', 'page': p})
    result, _ = run(SystemSettingsTool(), {'action': action})
    assert result.success and opened == [page]
    body = data(result)
    assert body['changed'] is False and 'settings page' in body['note'].casefold()


# --- inputControl ---------------------------------------------------------------------------

def test_hotkey_sends_the_normalised_chord(monkeypatch):
    sent = []
    monkeypatch.setattr(input_control, 'send_chord', lambda chord, sender=None: sent.append(chord) or
                        {'action': 'hotkey_sent', 'keys': '+'.join(chord)})
    result, _ = run(InputControlTool(), {'action': 'hotkey', 'keys': 'Control+Shift+Escape'})
    assert sent == [('ctrl', 'shift', 'esc')] and data(result)['keys'] == 'ctrl+shift+esc'


@pytest.mark.parametrize('args', [{'action': 'hotkey'}, {'action': 'hotkey', 'keys': 'ctrl+nonsense'},
                                  {'action': 'hotkey', 'keys': 'ctrl+alt+delete'}])
def test_bad_hotkeys_send_nothing(args, monkeypatch):
    sent = []
    monkeypatch.setattr(input_control, 'send_chord', lambda chord, sender=None: sent.append(chord))
    result, _ = run(InputControlTool(), args)
    assert result.success is False and sent == []


def test_a_blocked_injection_is_reported_honestly(monkeypatch):
    def blocked(chord, sender=None):
        raise OSError('Windows blocked the key press.')
    monkeypatch.setattr(input_control, 'send_chord', blocked)
    result, _ = run(InputControlTool(), {'action': 'hotkey', 'keys': 'ctrl+c'})
    assert result.success is False and 'blocked' in result.error_message


class Board:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


@pytest.fixture
def board():
    fake = Board('plain note')
    input_control.set_clipboard_backend(fake)
    yield fake
    input_control.set_clipboard_backend(None)


def test_clipboard_read_returns_redacted_text(board):
    board.text = 'call me on jane.doe@example.com about it'
    result, _ = run(InputControlTool(), {'action': 'clipboard_read'})
    assert result.success
    assert 'jane.doe@example.com' not in result.reply_text
    assert 'about it' in result.reply_text


def test_clipboard_write_sets_text_and_reports_only_the_length(board):
    result, _ = run(InputControlTool(), {'action': 'clipboard_write', 'text': 'hello world'})
    assert board.text == 'hello world' and data(result) == {'action': 'clipboard_written', 'length': 11}
    assert 'hello world' not in result.reply_text


@pytest.mark.parametrize('args', [{'action': 'clipboard_read'}, {'action': 'clipboard_write', 'text': 'x'}])
def test_a_clipboard_held_by_a_hung_app_fails_in_time(monkeypatch, args):
    """GetClipboardData can block on a hung owner's delayed rendering; the listener must not freeze."""
    import threading
    import time
    from jarvis.tools.builtin.windows import input_control as tool_module
    release = threading.Event()

    class HungBoard:
        def get_text(self):
            release.wait(5)
            return ''

        def set_text(self, text):
            release.wait(5)

    monkeypatch.setattr(tool_module, '_CLIPBOARD_TIMEOUT_SEC', 0.2)
    input_control.set_clipboard_backend(HungBoard())
    try:
        started = time.monotonic()
        result, _ = run(InputControlTool(), args)
        assert time.monotonic() - started < 2
        assert result.success is False
    finally:
        release.set()
        input_control.set_clipboard_backend(None)


def test_clipboard_write_without_text_changes_nothing(board):
    result, _ = run(InputControlTool(), {'action': 'clipboard_write'})
    assert result.success is False and board.text == 'plain note'


# --- safety classification ----------------------------------------------------------------

def tier(args, tool=None):
    from jarvis.tools.confirmation import evaluate_safety
    return evaluate_safety('inputControl', args, SimpleNamespace(), tool=tool or InputControlTool()).tier.value


@pytest.mark.parametrize('keys', ['alt+f4', 'Alt + F4', 'ctrl+w', 'shift+delete', 'delete', 'ctrl+shift+delete'])
def test_hotkeys_that_close_or_delete_need_confirmation(keys):
    assert tier({'action': 'hotkey', 'keys': keys}) == 'CONFIRM_VOICE'


@pytest.mark.parametrize('action', ['Hotkey', 'HOTKEY', ' hotkey ', 'hotkey\n'])
def test_the_action_is_read_as_the_tool_runs_it_when_classifying(action):
    """The tool runs any casing or padding of 'hotkey', so the safety check must read it the same way."""
    assert tier({'action': action, 'keys': 'alt+f4'}) == 'CONFIRM_VOICE'


@pytest.mark.parametrize('args', [{'action': 'hotkey', 'keys': 'ctrl+shift+esc'}, {'action': 'hotkey', 'keys': 'win+d'},
                                  {'action': 'clipboard_read'}, {'action': 'clipboard_write', 'text': 'x'},
                                  {'action': 'hotkey', 'keys': 'not a key'}])
def test_ordinary_input_actions_are_routine(args):
    assert tier(args) == 'SAFE'


def test_system_settings_is_routine_for_every_action():
    from jarvis.tools.confirmation import evaluate_safety
    for args in ({'action': 'brightness', 'percent': 10}, {'action': 'power_plan', 'name': 'balanced'},
                 {'action': 'audio_output', 'name': 'x'}, {'action': 'open_page', 'page': 'sound'}):
        assert evaluate_safety('systemSettings', args, SimpleNamespace(), tool=SystemSettingsTool()).tier.value == 'SAFE'


def test_the_tools_register_with_the_windows_catalogue(monkeypatch):
    from jarvis.config import load_settings
    from jarvis.tools import registry
    original = dict(registry.BUILTIN_TOOLS)
    try:
        registry.configure_windows_tools(load_settings(), platform='win32', start_index=False)
        assert {'systemSettings', 'inputControl'} <= registry.BUILTIN_TOOLS.keys()
        registry.configure_windows_tools(SimpleNamespace(windows_tools_enabled=False, windows_workspaces={}),
                                         platform='win32', start_index=False)
        assert not {'systemSettings', 'inputControl'} & registry.BUILTIN_TOOLS.keys()
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def test_a_confirmed_hotkey_is_bound_to_its_exact_chord_and_runs_once(monkeypatch):
    """The confirmation names the chord, authorises only that chord, and is consumed by one run."""
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety, get_confirmation_store
    store = get_confirmation_store()
    store.clear_pending()
    args = {'action': 'hotkey', 'keys': 'alt+f4'}
    request = evaluate_safety('inputControl', args, SimpleNamespace(), tool=InputControlTool())
    assert request.tier == SafetyTier.CONFIRM_VOICE and 'alt+f4' in request.action
    assert request.target == '' and request.consequence
    store.set_pending(request)
    store.get_pending().authorised = True
    other = evaluate_safety('inputControl', {'action': 'hotkey', 'keys': 'ctrl+w'}, SimpleNamespace(),
                            tool=InputControlTool())
    assert not store.is_authorised(other.tool_name, other.action, other.target, other.parameters)
    assert store.consume_authorisation(request.tool_name, request.action, request.target, request.parameters)
    assert not store.consume_authorisation(request.tool_name, request.action, request.target, request.parameters)
    store.clear_pending()

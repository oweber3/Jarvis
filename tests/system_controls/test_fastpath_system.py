"""Fast commands for settings, brightness, power, audio output, desktops and hotkeys."""
import json
from pathlib import Path

import pytest

from jarvis.fastpath import matcher
from jarvis.fastpath.matcher import match, normalise

TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'systemInfo', 'openPath',
         'systemSettings', 'inputControl'}


def fast(text, **kwargs):
    return match(text, 'en', available_tools=TOOLS, **kwargs)


def call(text):
    result = fast(text)
    return (result.tool_name, result.args) if result else None


@pytest.mark.parametrize('text,page', [
    ('Open Bluetooth settings', 'bluetooth'), ('open the display settings', 'display'),
    ('Open sound settings', 'sound'), ('open wifi settings', 'wifi'), ('Open Wi-Fi settings', 'wifi'),
    ('open my privacy settings', 'privacy'), ('go to power settings', 'power'),
    ('open windows update settings', 'updates'), ('open night light settings', 'night_light'),
    ('show the apps settings', 'apps'), ('open Bluetooth settings please', 'bluetooth'),
])
def test_settings_pages_open_by_locale_name(text, page):
    assert call(text) == ('systemSettings', {'action': 'open_page', 'page': page})


@pytest.mark.parametrize('text', ['open nonsense settings', 'open settings', 'open bluetooth settings and wifi settings',
                                  'what are bluetooth settings', 'close bluetooth settings'])
def test_unknown_or_compound_settings_requests_fall_through(text):
    assert call(text) is None


@pytest.mark.parametrize('text,args', [
    ('Set brightness to 40%', {'action': 'brightness', 'operation': 'set', 'percent': 40}),
    ('set the brightness to 75 percent', {'action': 'brightness', 'operation': 'set', 'percent': 75}),
    ('set screen brightness to 10', {'action': 'brightness', 'operation': 'set', 'percent': 10}),
    ('Increase the brightness', {'action': 'brightness', 'operation': 'up'}),
    ('turn the brightness down', {'action': 'brightness', 'operation': 'down'}),
    ('whats the brightness', {'action': 'brightness', 'operation': 'get'}),
    ('Make the screen a bit dimmer.', {'action': 'brightness', 'operation': 'down'}),
    ('make the screen brighter', {'action': 'brightness', 'operation': 'up'}),
    ('Brighten the screen', {'action': 'brightness', 'operation': 'up'}),
])
def test_brightness_commands(text, args):
    assert call(text) == ('systemSettings', args)


@pytest.mark.parametrize('text', ['set brightness to 140%', 'set brightness to high', 'set brightness to -5',
                                  'should I make the screen dimmer', 'make the screen dimmer at night'])
def test_brightness_out_of_range_or_non_numeric_falls_through(text):
    assert call(text) is None


@pytest.mark.parametrize('text,args', [
    ('Switch to the balanced power plan', {'action': 'power_plan', 'operation': 'set', 'name': 'balanced'}),
    ('set power plan to high performance', {'action': 'power_plan', 'operation': 'set', 'name': 'high_performance'}),
    ('use the power saver power plan', {'action': 'power_plan', 'operation': 'set', 'name': 'power_saver'}),
    ('switch to ultimate performance power plan', {'action': 'power_plan', 'operation': 'set',
                                                   'name': 'ultimate_performance'}),
    ('what power plan am i using', {'action': 'power_plan', 'operation': 'get'}),
])
def test_power_plan_commands(text, args):
    assert call(text) == ('systemSettings', args)


def test_an_unknown_power_plan_falls_through():
    assert call('switch to the turbo power plan') is None


@pytest.mark.parametrize('text,name', [
    ('Switch audio to my headphones', 'headphones'), ('switch the audio output to speakers', 'speakers'),
    ('change audio output to the monitor', 'monitor'), ('switch sound to headset', 'headset'),
])
def test_audio_output_switch_passes_the_spoken_device_name(text, name):
    assert call(text) == ('systemSettings', {'action': 'audio_output', 'operation': 'set', 'name': name})


@pytest.mark.parametrize('text,args', [
    ('Switch to desktop 2', {'action': 'desktop_switch', 'target': '', 'desktop': '2'}),
    ('go to desktop three', {'action': 'desktop_switch', 'target': '', 'desktop': '3'}),
    ('switch to virtual desktop 1', {'action': 'desktop_switch', 'target': '', 'desktop': '1'}),
    ('next desktop', {'action': 'desktop_switch', 'target': '', 'desktop': 'next'}),
    ('go to the previous desktop', {'action': 'desktop_switch', 'target': '', 'desktop': 'previous'}),
    ('new desktop', {'action': 'desktop_new', 'target': ''}),
    ('create a new desktop', {'action': 'desktop_new', 'target': ''}),
])
def test_virtual_desktop_commands(text, args):
    assert call(text) == ('windowControl', args)


@pytest.mark.parametrize('text', ['switch to desktop', 'switch to desktop 99999', 'switch to desktop banana',
                                  'switch to desktop 2 and open word', 'switch to desktop zero',
                                  'switch to desktop 0'])
def test_bad_desktop_requests_fall_through(text):
    assert call(text) is None


@pytest.mark.parametrize('text,keys', [
    ('Press control shift escape', 'ctrl+shift+esc'), ('press alt tab', 'alt+tab'),
    ('press windows d', 'win+d'), ('press control plus c', 'ctrl+c'), ('hit f11', 'f11'),
    ('press ctrl+shift+esc', 'ctrl+shift+esc'), ('press enter', 'enter'),
    # "plus" joins keys, but with no key after it, it is the key itself (zoom in)
    ('press control plus', 'ctrl+equals'), ('press control plus plus', 'ctrl+equals'),
    ('press control minus', 'ctrl+minus'),
])
def test_press_commands_build_a_canonical_chord(text, keys):
    assert call(text) == ('inputControl', {'action': 'hotkey', 'keys': keys})


@pytest.mark.parametrize('text', ['press', 'press nonsense', 'press control control', 'press a b', 'press control alt delete',
                                  'press control and c', 'press plus c'])
def test_malformed_or_unsendable_chords_fall_through(text):
    assert call(text) is None


@pytest.mark.parametrize('text,keys', [
    ('show the desktop', 'win+d'), ('undo', 'ctrl+z'), ('redo', 'ctrl+y'), ('copy', 'ctrl+c'), ('paste', 'ctrl+v'),
    ('select all', 'ctrl+a'), ('save this', 'ctrl+s'), ('open task view', 'win+tab'),
])
def test_common_hotkey_phrases(text, keys):
    assert call(text) == ('inputControl', {'action': 'hotkey', 'keys': keys})


def test_destructive_press_requests_are_matched_but_never_pass_the_safety_gate():
    from types import SimpleNamespace
    from jarvis.tools.builtin.windows.input_control import InputControlTool
    from jarvis.tools.confirmation import evaluate_safety
    result = fast('press alt f4')
    assert result is not None
    tier = evaluate_safety(result.tool_name, result.args, SimpleNamespace(), tool=InputControlTool()).tier.value
    assert tier == 'CONFIRM_VOICE'


@pytest.mark.parametrize('text', ['switch audio to my headphones', 'press control c', 'set brightness to 40%',
                                  'open bluetooth settings', 'switch to desktop 2'])
def test_routes_need_their_tool_to_be_available(text):
    assert match(text, 'en', available_tools={'getTime'}) is None


def test_locale_tables_are_normalised_so_lookups_can_match():
    data = json.loads((Path(matcher.__file__).parent / 'phrases' / 'en.json').read_text(encoding='utf-8'))
    for table in ('settings_pages', 'power_plans', 'keys', 'numbers'):
        for phrase in data[table]:
            assert normalise(phrase) == phrase, f'{table}: {phrase!r} is not in normalised form'


def test_locale_pages_all_exist_in_the_platform_page_table():
    from jarvis.platform.windows import settings_pages
    data = json.loads((Path(matcher.__file__).parent / 'phrases' / 'en.json').read_text(encoding='utf-8'))
    assert set(data['settings_pages'].values()) <= set(settings_pages.load_pages())


def test_locale_power_plans_are_known_keys():
    from jarvis.platform.windows import power
    data = json.loads((Path(matcher.__file__).parent / 'phrases' / 'en.json').read_text(encoding='utf-8'))
    assert set(data['power_plans'].values()) <= set(power.WELL_KNOWN)


def test_locale_keys_are_known_to_the_input_layer():
    from jarvis.platform.windows import input_control
    data = json.loads((Path(matcher.__file__).parent / 'phrases' / 'en.json').read_text(encoding='utf-8'))
    for spoken, key in data['keys'].items():
        assert input_control.canonical_key(key) == key, spoken


def test_no_slot_names_are_hardcoded_outside_the_registry():
    source = Path(matcher.__file__).read_text(encoding='utf-8')
    assert "('app', 'folder', 'percent', 'workspace')" not in source
    assert set(matcher.SLOTS) >= {'app', 'folder', 'percent', 'workspace', 'page', 'plan', 'number', 'keys', 'device'}


def test_every_rule_slot_is_a_registered_slot():
    import re
    data = json.loads((Path(matcher.__file__).parent / 'phrases' / 'en.json').read_text(encoding='utf-8'))
    for rule in data['rules']:
        for phrase in rule['phrases']:
            for slot in re.findall(r'\{(\w+)\}', phrase):
                assert slot in matcher.SLOTS, f'{rule["id"]}: unknown slot {slot}'


# --- through the dispatcher and the central safety policy ---------------------------------------

@pytest.fixture
def windows_catalogue(monkeypatch):
    from jarvis.platform.windows import apps
    from jarvis.tools import registry
    monkeypatch.setattr(apps.APP_INDEX, 'snapshot', lambda: ())
    monkeypatch.setattr(apps.APP_INDEX, 'start', lambda: None)
    from jarvis.config import load_settings
    cfg = load_settings()
    original = dict(registry.BUILTIN_TOOLS)
    registry.configure_windows_tools(cfg, platform='win32', start_index=False)
    yield cfg
    registry.BUILTIN_TOOLS.clear()
    registry.BUILTIN_TOOLS.update(original)


@pytest.mark.parametrize('text,tool', [
    ('open bluetooth settings', 'systemSettings'), ('set brightness to 40%', 'systemSettings'),
    ('switch to desktop 2', 'windowControl'), ('press control shift escape', 'inputControl'),
    ('switch to the balanced power plan', 'systemSettings'), ('copy', 'inputControl'),
])
def test_routine_commands_pass_the_dispatcher_gate(windows_catalogue, text, tool):
    from jarvis.fastpath.dispatcher import match_command
    result = match_command(text, windows_catalogue, 'en')
    assert result is not None and result.tool_name == tool


@pytest.mark.parametrize('text', ['press alt f4', 'press delete', 'press control w'])
def test_closing_or_deleting_hotkeys_are_not_fast_routed(windows_catalogue, text):
    from jarvis.fastpath.dispatcher import match_command
    assert match_command(text, windows_catalogue, 'en') is None


def test_switching_windows_tools_off_disables_every_new_route(windows_catalogue):
    from dataclasses import replace
    from jarvis.fastpath.dispatcher import match_command
    off = replace(windows_catalogue, windows_tools_enabled=False)
    for text in ('open bluetooth settings', 'press control c', 'switch to desktop 2', 'set brightness to 30%'):
        assert match_command(text, off, 'en') is None

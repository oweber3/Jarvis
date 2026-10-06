"""Display identity, virtual desktop, natural monitor references and FancyZones zone resolution.

The operating system is replaced by plain values; the FancyZones files are the anonymised
fixtures. A live smoke test at the end exercises the real Win32 identity lookup on Windows.
"""
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from jarvis.platform.windows import displays, fancyzones as fz
from jarvis.platform.windows.displays import Monitor

pytestmark = pytest.mark.unit

# The suite-wide fixture hides the developer's PowerToys folder; keep the real lookup to test it.
REAL_DIRECTORY = displays.fancyzones_directory

FIXTURES = Path(__file__).parent / 'fixtures' / 'fancyzones'
DESKTOP = uuid.UUID('{00000000-0000-0000-0000-0000FACE0001}')

MAIN = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True,
               'TST1001', '5&00000001&0&UID4301', 'SN00000001')
PORTRAIT = Monitor(r'\\.\DISPLAY2', (-1080, -162, 0, 1758), (-1080, -162, 0, 1710), False,
                   'TST1002', '5&00000004&0&UID4304', 'SN00000002')
SMALL = Monitor(r'\\.\DISPLAY17', (2560, 0, 3360, 600), (2560, 0, 3360, 552), False)
ALL = [MAIN, PORTRAIT, SMALL]


# --- identity helpers ----------------------------------------------------------------

@pytest.mark.parametrize('interface, expected', [
    (r'\\?\DISPLAY#GSM5CE4#5&18296720&0&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}',
     ('GSM5CE4', '5&18296720&0&UID4353')),
    (r'\\?\DISPLAY#Default_Monitor#1&1efbcd86&0&UID256#{e6f07b5f}', ('Default_Monitor', '1&1efbcd86&0&UID256')),
    (r'\\?\DISPLAY#GSM5CE4', ('', '')),
    ('', ('', '')),
    ('no separators', ('', '')),
])
def test_interface_names_split_into_monitor_id_and_instance(interface, expected):
    assert displays.parse_interface_name(interface) == expected


def edid(serial_text=None, tag=0xFF):
    data = bytearray(128)
    if serial_text is not None:
        payload = (serial_text.encode('ascii') + bytes([0x0A])).ljust(13, b' ')[:13]
        data[54:72] = bytes([0, 0, 0, tag, 0]) + payload
    return bytes(data)


@pytest.mark.parametrize('blob, expected', [
    (edid('509BNKP15909'), '509BNKP15909'),
    (edid('AB12'), 'AB12'),
    (edid('X', tag=0xFC), ''),   # a monitor-name descriptor, not a serial
    (edid(), ''),
    (b'', ''),
    (b'\x00' * 10, ''),
    (None, ''),
])
def test_edid_serial_text_is_read_from_the_serial_descriptor(blob, expected):
    assert displays.parse_edid_serial(blob) == expected


def test_registry_guid_bytes_are_little_endian_fields():
    raw = bytes.fromhex('f49efc2dffd6894293c7fa6b2efc561f')
    assert displays.guid_from_registry(raw) == uuid.UUID('{2DFC9EF4-D6FF-4289-93C7-FA6B2EFC561F}')
    assert displays.guid_from_registry(b'short') is None
    assert displays.guid_from_registry(None) is None


def test_current_virtual_desktop_prefers_the_explorer_key_then_session_then_the_first_listed(monkeypatch):
    first = uuid.UUID(int=1).bytes_le
    second = uuid.UUID(int=2).bytes_le
    third = uuid.UUID(int=3).bytes_le
    values = {('VirtualDesktops', 'CurrentVirtualDesktop'): first,
              ('SessionInfo', 'CurrentVirtualDesktop'): second,
              ('VirtualDesktops', 'VirtualDesktopIDs'): third + first}
    monkeypatch.setattr(displays, '_registry_bytes', lambda kind, name: values.get((kind, name)))
    assert displays.current_virtual_desktop() == uuid.UUID(int=1)
    del values[('VirtualDesktops', 'CurrentVirtualDesktop')]
    assert displays.current_virtual_desktop() == uuid.UUID(int=2)
    del values[('SessionInfo', 'CurrentVirtualDesktop')]
    assert displays.current_virtual_desktop() == uuid.UUID(int=3)
    values.clear()
    assert displays.current_virtual_desktop() == uuid.UUID(int=0)  # a single, unnamed desktop


def test_display_number_comes_from_the_device_name():
    assert MAIN.number == 1 and SMALL.number == 17
    assert Monitor('weird', (0, 0, 1, 1), (0, 0, 1, 1), False).number == 0


# --- natural monitor references ------------------------------------------------------

@pytest.mark.parametrize('reference, expected', [
    ('1', MAIN), ('2', PORTRAIT), ('3', SMALL),          # Windows display-number order
    (' 2 ', PORTRAIT), ('02', PORTRAIT),
    ('primary', MAIN), ('PRIMARY', MAIN), (' Primary ', MAIN),
    ('left', PORTRAIT), ('Left', PORTRAIT),              # physical position, not number
    ('right', SMALL),
])
def test_monitors_can_be_named_by_number_primary_or_position(reference, expected):
    assert displays.resolve_monitor(reference, ALL, {}) == expected


def test_numbering_follows_display_numbers_even_when_they_have_gaps():
    # DISPLAY17 is the third display, so "3", and the exact identifier still works.
    assert displays.resolve_monitor('3', [SMALL, MAIN, PORTRAIT], {}) == SMALL
    assert displays.resolve_monitor(SMALL.device, ALL, {}) == SMALL


@pytest.mark.parametrize('reference', ['0', '4', '17', '-1', '2.0', '٢', 'second', 'centre', 'top', '', '  '])
def test_unknown_references_never_pick_another_display(reference):
    with pytest.raises(ValueError):
        displays.resolve_monitor(reference, ALL, {})


def test_left_and_right_need_two_displays_and_a_clear_winner():
    with pytest.raises(ValueError, match='more than one'):
        displays.resolve_monitor('left', [MAIN], {})
    stacked_a = Monitor(r'\\.\DISPLAY1', (0, 0, 1000, 800), (0, 0, 1000, 760), True)
    stacked_b = Monitor(r'\\.\DISPLAY2', (0, 800, 1000, 1600), (0, 800, 1000, 1560), False)
    with pytest.raises(ValueError, match='Ambiguous'):
        displays.resolve_monitor('left', [stacked_a, stacked_b], {})
    with pytest.raises(ValueError, match='Ambiguous'):
        displays.resolve_monitor('right', [stacked_a, stacked_b], {})


def test_position_uses_the_centre_so_mixed_sizes_still_order_sensibly():
    wide = Monitor(r'\\.\DISPLAY1', (0, 0, 3440, 1440), (0, 0, 3440, 1400), True)       # centre 1720
    tall = Monitor(r'\\.\DISPLAY2', (3440, -200, 4520, 1720), (3440, -200, 4520, 1680), False)  # centre 3980
    assert displays.resolve_monitor('left', [tall, wide], {}) == wide
    assert displays.resolve_monitor('right', [tall, wide], {}) == tall


def test_primary_needs_exactly_one_primary_display():
    with pytest.raises(ValueError):
        displays.resolve_monitor('primary', [PORTRAIT, SMALL], {})


def test_configured_aliases_and_identifiers_beat_the_built_in_references():
    aliases = {'left': MAIN.device, 'primary': SMALL.device, '2': SMALL.device}
    assert displays.resolve_monitor('left', ALL, aliases) == MAIN
    assert displays.resolve_monitor('PRIMARY', ALL, aliases) == SMALL
    assert displays.resolve_monitor('2', ALL, aliases) == SMALL
    # An alias whose display is gone is an error, not a fall-through to the keyword.
    with pytest.raises(ValueError, match='not connected'):
        displays.resolve_monitor('left', [MAIN], {'left': r'\\.\DISPLAY9'})


# --- zone resolution -----------------------------------------------------------------

def zone_set(monitor=MAIN):
    sets = displays.fancyzone_sets([monitor], ('en',), directory=FIXTURES, desktop=DESKTOP)
    return sets[monitor.device]


def test_fancyzones_resolve_by_number_and_derived_name_case_insensitively():
    zones = zone_set()
    assert displays.resolve_zone({}, MAIN, '2', zones) == ('2', (1280, 0, 2560, 1392))
    assert displays.resolve_zone({}, MAIN, 'LEFT', zones) == ('left', (0, 0, 1280, 1392))
    assert displays.resolve_zone({}, MAIN, ' right ', zones) == ('right', (1280, 0, 2560, 1392))


def test_configured_zones_take_precedence_over_fancyzones_with_the_same_name():
    zones = zone_set()
    configured = {MAIN.device: {'left': [0, 0, 0.25, 1], 'Extra': [0.25, 0, 0.75, 1]}}
    assert displays.resolve_zone(configured, MAIN, 'left', zones) == ('left', (0, 0, 640, 1392))
    assert displays.resolve_zone(configured, MAIN, 'extra', zones) == ('Extra', (640, 0, 2560, 1392))
    assert displays.resolve_zone(configured, MAIN, 'right', zones) == ('right', (1280, 0, 2560, 1392))
    shadow_number = {MAIN.device: {'1': [0.5, 0, 0.5, 1]}}
    assert displays.resolve_zone(shadow_number, MAIN, '1', zones) == ('1', (1280, 0, 2560, 1392))
    assert displays.resolve_zone(shadow_number, MAIN, '2', zones) == ('2', (1280, 0, 2560, 1392))


def test_unknown_zones_list_every_usable_name_and_never_guess():
    zones = zone_set()
    configured = {MAIN.device: {'left': [0, 0, 0.25, 1]}}
    with pytest.raises(ValueError) as caught:
        displays.resolve_zone(configured, MAIN, 'centre', zones)
    message = str(caught.value)
    assert 'Available zones' in message and 'left' in message and '1' in message and '2' in message
    with pytest.raises(ValueError, match='none'):
        displays.resolve_zone({}, MAIN, 'left', None)
    for label in ('', '  ', '3', '0', 'lef'):
        with pytest.raises(ValueError):
            displays.resolve_zone({}, MAIN, label, zones)


def test_behaviour_without_fancyzones_is_unchanged():
    configured = {MAIN.device: {'left': [0, 0, 0.5, 1]}}
    assert displays.resolve_zone(configured, MAIN, 'LEFT', None) == ('left', (0, 0, 1280, 1392))
    with pytest.raises(ValueError, match='Available zones: left'):
        displays.resolve_zone(configured, MAIN, '1', None)


# --- listing -------------------------------------------------------------------------

def test_listing_adds_numbers_and_fancyzones_without_changing_configured_fields():
    zones = zone_set()
    configured = {MAIN.device: {'left': [0, 0, 0.25, 1]}}
    listing = displays.describe_displays(ALL, {'main': MAIN.device}, configured, {MAIN.device: zones})
    by_device = {entry['device']: entry for entry in listing['displays']}
    assert [entry['number'] for entry in listing['displays']] == [1, 2, 3]
    main = by_device[MAIN.device]
    assert main['zones'] == {'left': [0, 0, 640, 1392]} and main['aliases'] == ['main']
    # The configured "left" shadows FancyZones' "left", so only the number reaches zone 1.
    assert main['fancyzones'] == {'layout': 'columns', 'zones': [
        {'names': ['1'], 'rectangle': [0, 0, 1280, 1392]},
        {'names': ['2', 'right'], 'rectangle': [1280, 0, 2560, 1392]}]}
    assert 'fancyzones' not in by_device[PORTRAIT.device]


def test_listing_without_fancyzones_has_the_original_shape_plus_number():
    listing = displays.describe_displays([MAIN], {}, {})
    assert listing == {'displays': [{'device': MAIN.device, 'number': 1, 'primary': True,
                                     'bounds': list(MAIN.bounds), 'work_area': list(MAIN.work_area),
                                     'aliases': [], 'zones': {}}]}


# --- reading the real folder and failing open ----------------------------------------

def test_fancyzone_sets_match_monitors_by_identity(tmp_path):
    folder = tmp_path / 'FancyZones'
    shutil.copytree(FIXTURES, folder)
    sets = displays.fancyzone_sets(ALL, ('en',), directory=folder, desktop=DESKTOP)
    assert {device: zones.layout for device, zones in sets.items()} == {
        MAIN.device: 'columns', PORTRAIT.device: 'rows', SMALL.device: 'priority-grid'}
    assert sets[PORTRAIT.device].names == (('1', 'top'), ('2', 'bottom'))


@pytest.mark.parametrize('prepare', ['missing', 'empty', 'garbage'])
def test_missing_or_unreadable_data_means_no_fancyzones(tmp_path, prepare):
    folder = tmp_path / 'FancyZones'
    if prepare != 'missing':
        folder.mkdir()
    if prepare == 'garbage':
        (folder / 'applied-layouts.json').write_text('\x00 not json', encoding='utf-8')
    assert displays.fancyzone_sets(ALL, ('en',), directory=folder, desktop=DESKTOP) == {}
    assert displays.fancyzone_sets(ALL, ('en',), directory=None, desktop=DESKTOP) == {}


def test_unexpected_failures_fail_open_and_log_no_identifiers(monkeypatch):
    messages = []
    monkeypatch.setattr(displays, 'debug_log', lambda message, *_: messages.append(message))

    def explode(_directory):
        raise RuntimeError(r'C:\Users\someone\secret TST1001')

    monkeypatch.setattr(fz, 'load_data', explode)
    assert displays.fancyzone_sets(ALL, ('en',), directory=FIXTURES, desktop=DESKTOP) == {}
    assert messages and not any(token in ' '.join(messages) for token in ('secret', 'someone', 'TST1001'))


def test_the_default_location_is_powertoys_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    expected = tmp_path / 'Microsoft' / 'PowerToys' / 'FancyZones'
    assert REAL_DIRECTORY() == expected
    monkeypatch.delenv('LOCALAPPDATA')
    assert REAL_DIRECTORY() is None


# --- live smoke test -----------------------------------------------------------------

@pytest.mark.skipif(sys.platform != 'win32', reason='needs the Win32 display APIs')
def test_live_monitors_carry_identity_and_the_desktop_resolves():
    monitors = displays.list_monitors()
    assert monitors and all(monitor.number > 0 for monitor in monitors)
    assert all(isinstance(monitor.hardware_id, str) and isinstance(monitor.instance_id, str) for monitor in monitors)
    assert isinstance(displays.current_virtual_desktop(), uuid.UUID)

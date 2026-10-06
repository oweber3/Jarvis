"""FancyZones layouts as zone rectangles: PowerToys geometry, display matching, zone names.

Expected rectangles are derived by hand from PowerToys' own layout rules (equal-sum
integer division, half-spacing between neighbours, full spacing at the work-area edge),
not from the implementation. The data files are anonymised copies of a real install.
"""
import copy
import json
import uuid
from pathlib import Path

import pytest

from jarvis.platform.windows import fancyzones as fz
from jarvis.platform.windows.fancyzones import DisplayIdentity, DisplayInfo

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / 'fixtures' / 'fancyzones'
DESKTOP = uuid.UUID('{00000000-0000-0000-0000-0000FACE0001}')
OTHER_DESKTOP = uuid.UUID('{00000000-0000-0000-0000-0000FACE0002}')
LABELS = [{'left': 'left', 'right': 'right', 'middle': 'middle', 'top': 'top', 'bottom': 'bottom',
           'top_left': 'top left', 'top_right': 'top right', 'bottom_left': 'bottom left',
           'bottom_right': 'bottom right'}]

MAIN = DisplayInfo(DisplayIdentity(r'\\.\DISPLAY1', 1, 'TST1001', '5&00000001&0&UID4301', 'SN00000001'),
                   (0, 0, 2560, 1440), (0, 0, 2560, 1392))
PORTRAIT = DisplayInfo(DisplayIdentity(r'\\.\DISPLAY2', 2, 'TST1002', '5&00000004&0&UID4304', 'SN00000002'),
                       (-1080, -162, 0, 1758), (-1080, -162, 0, 1710))
SMALL = DisplayInfo(DisplayIdentity(r'\\.\DISPLAY17', 17), (2560, 0, 3360, 600), (2560, 0, 3360, 552))


def fixture_data(**overrides):
    files = {name: json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))
             for name in ('applied-layouts', 'custom-layouts', 'default-layouts', 'settings')}
    files.update(overrides)
    return fz.parse_data(files['applied-layouts'], files['custom-layouts'],
                         files['default-layouts'], files['settings'])


def applied(kind, count, *, spacing=16, show=False, uuid_='{00000000-0000-0000-0000-000000000000}'):
    return {'uuid': uuid_, 'type': kind, 'show-spacing': show, 'spacing': spacing, 'zone-count': count,
            'sensitivity-radius': 20}


def entry(layout, *, monitor='TST1001', instance='5&00000001&0&UID4301', serial='SN00000001', number=1,
          desktop=DESKTOP):
    return {'device': {'monitor': monitor, 'monitor-instance': instance, 'serial-number': serial,
                       'monitor-number': number, 'virtual-desktop': '{%s}' % str(desktop).upper()},
            'applied-layout': layout}


def data_for(*entries, custom=None, defaults=None):
    return fz.parse_data({'applied-layouts': list(entries)}, custom or {'custom-layouts': []},
                         defaults or {'default-layouts': []}, {})


# --- template geometry ---------------------------------------------------------------

@pytest.mark.parametrize('kind, count, size, spacing, expected', [
    ('columns', 2, (2560, 1392), 0, [(0, 0, 1280, 1392), (1280, 0, 2560, 1392)]),
    ('columns', 2, (2560, 1392), 16, [(16, 16, 1272, 1376), (1288, 16, 2544, 1376)]),
    # Sizes are not total / count: the integer remainders go to the last zone so the sum is exact.
    ('columns', 3, (1000, 600), 0, [(0, 0, 333, 600), (333, 0, 666, 600), (666, 0, 1000, 600)]),
    ('rows', 2, (1080, 1872), 0, [(0, 0, 1080, 936), (0, 936, 1080, 1872)]),
    ('rows', 3, (600, 1000), 10, [(10, 10, 590, 330), (10, 340, 590, 660), (10, 670, 590, 990)]),
    ('grid', 4, (1000, 800), 0, [(0, 0, 500, 400), (500, 0, 1000, 400), (0, 400, 500, 800), (500, 400, 1000, 800)]),
    ('grid', 4, (1000, 800), 16, [(16, 16, 492, 392), (508, 16, 984, 392), (16, 408, 492, 784), (508, 408, 984, 784)]),
    # An odd count leaves the last cell short: the final zone spans the remaining columns.
    ('grid', 5, (1000, 800), 0, [(0, 0, 333, 400), (333, 0, 666, 400), (666, 0, 1000, 400),
                                 (0, 400, 333, 800), (333, 400, 1000, 800)]),
    ('priority-grid', 1, (1000, 800), 0, [(0, 0, 1000, 800)]),
    ('priority-grid', 2, (1500, 900), 0, [(0, 0, 1000, 900), (1000, 0, 1500, 900)]),
    ('priority-grid', 3, (800, 552), 16, [(16, 16, 192, 536), (208, 16, 592, 536), (608, 16, 784, 536)]),
    ('priority-grid', 4, (1000, 800), 0, [(0, 0, 250, 800), (250, 0, 750, 800), (750, 0, 1000, 400), (750, 400, 1000, 800)]),
    ('priority-grid', 5, (1000, 800), 0, [(0, 0, 250, 400), (250, 0, 750, 800), (750, 0, 1000, 400),
                                          (0, 400, 250, 800), (750, 400, 1000, 800)]),
    ('focus', 1, (1000, 800), 0, [(100, 100, 500, 420)]),
    ('focus', 3, (1000, 800), 0, [(100, 100, 500, 420), (150, 150, 550, 470), (200, 200, 600, 520)]),
])
def test_template_layouts_match_powertoys_geometry(kind, count, size, spacing, expected):
    assert fz.template_zones(kind, size[0], size[1], count, spacing) == expected


def test_priority_grid_beyond_the_predefined_table_is_a_grid():
    assert fz.template_zones('priority-grid', 1200, 900, 12, 0) == fz.template_zones('grid', 1200, 900, 12, 0)
    assert len(fz.template_zones('grid', 1200, 900, 12, 0)) == 12


@pytest.mark.parametrize('count', [1, 2, 3, 6, 7, 9, 11, 16])
@pytest.mark.parametrize('kind', ['columns', 'rows', 'grid', 'priority-grid'])
def test_templates_always_yield_the_requested_number_of_ordered_zones(kind, count):
    zones = fz.template_zones(kind, 1920, 1040, count, 8)
    assert len(zones) == count
    assert all(right > left and bottom > top for left, top, right, bottom in zones)


@pytest.mark.parametrize('kind, count', [('grid', 0), ('rows', 0), ('columns', -1), ('priority-grid', 0), ('sphere', 2)])
def test_impossible_templates_are_rejected(kind, count):
    with pytest.raises(fz.LayoutError):
        fz.template_zones(kind, 1000, 800, count, 0)


def test_blank_layout_has_no_zones():
    assert fz.template_zones('blank', 1000, 800, 0, 0) == []


# --- custom layouts ------------------------------------------------------------------

def test_custom_grid_uses_its_own_percentages_map_and_spacing():
    layouts = fixture_data().custom
    zones = fz.layout_zones(applied('custom', 4, uuid_='{00000000-0000-0000-0000-0000C0DE0001}'), layouts, 1000, 800)
    assert zones == [(10, 10, 245, 790), (255, 10, 745, 790), (755, 10, 990, 395), (755, 405, 990, 790)]


def test_custom_canvas_scales_from_its_reference_size_and_keeps_zone_order():
    layouts = fixture_data().custom
    zones = fz.layout_zones(applied('custom', 2, uuid_='{00000000-0000-0000-0000-0000C0DE0002}'), layouts, 2000, 1000)
    assert zones == [(0, 0, 1000, 500), (1000, 500, 2000, 1000)]


def test_custom_canvas_truncates_fractional_pixels_like_powertoys():
    custom = {'custom-layouts': [{'uuid': '{00000000-0000-0000-0000-0000C0DE0003}', 'name': 'x', 'type': 'canvas',
                                  'info': {'ref-width': 1000, 'ref-height': 1000,
                                           'zones': [{'X': 333, 'Y': 0, 'width': 334, 'height': 1000}]}}]}
    parsed = fz.parse_data({'applied-layouts': []}, custom, {}, {})
    zones = fz.layout_zones(applied('custom', 1, uuid_='{00000000-0000-0000-0000-0000C0DE0003}'),
                            parsed.custom, 1920, 1000)
    assert zones == [(639, 0, 639 + 641, 1000)]  # 333*1.92=639.36, width 334*1.92=641.28


def test_unknown_or_malformed_custom_layouts_are_rejected():
    with pytest.raises(fz.LayoutError):
        fz.layout_zones(applied('custom', 2, uuid_='{00000000-0000-0000-0000-0000DEAD0000}'), {}, 1000, 800)
    broken = {'custom-layouts': [
        {'uuid': '{00000000-0000-0000-0000-0000C0DE0009}', 'name': 'x', 'type': 'grid',
         'info': {'rows': 2, 'columns': 2, 'rows-percentage': [10000], 'columns-percentage': [5000, 5000],
                  'cell-child-map': [[0, 1], [2, 3]]}}]}
    assert fz.parse_data({'applied-layouts': []}, broken, {}, {}).custom == {}


# --- matching connected displays to applied layouts ----------------------------------

def zone_sets(displays, data, desktop=DESKTOP, labels=LABELS):
    return fz.zone_sets(displays, data, desktop, labels)


def test_real_layouts_become_absolute_rectangles_for_each_connected_display():
    sets = zone_sets([MAIN, PORTRAIT, SMALL], fixture_data())
    assert sets[MAIN.identity.device].layout == 'columns'
    assert sets[MAIN.identity.device].rectangles == ((0, 0, 1280, 1392), (1280, 0, 2560, 1392))
    # Negative virtual-desktop coordinates: the work-area origin is added to the local zones.
    assert sets[PORTRAIT.identity.device].layout == 'rows'
    assert sets[PORTRAIT.identity.device].rectangles == ((-1080, -162, 0, 774), (-1080, 774, 0, 1710))
    # A display with no EDID id is matched by its device name, spacing included.
    assert sets[SMALL.identity.device].layout == 'priority-grid'
    assert sets[SMALL.identity.device].rectangles == ((2576, 16, 2752, 536), (2768, 16, 3152, 536),
                                                      (3168, 16, 3344, 536))


def test_only_the_current_virtual_desktop_counts():
    sets = zone_sets([MAIN], fixture_data(), desktop=OTHER_DESKTOP)
    assert sets[MAIN.identity.device].layout == 'grid'  # the other desktop applied a grid to the same monitor


def test_disconnected_displays_and_stale_entries_never_appear():
    sets = zone_sets([PORTRAIT], fixture_data())
    assert set(sets) == {PORTRAIT.identity.device}


def test_a_display_without_a_saved_layout_gets_the_default_for_its_orientation():
    sets = zone_sets([MAIN, PORTRAIT], fixture_data(), desktop=uuid.uuid4())
    assert sets[MAIN.identity.device].layout == 'priority-grid' and len(sets[MAIN.identity.device].rectangles) == 3
    assert sets[PORTRAIT.identity.device].layout == 'rows' and len(sets[PORTRAIT.identity.device].rectangles) == 2


def test_defaults_without_a_defaults_file_are_priority_grid_and_rows():
    sets = fz.zone_sets([MAIN, PORTRAIT], data_for(), DESKTOP, LABELS)
    assert sets[MAIN.identity.device].layout == 'priority-grid'
    assert sets[PORTRAIT.identity.device].layout == 'rows'


def identity(**changes):
    values = dict(device=r'\\.\DISPLAY1', number=1, hardware_id='TST1001', instance_id='I-1', serial='S-1')
    values.update(changes)
    return DisplayInfo(DisplayIdentity(**values), (0, 0, 1000, 800), (0, 0, 1000, 800))


def match_layout(display, **entry_values):
    sets = fz.zone_sets([display], data_for(entry(applied('columns', 2), **entry_values)), DESKTOP, LABELS)
    return sets[display.identity.device].layout


@pytest.mark.parametrize('display_changes, entry_changes, matches', [
    ({}, dict(instance='I-1', serial='S-1', number=1, monitor='TST1001'), True),
    # The same monitor and instance match whatever number it was saved under.
    ({}, dict(instance='I-1', serial='S-1', number=9), True),
    # A re-plugged monitor keeps its number even though the instance changed.
    ({}, dict(instance='I-OLD', serial='S-1', number=1), True),
    # Different instance and different number: a different connection, so no match.
    ({}, dict(instance='I-OLD', serial='S-1', number=9), False),
    ({}, dict(monitor='OTHER99'), False),
    # Serial numbers veto a match only when both sides have one.
    ({}, dict(serial='S-2'), False),
    ({}, dict(serial=''), True),
    ({'serial': ''}, dict(serial='S-2'), True),
    ({'hardware_id': '', 'instance_id': ''}, dict(monitor=r'\\.\DISPLAY1', instance='', serial='', number=0), True),
    ({'hardware_id': '', 'instance_id': ''}, dict(monitor=r'\\.\DISPLAY3', instance='', serial='', number=0), False),
])
def test_display_matching_follows_powertoys_identity_rules(display_changes, entry_changes, matches):
    layout = match_layout(identity(**display_changes), **entry_changes)
    assert (layout == 'columns') is matches  # otherwise the default (priority-grid) applies


def test_the_closest_identity_wins_and_conflicting_ties_are_dropped():
    display = identity()
    near = entry(applied('columns', 2), instance='I-1', serial='S-1', number=1)
    by_number = entry(applied('rows', 2), instance='I-OLD', serial='S-1', number=1)
    device = display.identity.device
    assert fz.zone_sets([display], data_for(by_number, near), DESKTOP, LABELS)[device].layout == 'columns'
    conflicting = entry(applied('grid', 2), instance='I-OLD2', serial='S-1', number=1)
    assert fz.zone_sets([display], data_for(by_number, conflicting), DESKTOP, LABELS) == {}
    duplicate = entry(applied('rows', 2), instance='I-OLD3', serial='S-1', number=1)
    assert fz.zone_sets([display], data_for(by_number, duplicate), DESKTOP, LABELS)[device].layout == 'rows'


def test_an_unusable_layout_hides_only_that_display():
    broken = entry(applied('custom', 2, uuid_='{00000000-0000-0000-0000-0000DEAD0000}'))  # matches MAIN
    working = entry(applied('rows', 2), monitor='TST1002', instance='5&00000004&0&UID4304',
                    serial='SN00000002', number=2)
    sets = fz.zone_sets([MAIN, PORTRAIT], data_for(broken, working), DESKTOP, LABELS)
    assert set(sets) == {PORTRAIT.identity.device}  # no default is substituted for a matched but unusable layout


def test_spanning_zones_across_monitors_is_not_supported_and_yields_nothing():
    spanning = fixture_data(settings={'properties': {'fancyzones_span_zones_across_monitors': {'value': True}}})
    assert zone_sets([MAIN, PORTRAIT], spanning) == {}


def test_zero_sized_zones_make_the_layout_unusable():
    broken = entry(applied('custom', 1, uuid_='{00000000-0000-0000-0000-0000C0DE0004}'))
    custom = {'custom-layouts': [{'uuid': '{00000000-0000-0000-0000-0000C0DE0004}', 'name': 'x', 'type': 'canvas',
                                  'info': {'ref-width': 100, 'ref-height': 100,
                                           'zones': [{'X': 10, 'Y': 10, 'width': 0, 'height': 50}]}}]}
    assert fz.zone_sets([MAIN], data_for(broken, custom=custom), DESKTOP, LABELS) == {}


# --- tolerant parsing and loading ----------------------------------------------------

@pytest.mark.parametrize('garbage', [None, [], 'text', 3, {}, {'applied-layouts': 'x'}, {'applied-layouts': [None, 3, {}]}])
def test_malformed_applied_layouts_are_ignored(garbage):
    parsed = fz.parse_data(garbage, {}, {}, {})
    assert parsed.applied == ()


def test_entries_with_missing_or_invalid_fields_are_skipped_individually():
    good = entry(applied('columns', 2))
    bad_desktop = copy.deepcopy(good)
    bad_desktop['device']['virtual-desktop'] = 'not-a-guid'
    no_layout = {'device': good['device']}
    odd_type = entry(applied('columns', 'two'))
    parsed = fz.parse_data({'applied-layouts': [bad_desktop, no_layout, odd_type, good]}, {}, {}, {})
    assert len(parsed.applied) == 1


def test_load_reads_the_directory_and_tolerates_missing_or_broken_files(tmp_path):
    assert fz.load_data(tmp_path / 'missing') is None
    for name in ('applied-layouts', 'custom-layouts', 'default-layouts', 'settings'):
        (tmp_path / f'{name}.json').write_text('{ not json', encoding='utf-8')
    assert fz.load_data(tmp_path) is None  # applied layouts are unusable
    for name in ('custom-layouts', 'default-layouts', 'settings'):
        (tmp_path / f'{name}.json').write_text('[1, 2', encoding='utf-8')
    (tmp_path / 'applied-layouts.json').write_text(
        (FIXTURES / 'applied-layouts.json').read_text(encoding='utf-8'), encoding='utf-8')
    loaded = fz.load_data(tmp_path)
    assert loaded is not None and loaded.applied  # custom, default and settings files are optional


def test_loading_logs_counts_only(monkeypatch, tmp_path):
    messages = []
    monkeypatch.setattr(fz, 'debug_log', lambda message, *_: messages.append(message))
    fz.load_data(FIXTURES)
    fz.load_data(tmp_path / 'private-folder')
    fz.zone_sets([MAIN, PORTRAIT, SMALL], fixture_data(), DESKTOP, LABELS)
    text = ' '.join(messages)
    assert messages and not any(token in text for token in ('TST1001', 'SN0000', 'UID43', 'private-folder', 'FACE0001'))


# --- zone names ----------------------------------------------------------------------

def names(kind, count, size=(1000, 800), spacing=0, labels=LABELS):
    return fz.zone_names(fz.template_zones(kind, size[0], size[1], count, spacing), size[0], size[1], labels)


@pytest.mark.parametrize('kind, count, expected', [
    ('columns', 2, [('1', 'left'), ('2', 'right')]),
    ('columns', 3, [('1', 'left'), ('2', 'middle'), ('3', 'right')]),
    ('rows', 2, [('1', 'top'), ('2', 'bottom')]),
    ('rows', 3, [('1', 'top'), ('2', 'middle'), ('3', 'bottom')]),
    ('grid', 4, [('1', 'top_left'), ('2', 'top_right'), ('3', 'bottom_left'), ('4', 'bottom_right')]),
    ('priority-grid', 2, [('1', 'left'), ('2', 'right')]),
    ('priority-grid', 3, [('1', 'left'), ('2', 'middle'), ('3', 'right')]),
    ('priority-grid', 4, [('1', 'left'), ('2', 'middle'), ('3', 'top_right'), ('4', 'bottom_right')]),
    ('priority-grid', 5, [('1', 'top_left'), ('2', 'middle'), ('3', 'top_right'),
                          ('4', 'bottom_left'), ('5', 'bottom_right')]),
    # Nothing here is unambiguous, so only the numbers remain.
    ('columns', 4, [('1',), ('2',), ('3',), ('4',)]),
    ('rows', 4, [('1',), ('2',), ('3',), ('4',)]),
    ('focus', 3, [('1',), ('2',), ('3',)]),
    ('priority-grid', 1, [('1',)]),
])
def test_derived_names_appear_only_where_the_geometry_is_unambiguous(kind, count, expected):
    expected = [(number, *[LABELS[0][key] if key in LABELS[0] else key for key in rest]) for number, *rest in expected]
    assert names(kind, count) == expected


def test_names_tolerate_spacing_and_odd_sizes():
    assert [n[1:] for n in names('priority-grid', 3, size=(800, 552), spacing=16)] == [('left',), ('middle',), ('right',)]
    assert [n[1:] for n in names('columns', 2, size=(1080, 1872), spacing=16)] == [('left',), ('right',)]


def test_numbers_are_always_present_and_follow_zone_order():
    for kind in ('columns', 'rows', 'grid', 'priority-grid', 'focus'):
        for count in range(1, 9):
            assert [n[0] for n in names(kind, count)] == [str(i) for i in range(1, count + 1)]


def test_labels_come_from_locale_data_and_several_locales_combine():
    german = {'left': 'links', 'right': 'rechts'}
    assert names('columns', 2, labels=[german]) == [('1', 'links'), ('2', 'rechts')]
    assert names('columns', 2, labels=[LABELS[0], german]) == [('1', 'left', 'links'), ('2', 'right', 'rechts')]
    assert names('columns', 2, labels=[]) == [('1',), ('2',)]
    assert names('columns', 2, labels=[{'middle': 'centre'}]) == [('1',), ('2',)]


def test_shipped_label_files_cover_every_derived_name():
    english = fz.load_zone_labels(['en'])
    assert len(english) == 1 and set(english[0]) == {
        'left', 'right', 'middle', 'top', 'bottom', 'top_left', 'top_right', 'bottom_left', 'bottom_right'}
    assert fz.load_zone_labels(['xx', 'en']) == english   # unsupported locales are skipped
    assert fz.load_zone_labels(['../en', 'EN']) == []      # only plain lower-case language codes are looked up

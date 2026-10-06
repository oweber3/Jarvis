"""Behaviour tests for monitor geometry and window placement.

The OS boundary is replaced by ``FakeDesktop``, which models invisible window
borders, per-monitor DPI changes, minimum sizes and rejected requests, so the
tests assert the outcome (where the visible window ends up), not call sequences.
"""
import math

import pytest

from jarvis.platform.windows import displays, windows_mgmt as wm
from jarvis.platform.windows.displays import Monitor

TOLERANCE = wm.PLACEMENT_TOLERANCE_PX

# A primary display plus a taller display whose origin is negative (to its left)
# and a display above both; work areas exclude a taskbar strip.
PRIMARY = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
LEFT = Monitor(r'\\.\DISPLAY2', (-1920, -200, 0, 880), (-1920, -200, 0, 840), False)
ABOVE = Monitor(r'\\.\DISPLAY3', (200, -1080, 2120, 0), (200, -1080, 2120, -40), False)
MONITORS = [PRIMARY, LEFT, ABOVE]


class FakeDesktop:
    """A tiny window manager. Windows keep a visible rectangle plus invisible
    borders that depend on the monitor's DPI scale."""

    def __init__(self, monitors=MONITORS, scales=None):
        self.monitors = monitors
        self.scales = scales or {m.device: 1.0 for m in monitors}
        self.windows = {}

    def add(self, hwnd, visible, *, on=None, state='normal', minimum=(0, 0), reject=False, gone=False):
        monitor = on or self.monitor_at(visible)
        self.windows[hwnd] = {'visible': visible, 'restored': visible, 'state': state,
                              'minimum': minimum, 'reject': reject, 'gone': gone,
                              'monitor': monitor.device}
        return self

    def monitor_at(self, visible):
        cx, cy = (visible[0] + visible[2]) / 2, (visible[1] + visible[3]) / 2
        for monitor in self.monitors:
            left, top, right, bottom = monitor.bounds
            if left <= cx < right and top <= cy < bottom:
                return monitor
        return min(self.monitors, key=lambda m: math.hypot(
            (m.bounds[0] + m.bounds[2]) / 2 - cx, (m.bounds[1] + m.bounds[3]) / 2 - cy))

    def inset(self, device):
        return round(8 * self.scales[device])

    def device(self, name):
        return next(m for m in self.monitors if m.device == name)

    def window(self, hwnd):
        entry = self.windows.get(hwnd)
        if entry is None or entry['gone']:
            raise OSError('The selected window is no longer open.')
        return entry

    # --- OS seam used by windows_mgmt -------------------------------------------------
    def state(self, hwnd):
        return {'normal': 'normal', 'min': 'minimised', 'max': 'maximised'}[self.window(hwnd)['state']]

    def show(self, hwnd, action):
        entry = self.window(hwnd)
        if action == 'restore':
            entry['state'], entry['visible'] = 'normal', entry['restored']
        elif action == 'maximise':
            entry['state'] = 'max'
            entry['visible'] = self.device(entry['monitor']).work_area
        else:
            entry['state'] = 'min'

    def visible(self, hwnd):
        return self.window(hwnd)['visible']

    def outer(self, hwnd):
        entry = self.window(hwnd)
        inset = self.inset(entry['monitor'])
        left, top, right, bottom = entry['visible']
        return (left - inset, top - inset, right + inset, bottom + inset)

    def move(self, hwnd, outer):
        entry = self.window(hwnd)
        if entry['reject']:
            raise OSError('Windows rejected the window position request.')
        inset = self.inset(entry['monitor'])
        left, top, right, bottom = outer[0] + inset, outer[1] + inset, outer[2] - inset, outer[3] - inset
        right = max(right, left + entry['minimum'][0])
        bottom = max(bottom, top + entry['minimum'][1])
        previous = entry['monitor']
        entry['monitor'] = self.monitor_at((left, top, right, bottom)).device
        if entry['monitor'] != previous:
            # WM_DPICHANGED: the window rescales itself to the new monitor.
            ratio = self.scales[entry['monitor']] / self.scales[previous]
            right = left + round((right - left) * ratio)
            bottom = top + round((bottom - top) * ratio)
        entry['visible'] = entry['restored'] = (left, top, right, bottom)

    def monitor(self, hwnd):
        entry = self.window(hwnd)
        if entry['state'] == 'max':
            return entry['monitor']
        entry['monitor'] = self.monitor_at(entry['visible']).device
        return entry['monitor']


@pytest.fixture
def desktop(monkeypatch):
    fake = FakeDesktop()
    monkeypatch.setattr(wm, '_window_state', fake.state)
    monkeypatch.setattr(wm, '_show_window', fake.show)
    monkeypatch.setattr(wm, '_visible_rect', fake.visible)
    monkeypatch.setattr(wm, '_outer_rect', fake.outer)
    monkeypatch.setattr(wm, '_move_outer', fake.move)
    monkeypatch.setattr(wm, '_window_monitor', fake.monitor)
    return fake


def close(actual, expected, tolerance=TOLERANCE):
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


# --- geometry ------------------------------------------------------------------------

def test_zone_maps_to_work_area_with_negative_origin():
    left, top, right, bottom = LEFT.work_area
    width, height = right - left, bottom - top
    rectangle = displays.zone_rectangle(LEFT, [0.5, 0.25, 0.5, 0.75])
    assert rectangle == (left + round(0.5 * width), top + round(0.25 * height),
                         left + width, top + height)
    # Adjacent zones tile the work area without gaps or overlaps.
    first = displays.zone_rectangle(PRIMARY, [0, 0, 1 / 3, 1])
    second = displays.zone_rectangle(PRIMARY, [1 / 3, 0, 2 / 3, 1])
    assert first[2] == second[0]
    assert (first[0], second[2]) == (PRIMARY.work_area[0], PRIMARY.work_area[2])


@pytest.mark.parametrize('zone', [
    [0, 0, 1], [0, 0, 1, 1, 1], 'left', None, {'x': 0}, [0, 0, 0, 1], [0, 0, 1, 0], [0, 0, -0.5, 1],
    [-0.1, 0, 0.5, 1], [1.1, 0, 0.1, 1], [0.6, 0, 0.5, 1], [0, 0.6, 1, 0.5],
    [float('nan'), 0, 1, 1], [0, 0, float('inf'), 1], [True, 0, 1, 1], ['0', 0, 1, 1],
    [0, 0, 0.0000001, 1],
])
def test_invalid_zone_is_rejected(zone):
    with pytest.raises(ValueError):
        displays.zone_rectangle(PRIMARY, zone)


def test_missing_or_ambiguous_monitor_is_rejected():
    aliases = {'side': LEFT.device, 'gone': r'\\.\DISPLAY9', 'clash': ABOVE.device,
               PRIMARY.device: LEFT.device}
    assert displays.resolve_monitor(r'\\.\display2', MONITORS, aliases) == LEFT
    assert displays.resolve_monitor('Side', MONITORS, aliases) == LEFT
    for target in ['', '  ', r'\\.\DISPLAY9', 'unknown', 'gone']:
        with pytest.raises(ValueError):
            displays.resolve_monitor(target, MONITORS, aliases)
    # An alias that shadows another display's identifier is ambiguous, not a guess.
    with pytest.raises(ValueError, match='(?i)ambiguous'):
        displays.resolve_monitor(PRIMARY.device, MONITORS, aliases)
    with pytest.raises(ValueError, match='(?i)ambiguous'):
        displays.resolve_monitor(LEFT.device, [LEFT, LEFT], {})


# --- placement -----------------------------------------------------------------------

def test_placement_verifies_actual_result(desktop):
    desktop.add(1, (100, 100, 900, 700), state='min')
    target = displays.zone_rectangle(LEFT, [0.5, 0, 0.5, 1])
    result = wm.place_window(1, LEFT, target)
    assert desktop.state(1) == 'normal'
    assert close(desktop.visible(1), target)
    assert result['monitor'] == LEFT.device and result['state'] == 'restore'
    assert result['hwnd'] == 1 and close(result['rectangle'], target)


def test_placement_compensates_for_invisible_borders(desktop):
    desktop.add(1, (100, 100, 900, 700))
    target = (300, 120, 1100, 720)
    wm.place_window(1, PRIMARY, target)
    assert close(desktop.visible(1), target, 0)


def test_placement_survives_a_dpi_change_between_monitors(desktop):
    desktop.scales[LEFT.device] = 1.5
    desktop.add(1, (100, 100, 900, 700))
    target = (-1500, 0, -700, 600)
    result = wm.place_window(1, LEFT, target)
    assert desktop.monitor(1) == LEFT.device
    assert close(desktop.visible(1), target)
    assert close(result['rectangle'], target)


def test_placement_without_a_rectangle_keeps_size_and_stays_in_the_work_area(desktop):
    desktop.add(1, (100, 100, 900, 700))
    result = wm.place_window(1, LEFT)
    width, height = 800, 600
    left, top, right, bottom = result['rectangle']
    assert (right - left, bottom - top) == (width, height)
    work = LEFT.work_area
    assert work[0] <= left and right <= work[2] and work[1] <= top and bottom <= work[3]
    assert desktop.monitor(1) == LEFT.device


def test_oversized_window_is_clamped_into_the_destination(desktop):
    desktop.add(1, (0, 0, 2400, 1300))
    result = wm.place_window(1, LEFT)
    left, top, right, bottom = result['rectangle']
    work = LEFT.work_area
    assert work[0] - TOLERANCE <= left and right <= work[2] + TOLERANCE
    assert work[1] - TOLERANCE <= top and bottom <= work[3] + TOLERANCE


def test_clamping_keeps_a_window_already_on_the_destination_nearby(desktop):
    desktop.add(1, (2300, 1200, 2700, 1500), on=PRIMARY)
    result = wm.place_window(1, PRIMARY)
    work = PRIMARY.work_area
    left, top, right, bottom = result['rectangle']
    assert (right - left, bottom - top) == (400, 300)
    assert right <= work[2] + TOLERANCE and bottom <= work[3] + TOLERANCE


def test_maximise_fills_the_destination_work_area(desktop):
    desktop.add(1, (100, 100, 900, 700))
    result = wm.place_window(1, ABOVE, state='maximise')
    assert desktop.state(1) == 'maximised'
    assert desktop.monitor(1) == ABOVE.device
    assert result['state'] == 'maximise' and close(result['rectangle'], ABOVE.work_area)


def test_maximised_window_is_restored_before_it_is_moved(desktop):
    desktop.add(1, (100, 100, 900, 700), state='max')
    target = displays.zone_rectangle(ABOVE, [0, 0, 0.5, 1])
    wm.place_window(1, ABOVE, target)
    assert desktop.state(1) == 'normal' and close(desktop.visible(1), target)


def test_minimum_size_that_prevents_the_zone_is_reported(desktop):
    desktop.add(1, (100, 100, 900, 700), minimum=(1500, 100))
    with pytest.raises(OSError, match='(?i)size|minimum'):
        wm.place_window(1, LEFT, (-1920, -200, -1520, 400))


def test_rejected_positioning_is_reported_not_claimed(desktop):
    desktop.add(1, (100, 100, 900, 700), reject=True)
    with pytest.raises(OSError):
        wm.place_window(1, LEFT, (-1500, 0, -700, 600))


def test_window_that_disappears_is_reported(desktop):
    desktop.add(1, (100, 100, 900, 700), gone=True)
    with pytest.raises(OSError, match='(?i)no longer open'):
        wm.place_window(1, LEFT)


@pytest.mark.parametrize('rectangle', [
    (0, 0, 0, 100), (10, 10, 5, 100), (0, 0, 100), 'left', (float('nan'), 0, 10, 10),
    (-5000, 0, -4000, 100), (0, 0, 9000, 100),
])
def test_rectangles_outside_the_monitor_are_rejected_before_moving(desktop, rectangle):
    desktop.add(1, (100, 100, 900, 700))
    before = desktop.visible(1)
    with pytest.raises((ValueError, OSError)):
        wm.place_window(1, PRIMARY, rectangle)
    assert desktop.visible(1) == before


def test_unknown_state_is_rejected_before_moving(desktop):
    desktop.add(1, (100, 100, 900, 700))
    before = desktop.visible(1)
    with pytest.raises(ValueError):
        wm.place_window(1, LEFT, state='fullscreen')
    with pytest.raises(ValueError):
        wm.place_window(1, LEFT, (-1900, -100, -1000, 500), state='maximise')
    assert desktop.visible(1) == before


def test_placement_stops_at_the_supplied_deadline(desktop, monkeypatch):
    desktop.add(1, (100, 100, 900, 700))
    monkeypatch.setattr(wm, '_move_outer', lambda hwnd, rect: None)  # accepted, nothing moves
    start = wm.time.monotonic()
    with pytest.raises(OSError):
        wm.place_window(1, LEFT, (-1500, 0, -700, 600), deadline=start + 0.2)
    assert wm.time.monotonic() - start < 0.6


# --- thread DPI scope ----------------------------------------------------------------

def test_thread_dpi_context_is_restored_on_every_exit(monkeypatch):
    state = {'context': 'qt'}

    def set_context(value):
        previous, state['context'] = state['context'], value
        return previous

    monkeypatch.setattr(displays, '_set_thread_dpi_context', set_context)
    with displays.per_monitor_dpi():
        assert state['context'] == displays.PER_MONITOR_AWARE_V2
    assert state['context'] == 'qt'
    with pytest.raises(RuntimeError):
        with displays.per_monitor_dpi():
            raise RuntimeError('boom')
    assert state['context'] == 'qt'


def test_unavailable_dpi_api_is_not_fatal(monkeypatch):
    monkeypatch.setattr(displays, '_set_thread_dpi_context', lambda value: None)
    with displays.per_monitor_dpi():
        pass

"""Brightness over DDC/CI with a WMI fallback, with the panels replaced by fakes."""
import pytest

from jarvis.platform.windows import brightness

DISPLAY1, DISPLAY2, DISPLAY9 = r'\\.\DISPLAY1', r'\\.\DISPLAY2', r'\\.\DISPLAY9'


class FakePanel:
    def __init__(self, device, number, level=50, method='ddc', fail=None, stale_readback=False):
        self.device, self.number, self.method = device, number, method
        self.level, self.fail, self.stale = level, fail, stale_readback
        self.writes = []
        self.closed = False

    def get(self):
        if self.fail:
            raise brightness.BrightnessUnsupported(self.fail)
        return self.level

    def set(self, percent):
        if self.fail:
            raise brightness.BrightnessUnsupported(self.fail)
        self.writes.append(percent)
        if not self.stale:
            self.level = percent

    def close(self):
        self.closed = True


@pytest.fixture
def panels(monkeypatch):
    fakes = [FakePanel(DISPLAY1, 1, 40), FakePanel(DISPLAY2, 2, 70)]
    monkeypatch.setattr(brightness, '_open_panels', lambda: fakes)
    return fakes


def test_levels_are_read_per_monitor(panels):
    readings = brightness.read_levels()
    assert [(r.device, r.number, r.percent, r.method) for r in readings] == [
        (DISPLAY1, 1, 40, 'ddc'), (DISPLAY2, 2, 70, 'ddc')]


def test_set_changes_every_monitor_and_reports_the_level_read_back(panels):
    readings = brightness.set_levels(25)
    assert [p.writes for p in panels] == [[25], [25]]
    assert [(r.percent, r.verified) for r in readings] == [(25, True), (25, True)]


def test_set_can_target_one_monitor(panels):
    brightness.set_levels(10, device=DISPLAY2)
    assert [p.writes for p in panels] == [[], [10]]


def test_a_target_that_is_not_a_known_panel_is_an_error_and_changes_nothing(panels):
    with pytest.raises(ValueError):
        brightness.set_levels(10, device=DISPLAY9)
    assert [p.writes for p in panels] == [[], []]


@pytest.mark.parametrize('percent', [-1, 101, 'high', None, float('nan')])
def test_levels_outside_zero_to_one_hundred_are_rejected_before_any_write(panels, percent):
    with pytest.raises(ValueError):
        brightness.set_levels(percent)
    assert [p.writes for p in panels] == [[], []]


def test_a_monitor_without_ddc_is_reported_not_skipped_silently(monkeypatch):
    good = FakePanel(DISPLAY1, 1, 40)
    bad = FakePanel(DISPLAY2, 2, fail='This monitor does not support DDC/CI.')
    monkeypatch.setattr(brightness, '_open_panels', lambda: [good, bad])
    readings = brightness.set_levels(30)
    assert [r.percent for r in readings] == [30, None]
    assert 'DDC/CI' in readings[1].error
    assert bad.writes == []


def test_a_readback_that_disagrees_is_flagged_unverified(monkeypatch):
    panel = FakePanel(DISPLAY1, 1, 40, stale_readback=True)
    monkeypatch.setattr(brightness, '_open_panels', lambda: [panel])
    [reading] = brightness.set_levels(30)
    assert reading.percent == 40 and reading.verified is False


def test_adjust_moves_relative_to_the_current_level_and_clamps(panels):
    brightness.adjust_levels(+10)
    assert [p.level for p in panels] == [50, 80]
    brightness.adjust_levels(+100)
    assert [p.level for p in panels] == [100, 100]
    brightness.adjust_levels(-250)
    assert [p.level for p in panels] == [0, 0]


def test_panels_are_always_released(panels):
    brightness.read_levels()
    brightness.set_levels(20)
    assert all(p.closed for p in panels)


def test_panels_are_released_even_when_an_operation_fails(monkeypatch):
    panel = FakePanel(DISPLAY1, 1)
    monkeypatch.setattr(brightness, '_open_panels', lambda: [panel])
    with pytest.raises(ValueError):
        brightness.set_levels(20, device=DISPLAY9)
    assert panel.closed


def test_no_panels_at_all_is_an_honest_error(monkeypatch):
    monkeypatch.setattr(brightness, '_open_panels', lambda: [])
    with pytest.raises(brightness.BrightnessError):
        brightness.read_levels()


def test_ddc_raw_values_map_onto_a_percentage_of_the_monitor_range():
    assert brightness.raw_to_percent(0, 0, 100) == 0
    assert brightness.raw_to_percent(30, 0, 100) == 30
    assert brightness.raw_to_percent(75, 50, 100) == 50
    assert brightness.raw_to_percent(5, 5, 5) == 0
    assert brightness.percent_to_raw(50, 0, 100) == 50
    assert brightness.percent_to_raw(50, 0, 200) == 100
    assert brightness.percent_to_raw(100, 20, 80) == 80


def test_wmi_panel_is_used_for_an_internal_display(monkeypatch):
    class FakeWmi:
        level = 60

        def read(self):
            return self.level

        def write(self, percent):
            self.level = percent

    wmi = FakeWmi()
    panel = brightness.WmiPanel('internal', 1, wmi.read, wmi.write)
    monkeypatch.setattr(brightness, '_open_panels', lambda: [panel])
    [reading] = brightness.set_levels(35)
    assert wmi.level == 35 and reading.method == 'wmi' and reading.percent == 35


@pytest.mark.integration
def test_real_brightness_can_be_read_without_changing_anything():
    readings = brightness.read_levels()
    assert readings and all(0 <= (r.percent or 0) <= 100 for r in readings)
    assert all(r.percent is not None or r.error for r in readings)

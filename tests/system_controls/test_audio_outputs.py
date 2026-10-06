"""Playback device listing and the default-device switch, with the endpoint API replaced."""
import pytest

from jarvis.platform.windows import audio_outputs as outputs

SPEAKERS = outputs.OutputDevice('{0.0.0.00000000}.{aaa}', 'Speakers (Realtek(R) Audio)', True)
HEADSET = outputs.OutputDevice('{0.0.0.00000000}.{bbb}', 'Headset Earphone (Razer Kraken)', False)
MONITOR = outputs.OutputDevice('{0.0.0.00000000}.{ccc}', 'LG ULTRAGEAR (NVIDIA High Definition Audio)', False)
HEADPHONES = outputs.OutputDevice('{0.0.0.00000000}.{ddd}', 'Headphones (Arctis 7)', False)


class FakeEndpoints:
    def __init__(self, devices):
        self.devices = list(devices)
        self.default_calls = []

    def list(self):
        return list(self.devices)

    def set_default(self, device_id):
        self.default_calls.append(device_id)
        self.devices = [outputs.OutputDevice(d.id, d.name, d.id == device_id) for d in self.devices]


@pytest.fixture
def endpoints(monkeypatch):
    fake = FakeEndpoints([SPEAKERS, HEADSET, MONITOR])
    monkeypatch.setattr(outputs, '_endpoints', lambda: fake)
    return fake


def test_outputs_list_name_and_which_is_the_default(endpoints):
    assert outputs.list_outputs() == [SPEAKERS, HEADSET, MONITOR]


@pytest.mark.parametrize('query,expected', [
    ('speakers', SPEAKERS), ('Headset', HEADSET), ('lg ultragear', MONITOR),
    ('Headset Earphone (Razer Kraken)', HEADSET), ('razer', HEADSET),
])
def test_a_device_resolves_by_exact_name_or_whole_words(query, expected):
    assert outputs.resolve_output(query, [SPEAKERS, HEADSET, MONITOR], {}) == expected


def test_a_configured_alias_names_a_device():
    aliases = {'my headphones': 'Headset Earphone (Razer Kraken)', 'tv': 'LG ULTRAGEAR'}
    assert outputs.resolve_output('My Headphones', [SPEAKERS, HEADSET, MONITOR], aliases) == HEADSET
    assert outputs.resolve_output('tv', [SPEAKERS, HEADSET, MONITOR], aliases) == MONITOR


def test_an_alias_for_a_device_that_is_not_connected_is_an_error():
    with pytest.raises(ValueError) as error:
        outputs.resolve_output('cans', [SPEAKERS], {'cans': 'Arctis'})
    assert 'Speakers' in str(error.value)


def test_an_unknown_device_lists_what_is_available_and_never_guesses():
    with pytest.raises(ValueError) as error:
        outputs.resolve_output('bluetooth speaker', [SPEAKERS, HEADSET], {})
    assert 'Speakers' in str(error.value) and 'Headset' in str(error.value)


def test_a_name_matching_several_devices_is_ambiguous():
    nommo = outputs.OutputDevice('{0.0.0.00000000}.{eee}', 'Speakers (Razer Nommo)', False)
    with pytest.raises(ValueError) as error:
        outputs.resolve_output('razer', [HEADSET, nommo, SPEAKERS], {})
    assert 'Kraken' in str(error.value) and 'Nommo' in str(error.value)


def test_setting_the_default_switches_and_reports_the_device_that_is_now_default(endpoints):
    device = outputs.set_default_output('headset', {})
    assert endpoints.default_calls == [HEADSET.id]
    assert device.id == HEADSET.id and device.default


def test_switching_to_the_current_default_changes_nothing_and_says_so(endpoints):
    device = outputs.set_default_output('speakers', {})
    assert endpoints.default_calls == [] and device.default


def test_an_unknown_target_changes_nothing(endpoints):
    with pytest.raises(ValueError):
        outputs.set_default_output('nonexistent', {})
    assert endpoints.default_calls == []


def test_a_switch_that_does_not_take_effect_is_an_error(monkeypatch):
    class Stuck(FakeEndpoints):
        def set_default(self, device_id):
            self.default_calls.append(device_id)

    stuck = Stuck([SPEAKERS, HEADSET])
    monkeypatch.setattr(outputs, '_endpoints', lambda: stuck)
    with pytest.raises(outputs.AudioOutputError):
        outputs.set_default_output('headset', {})


@pytest.mark.integration
def test_real_playback_endpoints_can_be_listed_without_changing_anything():
    devices = outputs.list_outputs()
    assert devices, 'expected at least one active playback device'
    assert sum(1 for d in devices if d.default) == 1

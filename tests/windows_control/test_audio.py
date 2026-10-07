"""Behaviour of the Windows audio layer, driven through a fake endpoint."""

import sys
import threading

import pytest

from jarvis.platform.windows import audio


class FakeEndpoint:
    def __init__(self, scalar=0.5, muted=False):
        self.scalar = scalar
        self.muted = muted

    def get_scalar(self):
        return self.scalar

    def set_scalar(self, value):
        self.scalar = value

    def get_mute(self):
        return self.muted

    def set_mute(self, muted):
        self.muted = muted


@pytest.fixture
def endpoint(monkeypatch):
    fake = FakeEndpoint()
    monkeypatch.setattr(audio, "open_default_endpoint", lambda: fake)
    return fake


@pytest.mark.unit
def test_get_volume_reports_rounded_percent_and_mute(endpoint):
    endpoint.scalar = 0.8000000119
    endpoint.muted = True
    state = audio.get_volume()
    assert state.percent == 80
    assert state.muted is True


@pytest.mark.unit
@pytest.mark.parametrize("percent", [0, 30, 100])
def test_set_volume_applies_percent_and_returns_new_state(endpoint, percent):
    state = audio.set_volume(percent)
    assert endpoint.scalar == pytest.approx(percent / 100)
    assert state.percent == percent


@pytest.mark.unit
@pytest.mark.parametrize("percent", [-1, 101, 250])
def test_set_volume_rejects_out_of_range_without_touching_device(endpoint, percent):
    with pytest.raises(ValueError):
        audio.set_volume(percent)
    assert endpoint.scalar == 0.5


@pytest.mark.unit
def test_change_volume_is_relative_and_clamped(endpoint):
    assert audio.change_volume(+10).percent == 60
    assert audio.change_volume(-20).percent == 40
    assert audio.change_volume(+500).percent == 100
    assert audio.change_volume(-500).percent == 0


@pytest.mark.unit
def test_mute_and_unmute_preserve_level(endpoint):
    assert audio.set_muted(True).muted is True
    assert endpoint.muted is True
    assert endpoint.scalar == 0.5
    assert audio.set_muted(False).muted is False


@pytest.mark.unit
def test_device_failure_surfaces_as_audio_error(monkeypatch):
    def boom():
        raise OSError("no audio endpoint")

    monkeypatch.setattr(audio, "open_default_endpoint", boom)
    with pytest.raises(audio.AudioError):
        audio.get_volume()


@pytest.mark.unit
def test_hung_device_call_is_bounded(monkeypatch):
    release = threading.Event()

    class HungEndpoint(FakeEndpoint):
        def get_scalar(self):
            release.wait(5)
            return 0.5

    monkeypatch.setattr(audio, "open_default_endpoint", lambda: HungEndpoint())
    monkeypatch.setattr(audio, "CALL_TIMEOUT_SEC", 0.2)
    try:
        with pytest.raises(audio.AudioError):
            audio.get_volume()
    finally:
        release.set()


@pytest.mark.unit
def test_com_lifecycle_order_drops_objects_before_uninitialize(monkeypatch):
    events = []

    comtypes = pytest.importorskip("comtypes")
    monkeypatch.setattr(comtypes, "CoInitialize", lambda: events.append("CoInitialize"))
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: events.append("CoUninitialize"))

    class TrackingEndpoint:
        def __init__(self):
            events.append("endpoint_created")

        def get_scalar(self):
            events.append("action_executed")
            return 0.5

        def get_mute(self):
            return False

        def close(self):
            events.append("endpoint_closed")

    import gc
    monkeypatch.setattr(gc, "collect", lambda: events.append("gc_collect"))
    monkeypatch.setattr(audio, "open_default_endpoint", lambda: TrackingEndpoint())

    state = audio.get_volume()
    assert state.percent == 50

    expected_sequence = [
        "CoInitialize",
        "endpoint_created",
        "action_executed",
        "endpoint_closed",
        "gc_collect",
        "CoUninitialize",
    ]
    assert events == expected_sequence


@pytest.mark.unit
def test_com_lifecycle_drops_objects_on_action_exception(monkeypatch):
    events = []

    comtypes = pytest.importorskip("comtypes")
    monkeypatch.setattr(comtypes, "CoInitialize", lambda: events.append("CoInitialize"))
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: events.append("CoUninitialize"))

    class FailingEndpoint:
        def close(self):
            events.append("endpoint_closed")

    def failing_action(ep):
        events.append("action_failed")
        raise RuntimeError("device error")

    import gc
    monkeypatch.setattr(gc, "collect", lambda: events.append("gc_collect"))
    monkeypatch.setattr(audio, "open_default_endpoint", lambda: FailingEndpoint())

    with pytest.raises(audio.AudioError):
        audio._on_device(failing_action)

    expected_sequence = [
        "CoInitialize",
        "action_failed",
        "endpoint_closed",
        "gc_collect",
        "CoUninitialize",
    ]
    assert events == expected_sequence


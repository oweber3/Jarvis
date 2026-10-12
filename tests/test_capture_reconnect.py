"""The listener gets its microphone back after the capture stream stops delivering audio.

A fake PortAudio stands in for the sound system: unplugging a device stops
its streams, plugging it back in gives it a new handle that only a refreshed
device list can see, and refreshing the list closes every open stream (as
``Pa_Terminate`` does), so the tests can tell when a refresh would cut off
Jarvis's own voice.
"""

from collections import deque
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.listening.listener import VoiceListener
import jarvis.listening.listener as listener_module
from jarvis.utils import audio_lock

pytestmark = pytest.mark.unit

RATE = 16000
FRAME = RATE * 20 // 1000


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeStream:
    def __init__(self, audio, handle, callback):
        self.audio = audio
        self.handle = handle
        self.callback = callback
        self.active = False
        self.alive = True

    def start(self):
        if not self.alive:
            raise RuntimeError('Error starting stream: Unanticipated host error')
        self.active = True

    def stop(self):
        self.active = False

    def abort(self):
        self.active = False

    def close(self):
        self.active = False
        self.alive = False
        audio_lock.stream_closed(self)

    def deliver(self, level=0.1):
        """One block of audio, if the device is still delivering."""
        if self.alive and self.active and self.handle in self.audio.present:
            self.callback(np.full((FRAME, 1), level, dtype=np.float32), FRAME, None, None)
            return True
        return False


class FakePortAudio:
    """Devices are (name, generation) handles; the cached list changes only on refresh."""

    def __init__(self, names, default):
        self.present = [(name, 0) for name in names]
        self.listed = list(self.present)
        self.default = default
        self.streams = []
        self.refreshes = 0

    # -- the sounddevice surface the listener uses --
    def query_devices(self, device=None, kind=None):
        devices = [{'name': name, 'max_input_channels': 1, 'default_samplerate': RATE}
                   for name, _ in self.listed]
        if device is not None:
            return devices[device]
        if kind == 'input':
            return devices[self._default_index()]
        return devices

    def InputStream(self, samplerate, channels, dtype, blocksize, callback, device=None):
        index = self._default_index() if device is None else device
        handle = self.listed[index] if 0 <= index < len(self.listed) else None
        if handle is None or handle not in self.present:
            raise RuntimeError('Error opening InputStream: Device unavailable [PaErrorCode -9985]')
        stream = FakeStream(self, handle, callback)
        self.streams.append(stream)
        return stream

    def _terminate(self):
        for stream in self.streams:
            stream.close()

    def _initialize(self):
        self.refreshes += 1
        self.listed = list(self.present)

    # -- the hardware --
    def _default_index(self):
        names = [name for name, _ in self.listed]
        return names.index(self.default) if self.default in names else 0

    def unplug(self, name):
        self.present = [handle for handle in self.present if handle[0] != name]

    def plug(self, name):
        generation = max([g for n, g in self.listed if n == name] + [0]) + 1
        self.present.append((name, generation))

    def other_stream(self):
        """Another part of Jarvis (its voice, dictation) holding a stream open, as they record them."""
        stream = FakeStream(self, ('speaker', 0), lambda *args: None)
        self.present.append(('speaker', 0))
        stream.start()
        self.streams.append(stream)
        audio_lock.stream_opened(stream)
        return stream


class FakeTTS:
    enabled = True

    def __init__(self):
        self.speaking = False

    def is_speaking(self):
        return self.speaking


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(listener_module.time, 'monotonic', clock)
    return clock


@pytest.fixture
def audio(monkeypatch):
    audio = FakePortAudio(['Microphone (Webcam)', 'Microphone (USB Mic)'], default='Microphone (USB Mic)')
    monkeypatch.setattr(listener_module, 'sd', audio)
    yield audio
    for stream in audio.streams:
        stream.close()


def make_listener(audio, voice_device=None):
    obj = VoiceListener.__new__(VoiceListener)
    obj.cfg = SimpleNamespace(voice_min_energy=0.0045, voice_debug=False, voice_device=voice_device)
    obj.tts = FakeTTS()
    obj._samplerate = RATE
    obj._frame_ms = 20
    obj._stream_samplerate = RATE
    obj._frame_samples = FRAME
    obj._pending_audio = None
    obj._pre_roll = deque()
    obj._utterance_frames = []
    obj.is_speech_active = False
    obj._silence_frames = 0
    obj._wake_timestamp = None
    obj._recent_audio_energy = deque(maxlen=20)
    obj._vad_error_logged = False
    obj._vad = None
    obj._should_stop = False
    obj._dictation_is_active = False
    obj._dictation_generation = 0
    obj._callback_count = 0
    obj._audio_q = queue.Queue(maxsize=64)
    obj._reset_audio_health()
    obj._start_capture(listener_module.resolve_input_device(audio, voice_device))
    return obj


def run_for(obj, clock, seconds, *, delivering=True, level=0.1):
    """Run the listener loop's capture upkeep for ``seconds``, with audio arriving if the device delivers it."""
    heard = 0
    for _ in range(int(seconds * 5)):
        clock.advance(0.2)
        stream = obj._capture_stream
        if delivering and stream is not None and stream.deliver(level):
            heard += 1
        while not obj._audio_q.empty():
            for _frame in obj._audio_frames(obj._audio_q.get_nowait()):
                pass
        obj._check_audio_health()
        obj._recover_capture()
    return heard


def test_capture_that_stops_delivering_is_reopened_and_heard_again(audio, clock, capsys):
    obj = make_listener(audio)
    assert run_for(obj, clock, 6) > 0
    first = obj._capture_stream

    first.alive = False  # e.g. the PC slept and the stream died
    run_for(obj, clock, 12)

    assert obj._capture_stream is not first
    assert run_for(obj, clock, 6) > 0
    output = capsys.readouterr().out
    assert 'No microphone callbacks' in output
    assert 'arriving again' in output


def test_usb_mic_plugged_back_in_is_found_after_the_device_list_refreshes(audio, clock, capsys):
    audio.unplug('Microphone (Webcam)')  # the USB mic is the only microphone
    obj = make_listener(audio)
    run_for(obj, clock, 6)

    audio.unplug('Microphone (USB Mic)')
    run_for(obj, clock, 15)
    assert obj._capture_stream is None or not obj._capture_stream.deliver()

    audio.plug('Microphone (USB Mic)')
    run_for(obj, clock, 60)

    assert obj._capture_stream is not None and obj._capture_stream.handle[0] == 'Microphone (USB Mic)'
    assert run_for(obj, clock, 6) > 0
    assert 'arriving again' in capsys.readouterr().out


def test_named_mic_is_found_again_at_its_new_position(audio, clock):
    obj = make_listener(audio, voice_device='USB Mic')
    run_for(obj, clock, 6)

    audio.unplug('Microphone (USB Mic)')
    audio.unplug('Microphone (Webcam)')
    run_for(obj, clock, 10)
    audio.plug('Microphone (USB Mic)')  # comes back first, so its index changes
    audio.plug('Microphone (Webcam)')
    run_for(obj, clock, 60)

    assert obj._capture_stream.handle[0] == 'Microphone (USB Mic)'
    assert run_for(obj, clock, 6) > 0


def test_when_the_default_mic_goes_away_the_new_default_is_used(audio, clock):
    obj = make_listener(audio)
    run_for(obj, clock, 6)

    audio.unplug('Microphone (USB Mic)')  # Windows makes the webcam mic the default
    run_for(obj, clock, 60)

    assert obj._capture_stream.handle[0] == 'Microphone (Webcam)'
    assert run_for(obj, clock, 6) > 0


def test_device_list_is_not_refreshed_while_jarvis_is_speaking(audio, clock):
    obj = make_listener(audio)
    run_for(obj, clock, 6)
    audio.unplug('Microphone (USB Mic)')
    audio.plug('Microphone (USB Mic)')

    voice = audio.other_stream()
    run_for(obj, clock, 60)
    assert voice.active, "refreshing the device list would have cut Jarvis off mid-sentence"

    voice.close()
    run_for(obj, clock, 60)
    assert run_for(obj, clock, 6) > 0


def test_device_list_is_not_refreshed_while_dictating(audio, clock):
    obj = make_listener(audio)
    run_for(obj, clock, 6)
    audio.unplug('Microphone (USB Mic)')
    audio.plug('Microphone (USB Mic)')
    run_for(obj, clock, 6)  # the loss is noticed before dictation starts

    dictation = audio.other_stream()
    obj._dictation_active = True
    for _ in range(300):
        clock.advance(0.2)
        obj._recover_capture()
    assert dictation.active

    obj._dictation_active = False
    dictation.close()
    run_for(obj, clock, 60)
    assert run_for(obj, clock, 6) > 0


def test_mic_chosen_in_settings_by_number_stays_that_mic_after_a_refresh(audio, clock):
    # Settings stores the device number; unplugging the USB mic moves the webcam mic into its slot.
    audio.present = [('Microphone (USB Mic)', 0), ('Microphone (Webcam)', 0)]
    audio.listed = list(audio.present)
    obj = make_listener(audio, voice_device='0')
    run_for(obj, clock, 6)

    audio.unplug('Microphone (USB Mic)')
    run_for(obj, clock, 60)
    assert obj._capture_stream is None or obj._capture_stream.handle[0] != 'Microphone (Webcam)'

    audio.plug('Microphone (USB Mic)')
    run_for(obj, clock, 60)
    assert obj._capture_stream.handle[0] == 'Microphone (USB Mic)'
    assert run_for(obj, clock, 6) > 0


def test_device_list_is_not_refreshed_while_the_startup_mic_check_is_still_running(audio, clock):
    import threading
    obj = make_listener(audio)
    run_for(obj, clock, 6)
    release = threading.Event()
    probe = threading.Thread(target=release.wait, daemon=True)  # a permission check stuck inside Windows
    probe.start()
    obj._mic_probe_thread = probe
    audio.unplug('Microphone (USB Mic)')
    audio.plug('Microphone (USB Mic)')

    run_for(obj, clock, 60)
    assert audio.refreshes == 0

    release.set()
    probe.join(1)
    run_for(obj, clock, 60)
    assert run_for(obj, clock, 6) > 0


def test_missing_mic_is_retried_with_growing_gaps_and_announced_once(audio, clock, capsys):
    obj = make_listener(audio)
    run_for(obj, clock, 6)
    opens_before = len(audio.streams)

    audio.unplug('Microphone (USB Mic)')
    audio.unplug('Microphone (Webcam)')
    attempts = []
    original = audio.InputStream

    def counting_open(*args, **kwargs):
        attempts.append(clock())
        return original(*args, **kwargs)

    audio.InputStream = counting_open
    run_for(obj, clock, 300)

    gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
    assert len(attempts) >= 3
    assert gaps == sorted(gaps), "retries should not get more frequent"
    assert len(attempts) <= 300 / 20, "a missing mic should not be hammered every few seconds"
    assert capsys.readouterr().out.count('Reconnecting to the microphone') == 1
    assert len(audio.streams) == opens_before  # every attempt failed to open

    audio.plug('Microphone (USB Mic)')
    run_for(obj, clock, 60)
    assert run_for(obj, clock, 6) > 0


def test_silent_capture_is_reopened_once_not_repeatedly(audio, clock):
    obj = make_listener(audio)
    run_for(obj, clock, 6)
    opened = len(audio.streams)

    run_for(obj, clock, 120, level=0.0)  # digital silence, e.g. a muted mic

    assert len(audio.streams) == opened + 1


def test_stopping_the_listener_closes_the_capture_stream(audio, clock):
    obj = make_listener(audio)
    stream = obj._capture_stream
    obj._close_capture()
    assert not stream.alive and obj._capture_stream is None

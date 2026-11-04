"""Start-up overlaps the voice (TTS) load with speech recognition's load, but never with the
microphone permission check: a DLL load racing that check can deadlock on the Windows loader lock."""

import threading
import time
from types import SimpleNamespace

import pytest

import jarvis.listening.listener as listener_module
from jarvis.listening.listener import VoiceListener


class FakeListener:
    """Lists the audio devices, checks the microphone, then spends a while loading speech recognition."""

    def __init__(self, timeline, enumerate_s=0.0, check_s=0.1, load_s=0.6, check_hangs=False,
                 ends_before_check=False):
        self.timeline = timeline
        self.enumerate_s = enumerate_s
        self.check_s = check_s
        self.load_s = load_s
        self.check_hangs = check_hangs
        self.ends_before_check = ends_before_check
        self._checked = threading.Event()
        self._check_started_at = None
        self._thread = None

    def start(self):
        def run():
            time.sleep(self.enumerate_s)
            if self.ends_before_check:
                self.timeline.append("no microphone")
                self._checked.set()
                return
            self._check_started_at = time.monotonic()
            self.timeline.append("mic check started")
            if self.check_hangs:
                time.sleep(10)
                return
            time.sleep(self.check_s)
            self.timeline.append("mic checked")
            self._checked.set()
            time.sleep(self.load_s)
            self.timeline.append("whisper loaded")
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def microphone_check_started_at(self):
        return self._check_started_at

    def wait_until_microphone_checked(self, timeout):
        return self._checked.wait(timeout)


class FakeTTS:
    def __init__(self, timeline):
        self.timeline = timeline

    def start(self):
        self.timeline.append("tts started")


def _start(listener, tts, window=5.0, cap=30.0):
    from jarvis import daemon

    daemon._start_listener_then_tts(listener, tts, check_window_sec=window, safety_cap_sec=cap)


def test_the_voice_loads_after_the_microphone_check_while_speech_recognition_loads():
    timeline = []
    _start(FakeListener(timeline), FakeTTS(timeline))
    time.sleep(0.8)

    assert timeline == ["mic check started", "mic checked", "tts started", "whisper loaded"]


def test_slow_device_listing_does_not_eat_into_the_microphone_check_window():
    """The window starts when the check starts: listing devices slowly (Bluetooth) never lets the voice
    load while the check is still open."""
    timeline = []
    _start(FakeListener(timeline, enumerate_s=0.6, check_s=0.2), FakeTTS(timeline), window=0.5)

    assert timeline == ["mic check started", "mic checked", "tts started"]


def test_a_microphone_check_that_hangs_is_given_up_on_after_its_window():
    timeline = []
    started = time.monotonic()
    _start(FakeListener(timeline, check_hangs=True), FakeTTS(timeline), window=0.3)

    assert timeline == ["mic check started", "tts started"]
    assert time.monotonic() - started < 1.0


def test_a_listener_that_ends_before_checking_lets_the_voice_load_at_once():
    timeline = []
    started = time.monotonic()
    _start(FakeListener(timeline, ends_before_check=True), FakeTTS(timeline))

    assert timeline == ["no microphone", "tts started"]
    assert time.monotonic() - started < 1.0


def test_the_safety_cap_bounds_a_device_listing_that_never_finishes():
    timeline = []
    started = time.monotonic()
    _start(FakeListener(timeline, enumerate_s=10), FakeTTS(timeline), window=0.2, cap=0.4)

    assert timeline == ["tts started"]
    assert time.monotonic() - started < 1.0


def test_no_voice_engine_still_starts_the_listener():
    timeline = []
    _start(FakeListener(timeline, load_s=0.0), None)
    time.sleep(0.3)

    assert timeline == ["mic check started", "mic checked", "whisper loaded"]


def _bare_listener(cfg=None):
    obj = VoiceListener.__new__(VoiceListener)
    threading.Thread.__init__(obj, daemon=True)
    obj._init_startup_signals()
    obj.cfg = cfg or SimpleNamespace(voice_device=None)
    obj._transcription_worker_thread = None
    return obj


def test_a_listener_without_an_audio_system_reports_the_microphone_check_as_done(monkeypatch):
    monkeypatch.setattr(listener_module, "sd", None)
    obj = _bare_listener()

    obj.run()

    assert obj.wait_until_microphone_checked(0) is True


def test_the_microphone_check_is_reported_done_before_speech_recognition_loads(monkeypatch):
    """By the time Whisper starts loading (DLL loads on this thread) the check is over."""
    class Stream:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    class PortAudio:
        def query_devices(self, device=None, kind=None):
            return [{"name": "Mic", "max_input_channels": 1, "default_samplerate": 16000}]

        def InputStream(self, **kwargs):
            return Stream()

    seen = {}

    def whisper_class():
        seen["checked when whisper loads"] = obj.wait_until_microphone_checked(0)
        return None  # "faster-whisper not available": the listener stops here

    monkeypatch.setattr(listener_module, "sd", PortAudio())
    monkeypatch.setattr(listener_module.sys, "platform", "win32")
    monkeypatch.setattr(listener_module, "_faster_whisper_model_class", whisper_class)
    obj = _bare_listener(SimpleNamespace(voice_device=None, whisper_model="small", wake_sound_enabled=False))
    obj._samplerate = 16000
    monkeypatch.setattr(obj, "_start_llm_warmup", lambda: [], raising=False)
    monkeypatch.setattr(obj, "_determine_whisper_backend", lambda: "faster-whisper", raising=False)

    obj.run()

    assert seen == {"checked when whisper loads": True}
    assert obj.microphone_check_started_at() is not None

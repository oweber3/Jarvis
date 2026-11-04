"""Start-up overlaps the voice (TTS) load with speech recognition's load, but never with the
microphone permission check: a DLL load racing that check can deadlock on the Windows loader lock."""

import threading
import time
from types import SimpleNamespace

import pytest

import jarvis.listening.listener as listener_module
from jarvis.listening.listener import VoiceListener


class FakeListener:
    """Checks the microphone briefly, then spends a while loading speech recognition."""

    def __init__(self, timeline, check_s=0.1, load_s=0.6, checks=True):
        self.timeline = timeline
        self.check_s = check_s
        self.load_s = load_s
        self.checks = checks
        self._checked = threading.Event()

    def start(self):
        def run():
            time.sleep(self.check_s)
            self.timeline.append("mic checked")
            if self.checks:
                self._checked.set()
            time.sleep(self.load_s)
            self.timeline.append("whisper loaded")
        threading.Thread(target=run, daemon=True).start()

    def wait_until_microphone_checked(self, timeout):
        return self._checked.wait(timeout)


class FakeTTS:
    def __init__(self, timeline):
        self.timeline = timeline

    def start(self):
        self.timeline.append("tts started")


def test_the_voice_loads_after_the_microphone_check_while_speech_recognition_loads():
    from jarvis import daemon

    timeline = []
    daemon._start_listener_then_tts(FakeListener(timeline), FakeTTS(timeline), check_timeout_sec=5.0)
    time.sleep(0.8)

    assert timeline == ["mic checked", "tts started", "whisper loaded"]


def test_a_microphone_check_that_never_reports_back_does_not_stop_the_voice_from_loading():
    from jarvis import daemon

    timeline = []
    started = time.monotonic()
    daemon._start_listener_then_tts(FakeListener(timeline, checks=False), FakeTTS(timeline), check_timeout_sec=0.3)

    assert "tts started" in timeline
    assert time.monotonic() - started < 1.0


def test_no_voice_engine_still_starts_the_listener():
    from jarvis import daemon

    timeline = []
    daemon._start_listener_then_tts(FakeListener(timeline, load_s=0.0), None, check_timeout_sec=5.0)
    time.sleep(0.3)

    assert timeline == ["mic checked", "whisper loaded"]


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

"""onnxruntime telemetry must be off before Jarvis creates any onnxruntime session."""

import sys
import types

import numpy as np
import pytest


@pytest.fixture
def fake_ort(monkeypatch):
    """A stand-in onnxruntime that records the order of calls."""
    events = []
    ort = types.ModuleType("onnxruntime")

    class _Options:
        intra_op_num_threads = 0
        inter_op_num_threads = 0

    class _Input:
        name = "feats"

    class _Session:
        def __init__(self, *a, **k):
            events.append("session")

        def get_inputs(self):
            return [_Input()]

    ort.SessionOptions = _Options
    ort.InferenceSession = _Session
    ort.disable_telemetry_events = lambda: events.append("telemetry_off")
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    return events


def test_speaker_embedder_disables_telemetry_before_its_session(fake_ort):
    from src.jarvis.listening.speaker_verification import SpeakerEmbedder

    SpeakerEmbedder("model.onnx")

    assert fake_ort == ["telemetry_off", "session"]


def test_piper_disables_telemetry_before_loading_the_voice(fake_ort, monkeypatch, tmp_path):
    from src.jarvis.output.tts import PiperTTS

    model = tmp_path / "voice.onnx"
    model.write_bytes(b"x")
    (tmp_path / "voice.onnx.json").write_text("{}")

    class _Voice:
        config = types.SimpleNamespace(sample_rate=22050)

        @staticmethod
        def load(model_path, config_path):
            fake_ort.append("piper_session")
            return _Voice()

    piper = types.ModuleType("piper")
    voice_mod = types.ModuleType("piper.voice")
    voice_mod.PiperVoice = _Voice
    piper.voice = voice_mod
    monkeypatch.setitem(sys.modules, "piper", piper)
    monkeypatch.setitem(sys.modules, "piper.voice", voice_mod)

    tts = PiperTTS(enabled=True, model_path=str(model))
    assert tts._ensure_initialized() is True

    assert fake_ort == ["telemetry_off", "piper_session"]


def test_a_missing_onnxruntime_is_not_an_error(monkeypatch):
    from src.jarvis.utils.onnx_privacy import disable_onnxruntime_telemetry

    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # makes the import raise ImportError

    disable_onnxruntime_telemetry()


def test_a_failing_disable_call_never_blocks_the_session(monkeypatch):
    from src.jarvis.utils.onnx_privacy import disable_onnxruntime_telemetry

    ort = types.ModuleType("onnxruntime")

    def _boom():
        raise RuntimeError("no")

    ort.disable_telemetry_events = _boom
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    disable_onnxruntime_telemetry()

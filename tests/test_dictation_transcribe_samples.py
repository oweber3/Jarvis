"""Transcribing audio recorded elsewhere (dictation.spec.md, Transcribing audio for a caller).

Phone access hands the engine the phone's audio. The engine only transcribes: it records nothing, pauses
nothing, beeps nothing and keeps and logs no text. Whisper is faked.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

SECRET = "the quarterly figures are private"


@pytest.fixture(autouse=True)
def _dictation_deps():
    pytest.importorskip("pynput")
    pytest.importorskip("numpy")
    pytest.importorskip("sounddevice")


def make_engine(tmp_path, *, model=True, backend="faster-whisper", **extra):
    from jarvis.dictation import dictation_engine as de
    from jarvis.dictation.history import DictationHistory

    calls = SimpleNamespace(start=0, processing=0, end=0, result=[])
    engine = de.DictationEngine(
        whisper_model_ref=(lambda: MagicMock()) if model else (lambda: None),
        whisper_backend_ref=lambda: backend,
        mlx_repo_ref=lambda: None,
        on_dictation_start=lambda: setattr(calls, "start", calls.start + 1),
        on_dictation_processing_start=lambda: setattr(calls, "processing", calls.processing + 1),
        on_dictation_end=lambda: setattr(calls, "end", calls.end + 1),
        on_dictation_result=calls.result.append,
        history=DictationHistory(path=tmp_path / "history.json"),
        **extra,
    )
    return engine, calls


def audio(seconds=1.0, rate=16000):
    import numpy as np
    return np.full(int(rate * seconds), 0.1, dtype=np.float32)


def test_the_audio_is_transcribed_and_the_text_returned(tmp_path):
    engine, _ = make_engine(tmp_path)
    with patch.object(engine, "_transcribe", return_value=SECRET) as transcribe:
        assert engine.transcribe_samples(audio(), 16000) == SECRET
    assert transcribe.call_count == 1 and len(transcribe.call_args.args[0]) == 16000


def test_nothing_is_recorded_paused_beeped_pasted_or_remembered(tmp_path):
    from jarvis.dictation import dictation_engine as de

    engine, calls = make_engine(tmp_path)
    with patch.object(de, "sd") as sd, patch.object(de, "_play_beep") as beep, \
         patch.object(de, "_clipboard_paste") as paste, patch.object(engine.history, "add") as remember, \
         patch.object(engine, "_transcribe", return_value=SECRET):
        engine.transcribe_samples(audio(), 16000)
    remember.assert_not_called()
    sd.InputStream.assert_not_called()
    beep.assert_not_called()
    paste.assert_not_called()
    assert (calls.start, calls.processing, calls.end, calls.result) == (0, 0, 0, [])


def test_audio_that_is_too_short_or_empty_gives_nothing_without_calling_whisper(tmp_path):
    engine, _ = make_engine(tmp_path)
    with patch.object(engine, "_transcribe", return_value=SECRET) as transcribe:
        assert engine.transcribe_samples(audio(0.1), 16000) == ""
        assert engine.transcribe_samples(audio(0), 16000) == ""
    transcribe.assert_not_called()


def test_other_rates_are_resampled_to_the_models(tmp_path):
    engine, _ = make_engine(tmp_path)
    with patch.object(engine, "_transcribe", return_value="ok") as transcribe:
        engine.transcribe_samples(audio(1.0, 48000), 48000)
    assert abs(len(transcribe.call_args.args[0]) - 16000) <= 2


def test_the_custom_dictionary_applies(tmp_path):
    engine, _ = make_engine(tmp_path, custom_dictionary=["jarvice -> Jarvis"])
    with patch.object(engine, "_transcribe", return_value="hello jarvice"):
        assert engine.transcribe_samples(audio(), 16000) == "hello Jarvis"


def test_it_can_run_while_the_pc_is_listening_or_dictating(tmp_path):
    from jarvis.dictation import dictation_engine as de

    engine, _ = make_engine(tmp_path)
    with patch.object(de, "sd") as sd, patch.object(de, "_play_beep"), \
         patch.object(engine, "_transcribe", return_value="ok"):
        sd.query_devices.return_value = {"max_input_channels": 1, "default_samplerate": 16000}
        token = engine.start_capture(max_seconds=30)
        assert engine.transcribe_samples(audio(), 16000) == "ok"
        engine.cancel_capture(token)


def test_the_text_is_never_logged(tmp_path):
    from jarvis.dictation import dictation_engine as de

    engine, _ = make_engine(tmp_path, filler_removal=True, cfg=SimpleNamespace())
    logged = []
    backend = SimpleNamespace(direct=lambda *a, **k: "tidied private text")
    with patch.object(de, "debug_log", side_effect=lambda message, *_a, **_k: logged.append(str(message))), \
         patch("jarvis.llm.get_llm_backend", return_value=backend), patch("jarvis.llm.resolve_model", return_value="m"), \
         patch.object(engine, "_transcribe", return_value=SECRET):
        engine.transcribe_samples(audio(), 16000)
    assert logged
    assert not [m for m in logged if "quarterly" in m or "tidied" in m]


def test_the_blocker_says_when_the_speech_model_is_not_loaded(tmp_path):
    engine, _ = make_engine(tmp_path, model=False)
    assert engine.transcribe_blocker() == "not_ready"
    assert engine.transcribe_samples(audio(), 16000) == ""


def test_the_blocker_is_clear_when_the_model_is_ready(tmp_path):
    engine, _ = make_engine(tmp_path)
    assert engine.transcribe_blocker() is None


def test_the_mlx_backend_needs_no_model_object(tmp_path):
    engine, _ = make_engine(tmp_path, model=False, backend="mlx")
    assert engine.transcribe_blocker() is None


def test_without_numpy_or_audio_support_it_is_unavailable(tmp_path):
    from jarvis.dictation import dictation_engine as de

    engine, _ = make_engine(tmp_path)
    with patch.object(de, "np", None):
        assert engine.transcribe_blocker() == "unavailable"

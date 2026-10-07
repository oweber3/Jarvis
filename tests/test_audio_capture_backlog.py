"""Microphone audio is kept while the listener is busy deciding on an utterance, within a fixed bound.

The intent judge runs on the listener thread, so capture blocks wait in the
capture queue for up to ``intent_judge_timeout_sec``. Speech the user starts
in that time must still be there when the listener returns to it.
"""

import numpy as np
import pytest

from tests.test_hot_window_input import _create_listener

pytestmark = pytest.mark.unit


def _listener(judge_timeout_sec, frame_ms):
    from jarvis.listening.listener import VoiceListener
    template, tts = _create_listener()
    cfg = template.cfg
    template.state_manager.stop()
    cfg.intent_judge_timeout_sec = judge_timeout_sec
    cfg.vad_frame_ms = frame_ms
    listener = VoiceListener(None, cfg, tts, None)
    return listener


def _capture(listener, seconds, frame_ms):
    """Deliver ``seconds`` of capture callbacks with nothing consuming them; return (delivered, kept)."""
    block = np.zeros((16000 * frame_ms // 1000, 1), dtype=np.float32)
    blocks = int(round(seconds * 1000 / frame_ms))
    for _ in range(blocks):
        listener._on_audio(block, len(block), None, None)
    kept = 0
    while not listener._audio_q.empty():
        listener._audio_q.get_nowait()
        kept += 1
    return blocks, kept


@pytest.mark.parametrize("frame_ms", [10, 20, 30])
def test_speech_captured_while_the_judge_decides_is_kept(frame_ms):
    listener = _listener(6.0, frame_ms)

    delivered, kept = _capture(listener, 6.0, frame_ms)

    assert kept == delivered
    listener.state_manager.stop()


def test_a_longer_judge_timeout_keeps_correspondingly_more_audio():
    listener = _listener(12.0, 20)

    delivered, kept = _capture(listener, 12.0, 20)

    assert kept == delivered
    listener.state_manager.stop()


VOICED, QUIET = 0.1, 0.0


def _listener_with_audio(judge_timeout_sec=6.0):
    listener = _listener(judge_timeout_sec, 20)
    cfg = listener.cfg
    cfg.vad_enabled = False
    cfg.voice_min_energy = 0.02
    cfg.endpoint_silence_ms = 600
    cfg.vad_pre_roll_ms = 240
    cfg.max_utterance_ms = 12000
    cfg.tts_max_utterance_ms = 3000
    cfg.whisper_min_audio_duration = 0.3
    listener._stream_samplerate = 16000
    listener._configure_audio(20)
    return listener


def _queue_audio(listener, *spans):
    """Capture ``(seconds, level)`` spans into the queue, as the audio callback would."""
    for seconds, level in spans:
        for _ in range(int(round(seconds / 0.02))):
            listener._on_audio(np.full((320, 1), level, dtype=np.float32), 320, None, None)


def _drain_capture(listener):
    while not listener._audio_q.empty():
        listener._consume_audio_block(listener._audio_q.get_nowait())


def _hear_job_as(listener, text):
    from types import SimpleNamespace
    job = listener._transcription_jobs_q.get_nowait()
    listener._handle_transcription_result(SimpleNamespace(
        text=text, language="en", low_confidence_events=(), start_time=job.start_time,
        end_time=job.end_time, speech_end_time=job.speech_end_time, energy=job.energy,
        dictation_generation=job.dictation_generation, captured_during_tts=job.captured_during_tts,
        captured_tts_start_time=job.captured_tts_start_time, speaker_verdict=None))
    listener._transcription_jobs_q.task_done()


def test_speech_said_while_the_judge_decides_joins_the_request():
    import time
    from types import SimpleNamespace
    from tests.test_hot_window_input import _install_intent_judge, _make_judgment

    listener = _listener_with_audio()
    listener.state_manager.voice_collect_seconds = 0.3
    dispatched = []
    listener._dispatch_query = dispatched.append
    judge = _install_intent_judge(listener, _make_judgment(query="set a timer"))

    def slow_judgement(**_kwargs):
        # The user carries on ("for ten minutes") while the judge is still thinking.
        _queue_audio(listener, (0.3, QUIET), (1.0, VOICED), (0.8, QUIET))
        time.sleep(0.5)
        return _make_judgment(query="set a timer")

    judge.judge.side_effect = slow_judgement
    now = time.time()
    listener._handle_transcription_result(SimpleNamespace(
        text="jarvis set a timer", language="en", low_confidence_events=(), start_time=now - 1.5,
        end_time=now - 0.1, speech_end_time=now - 0.2, energy=0.1, dictation_generation=0,
        captured_during_tts=False, captured_tts_start_time=0.0, speaker_verdict=None))

    _drain_capture(listener)
    listener._check_query_timeout()
    assert dispatched == [], "the request left before the speech said meanwhile was heard"

    _hear_job_as(listener, "for ten minutes")
    time.sleep(0.4)
    listener._check_query_timeout()
    assert dispatched == ["set a timer for ten minutes"]
    listener.state_manager.stop()


def test_echo_waiting_in_the_backlog_keeps_its_during_tts_flag():
    import time
    listener = _listener_with_audio()
    listener.tts.is_speaking.return_value = True
    listener.track_tts_start("Here is a long answer about the weather in London today.")
    _queue_audio(listener, (1.0, VOICED), (0.7, QUIET))  # Jarvis's echo, captured while it spoke
    listener.tts.is_speaking.return_value = False        # the reply ends while the listener is busy
    listener.echo_detector.track_tts_finish()
    time.sleep(1.0)

    _drain_capture(listener)

    job = listener._transcription_jobs_q.get_nowait()
    assert job.captured_during_tts
    listener.state_manager.stop()


def test_the_capture_backlog_stays_bounded():
    listener = _listener(6.0, 20)

    delivered, kept = _capture(listener, 120.0, 20)

    assert kept < delivered
    listener.state_manager.stop()

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


def test_the_capture_backlog_stays_bounded():
    listener = _listener(6.0, 20)

    delivered, kept = _capture(listener, 120.0, 20)

    assert kept < delivered
    listener.state_manager.stop()

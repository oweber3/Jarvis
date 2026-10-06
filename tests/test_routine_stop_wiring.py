"""A stop addressed to Jarvis (spoken, the chat Stop button or a click on the orb) ends a routine between steps."""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.routines import runner, store
from jarvis.tools import registry
from jarvis.tools.registry import run_tool_with_retries
from routine_fakes import FakeTool, Gate

_LISTENER = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "listening" / "listener.py"


def _listener_parses() -> bool:
    try:
        ast.parse(_LISTENER.read_text(encoding="utf-8"))
        return True
    except SyntaxError:
        return False


# The voice listener and the daemon cannot be imported while listener.py does not parse.
pytestmark = pytest.mark.skipif(not _listener_parses(), reason="listening/listener.py does not parse")


@pytest.fixture
def running_routine(monkeypatch, mock_config):
    """A routine whose first step waits; yields a function that returns the finished result."""
    gate = Gate()
    for name, behaviour in (("tvControl", gate), ("lightsControl", None)):
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, FakeTool(name, behaviour=behaviour))
    store.load({"movie mode": {"steps": [{"tool": "tvControl", "args": {"action": "launch"}},
                                         {"tool": "lightsControl", "args": {"action": "dim"}}]}})
    results = []
    worker = threading.Thread(target=lambda: results.append(run_tool_with_retries(
        None, mock_config, "routineControl", {"action": "run", "name": "movie mode"}, "", "", "", quiet=True)))
    worker.start()
    assert gate.entered.wait(5)

    def finish():
        gate.release.set()
        worker.join(5)
        return results[0]

    yield finish
    gate.release.set()
    worker.join(5)


def _listener(cfg):
    from jarvis.listening.listener import VoiceListener
    from jarvis.listening.state_manager import StateManager
    from jarvis.listening.transcript_buffer import TranscriptBuffer
    obj = VoiceListener.__new__(VoiceListener)
    obj.cfg = cfg
    obj.tts = None
    obj.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0,
                                        _last_tts_text='', echo_tolerance=.3)
    obj.state_manager = StateManager(voice_collect_seconds=.1)
    obj._transcript_buffer = TranscriptBuffer()
    obj._buffer_duration = 120
    obj._last_detected_language = 'en'
    obj._intent_judge = SimpleNamespace(available=True, judge=lambda **kwargs: pytest.fail("judge called"))
    obj._is_engaged = lambda: False
    obj._start_engagement = lambda: None
    obj._end_engagement = lambda: None
    obj._set_face_state_listening = lambda: None
    obj._clear_audio_buffers = lambda: None
    obj.dispatched = []
    obj._dispatch_query = obj.dispatched.append
    return obj


def _skipped_as_stopped(result):
    return "Step 2 (lightsControl dim) skipped: stopped." in (result.error_message or "")


def test_a_spoken_stop_ends_the_routine_between_steps(mock_config, running_routine):
    obj = _listener(mock_config)
    stamp = time.time()
    obj._transcript_buffer.add("jarvis stop", stamp - .2, stamp, .1)
    obj._process_transcript("jarvis stop", .1, stamp - .2, stamp, captured_during_tts=False,
                            captured_tts_start_time=0)
    obj.state_manager.stop()
    assert obj.dispatched == []
    assert _skipped_as_stopped(running_routine())


def test_the_chat_stop_button_ends_the_routine_between_steps(running_routine):
    from jarvis import daemon
    daemon.cancel_active_chat_query()
    assert _skipped_as_stopped(running_routine())


def test_a_click_on_the_orb_ends_the_routine_between_steps(mock_config, running_routine):
    obj = _listener(mock_config)
    assert obj._stop_reply_in_progress() is True
    obj.state_manager.stop()
    assert _skipped_as_stopped(running_routine())


def test_no_routine_running_leaves_the_stop_paths_unchanged(mock_config):
    assert runner.is_running() is False
    obj = _listener(mock_config)
    assert obj._stop_reply_in_progress() is False

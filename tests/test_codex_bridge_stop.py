"""Stop commands and the chat Stop button cancel an in-flight background Codex request."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from jarvis.bridge import runtime


class FakeService:
    def __init__(self, busy=True):
        self.busy = busy
        self.cancelled = []

    def is_busy(self):
        return self.busy

    def cancel_active(self, reason):
        self.cancelled.append(reason)
        return self.busy


@pytest.fixture(autouse=True)
def isolated():
    runtime.set_service(None)
    yield
    runtime.set_service(None)


def make_listener(cfg, judge):
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
    obj._intent_judge = SimpleNamespace(available=True, judge=judge)
    obj._is_engaged = lambda: False
    obj._start_engagement = lambda: None
    obj._end_engagement = lambda: None
    obj._set_face_state_listening = lambda: None
    obj._clear_audio_buffers = lambda: None
    obj.dispatched = []
    obj._dispatch_query = obj.dispatched.append
    return obj


def process(obj, text):
    stamp = time.time()
    obj._transcript_buffer.add(text, stamp - .2, stamp, .1)
    obj._process_transcript(text, .1, stamp - .2, stamp, captured_during_tts=False, captured_tts_start_time=0)


def no_judge(**kwargs):
    from jarvis.listening.intent_judge import IntentJudgment
    return IntentJudgment(False, '', False, 'high', 'not directed')


@pytest.mark.unit
class TestRuntime:
    def test_cancel_without_a_service_is_a_noop(self):
        assert runtime.cancel_active_request("stop") is False
        assert runtime.request_active() is False

    def test_cancel_reaches_the_service(self):
        service = FakeService()
        runtime.set_service(service)
        assert runtime.request_active() is True
        assert runtime.cancel_active_request("stop") is True
        assert service.cancelled == ["stop"]


@pytest.mark.unit
class TestSpokenStop:
    def test_wake_word_plus_stop_cancels_without_dispatch_or_judge(self, mock_config):
        service = FakeService()
        runtime.set_service(service)

        def forbidden(**kwargs):
            pytest.fail("a stop command must not reach the intent judge")

        obj = make_listener(mock_config, forbidden)
        process(obj, "jarvis stop")
        assert service.cancelled == ["stop"]
        assert obj.dispatched == []
        assert all(seg.processed for seg in obj._transcript_buffer.get_last_seconds(120))
        obj.state_manager.stop()

    def test_ambient_stop_without_the_wake_word_does_not_cancel(self, mock_config):
        service = FakeService()
        runtime.set_service(service)
        obj = make_listener(mock_config, no_judge)
        process(obj, "stop")
        assert service.cancelled == []
        obj.state_manager.stop()

    def test_nothing_in_flight_means_the_normal_path(self, mock_config):
        service = FakeService(busy=False)
        runtime.set_service(service)
        obj = make_listener(mock_config, no_judge)
        process(obj, "jarvis stop")
        assert service.cancelled == []
        obj.state_manager.stop()

    def test_other_wake_word_speech_does_not_cancel(self, mock_config):
        service = FakeService()
        runtime.set_service(service)
        obj = make_listener(mock_config, no_judge)
        process(obj, "jarvis what time is it?")
        assert service.cancelled == []
        assert obj.dispatched == ["what time is it?"]
        obj.state_manager.stop()


@pytest.mark.unit
class TestChatStop:
    def test_chat_stop_button_cancels_a_codex_request(self):
        from jarvis import daemon

        service = FakeService()
        runtime.set_service(service)
        daemon.cancel_active_chat_query()
        assert service.cancelled == ["stop"]


@pytest.mark.unit
class TestCloudModelCannotEndTheConversation:
    """A cloud model calling the local ``stop`` tool would end the turn silently, so it is never offered."""

    @pytest.mark.parametrize("mode", ["codex", "claude"])
    def test_stop_tool_is_not_in_the_bridge_snapshot(self, mode):
        from jarvis.tools.registry import BUILTIN_TOOLS
        from jarvis.codex_bridge.service import codex_tool_snapshot
        from jarvis.claude_bridge.service import claude_tool_snapshot

        assert "stop" in BUILTIN_TOOLS
        cfg = SimpleNamespace(mcps={})
        snapshot = (codex_tool_snapshot if mode == "codex" else claude_tool_snapshot)(cfg)
        assert snapshot, "the bridge still offers the other tools"
        assert "stop" not in snapshot

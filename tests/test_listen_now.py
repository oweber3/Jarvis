"""Listening on request (a raised hand in gesture control): like a bare wake word, but it never ends a wait or a hot
window already running, and it silences a reply in progress before listening."""
import threading
import time
from unittest.mock import MagicMock, patch

from tests.test_voice_latency import FakeSpeakingTTS, hear, judged, make_voice_listener


def test_listen_now_waits_for_the_request_like_a_spoken_wake_word(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.listen_now()
    obj._check_query_timeout()
    assert obj.state_manager.is_collecting() and obj.dispatched == []
    hear(obj, 'what is the weather', speech_end=time.time())
    time.sleep(0.4)
    obj._check_query_timeout()
    assert obj.dispatched == ['what is the weather']
    obj.state_manager.stop()


def test_listen_now_while_already_waiting_keeps_waiting(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.listen_now()
    obj._check_query_timeout()
    obj.listen_now()
    obj._check_query_timeout()
    assert obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_listen_now_during_the_hot_window_keeps_it(mock_config):
    from jarvis.listening.state_manager import ListeningState
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.state_manager._state = ListeningState.HOT_WINDOW
    obj.state_manager._hot_window_start_time = time.time()  # just opened
    obj.listen_now()
    obj._check_query_timeout()
    assert obj.state_manager.get_state() == ListeningState.HOT_WINDOW
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_listen_now_while_speaking_lets_jarvis_finish_and_does_not_listen(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.tts = FakeSpeakingTTS(speaking=True)
    obj.listen_now()
    assert obj.tts.is_speaking()
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_listen_now_while_thinking_keeps_the_reply_and_does_not_listen(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    reply = threading.Event()
    obj._pending_replies = (reply,)
    obj.listen_now()
    assert not reply.is_set()
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_listen_now_is_ignored_while_dictating(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj._dictation_active = True
    obj.listen_now()
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_the_daemon_passes_listen_now_to_the_listener():
    from jarvis import daemon
    listener = MagicMock()
    with patch.object(daemon, "_global_voice_listener", listener), patch.object(daemon, "_chat_cancel_event", None):
        daemon.listen_now()
    listener.listen_now.assert_called_once_with()


def test_the_daemon_ignores_listen_now_during_a_typed_request_and_without_a_listener():
    from jarvis import daemon
    listener = MagicMock()
    cancel = threading.Event()
    with patch.object(daemon, "_global_voice_listener", listener), patch.object(daemon, "_chat_cancel_event", cancel):
        daemon.listen_now()
    assert not cancel.is_set()  # the typed request goes on
    listener.listen_now.assert_not_called()
    with patch.object(daemon, "_global_voice_listener", None):
        daemon.listen_now()

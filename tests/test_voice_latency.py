"""Voice-path latency behaviours: when audio starts and what the turn reports."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

sd = pytest.importorskip("sounddevice")

from jarvis.output.tts import PiperTTS


class FakeOutputStream:
    """Plays callback blocks in (accelerated) real time without an audio device."""

    instances: list = []

    def __init__(self, samplerate, channels, dtype, blocksize, callback, **_):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.active = False
        self.started_at = None
        self.played = []
        self._thread = None
        FakeOutputStream.instances.append(self)

    def start(self):
        self.active = True
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._play, daemon=True)
        self._thread.start()

    def _play(self):
        while self.active:
            out = np.zeros((self.blocksize, 1), dtype=np.int16)
            try:
                self.callback(out, self.blocksize, None, None)
            except (sd.CallbackStop, sd.CallbackAbort):
                self.played.append(out[:, 0].copy())
                self.active = False
                return
            self.played.append(out[:, 0].copy())
            time.sleep(0.001)

    def abort(self):
        self.active = False

    def stop(self):
        self.active = False

    def close(self):
        self.active = False


class FakeVoice:
    """Yields one chunk per sentence, taking ``delay`` seconds per sentence."""

    def __init__(self, delay=0.0, samples_per_sentence=2048):
        self.delay = delay
        self.samples = samples_per_sentence
        self.finished_at = None
        self.config = SimpleNamespace(sample_rate=22050)

    def synthesize(self, text, _config):
        sentences = [s for s in text.split(".") if s.strip()]
        for index, _sentence in enumerate(sentences):
            time.sleep(self.delay)
            yield SimpleNamespace(audio_int16_array=np.full(self.samples, index + 1, dtype=np.int16))
        self.finished_at = time.monotonic()


@pytest.fixture
def fake_audio(monkeypatch):
    FakeOutputStream.instances = []
    monkeypatch.setattr(sd, "OutputStream", FakeOutputStream)
    return FakeOutputStream


def make_tts(voice):
    tts = PiperTTS(enabled=True)
    tts._voice = voice
    tts._sample_rate = voice.config.sample_rate
    tts._initialized = True
    return tts


def speak_and_wait(tts, text, timeout=5.0, **callbacks):
    done = threading.Event()
    user_completion = callbacks.pop("completion_callback", None)

    def _complete():
        if user_completion:
            user_completion()
        done.set()

    tts.speak(text, completion_callback=_complete, **callbacks)
    assert done.wait(timeout), "speech did not complete"


def test_first_audio_is_reported_once_when_playback_starts(fake_audio):
    events = []
    tts = make_tts(FakeVoice())
    tts.start()
    try:
        speak_and_wait(
            tts, "Hello there. How are you.",
            first_audio_callback=lambda: events.append(("first", tts.is_speaking())),
            completion_callback=lambda: events.append(("done", None)),
        )
    finally:
        tts.stop()
    assert events[0] == ("first", True)
    assert events[-1] == ("done", None)
    assert [name for name, _ in events].count("first") == 1


def test_playback_starts_before_the_whole_reply_is_synthesised(fake_audio):
    voice = FakeVoice(delay=0.15)
    tts = make_tts(voice)
    tts.start()
    try:
        speak_and_wait(tts, "One. Two. Three. Four.")
    finally:
        tts.stop()
    stream = fake_audio.instances[-1]
    assert stream.started_at < voice.finished_at


def test_streamed_playback_keeps_every_sentence_in_order(fake_audio):
    durations = []
    voice = FakeVoice(delay=0.05, samples_per_sentence=1500)
    tts = make_tts(voice)
    tts.start()
    try:
        speak_and_wait(tts, "One. Two. Three.", duration_callback=durations.append)
    finally:
        tts.stop()
    played = np.concatenate(fake_audio.instances[-1].played)
    voiced = played[played != 0]
    assert voiced.tolist() == [1] * 1500 + [2] * 1500 + [3] * 1500
    assert durations == [pytest.approx(4500 / voice.config.sample_rate)]


def test_interrupt_while_synthesising_stops_without_completing(fake_audio):
    completed = []
    voice = FakeVoice(delay=0.2)
    tts = make_tts(voice)
    tts.start()
    try:
        tts.speak("One. Two. Three. Four. Five. Six.", completion_callback=lambda: completed.append(True))
        deadline = time.monotonic() + 3
        while not fake_audio.instances and time.monotonic() < deadline:
            time.sleep(0.01)
        tts.interrupt()
        deadline = time.monotonic() + 3
        while tts.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not tts.is_speaking()
        assert voice.finished_at is None
        assert completed == []
    finally:
        tts.stop()


# --------------------------------------------------------------------------
# Collection window: measured from the end of speech, held open while the
# user is still talking, and never discarding speech already under way.


def make_voice_listener(cfg, judge, *, collect_seconds=0.3):
    import queue
    from jarvis.listening.listener import VoiceListener
    from jarvis.listening.state_manager import StateManager
    from jarvis.listening.transcript_buffer import TranscriptBuffer

    obj = VoiceListener.__new__(VoiceListener)
    obj.cfg = cfg
    obj.tts = None
    obj.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0,
                                        _last_tts_text='', echo_tolerance=.3)
    obj.state_manager = StateManager(voice_collect_seconds=collect_seconds, wake_wait_seconds=5.0)
    obj._transcript_buffer = TranscriptBuffer()
    obj._buffer_duration = 120
    obj._last_detected_language = 'en'
    obj._intent_judge = SimpleNamespace(available=True, judge=judge)
    obj._is_engaged = lambda: False
    obj._start_engagement = lambda: None
    obj._end_engagement = lambda: None
    obj._set_face_state_listening = lambda: None
    obj._should_stop = False
    obj._dictation_is_active = False
    obj._dictation_generation = 0
    obj._first_utterance = False
    obj.on_low_confidence = None
    obj._transcription_jobs_q = queue.Queue()
    obj._transcription_results_q = queue.Queue()
    obj._audio_q = queue.Queue()
    obj._pre_roll = []
    obj._utterance_frames = []
    obj._pending_audio = None
    obj.is_speech_active = False
    obj._silence_frames = 0
    obj._wake_timestamp = None
    obj._request_addressed = True
    obj.dispatched = []
    obj._dispatch_query = obj.dispatched.append
    return obj


def hear(obj, text, speech_end):
    obj._handle_transcription_result(SimpleNamespace(
        text=text, language='en', low_confidence_events=(), start_time=speech_end - 1.0,
        end_time=speech_end + 0.6, speech_end_time=speech_end, energy=.1,
        dictation_generation=0, captured_during_tts=False, captured_tts_start_time=0))


def judged(query):
    from jarvis.listening.intent_judge import IntentJudgment
    return lambda **kw: IntentJudgment(True, query, False, 'high', 'directed')


def test_query_dispatches_once_the_pause_after_speech_has_passed(mock_config):
    obj = make_voice_listener(mock_config, judged('what is the capital of France'), collect_seconds=0.3)
    # Whisper and the judge already took longer than the pause.
    hear(obj, 'jarvis what is the capital of france', speech_end=time.time() - 0.5)
    obj._check_query_timeout()
    assert obj.dispatched == ['what is the capital of France']
    obj.state_manager.stop()


def test_ongoing_speech_keeps_the_query_open_and_is_merged(mock_config):
    obj = make_voice_listener(mock_config, judged('what is the weather'), collect_seconds=0.3)
    hear(obj, 'jarvis what is the weather', speech_end=time.time() - 0.5)
    obj.is_speech_active = True  # the user carried on talking
    obj._check_query_timeout()
    assert obj.dispatched == []
    obj.is_speech_active = False
    hear(obj, 'in paris', speech_end=time.time() - 0.5)
    obj._check_query_timeout()
    assert obj.dispatched == ['what is the weather in paris']
    obj.state_manager.stop()


def test_queued_transcription_keeps_the_query_open(mock_config):
    obj = make_voice_listener(mock_config, judged('what is the weather'), collect_seconds=0.3)
    hear(obj, 'jarvis what is the weather', speech_end=time.time() - 0.5)
    obj._transcription_jobs_q.put(object())  # a continuation awaiting Whisper
    obj._check_query_timeout()
    assert obj.dispatched == []
    obj.state_manager.stop()


def test_accepting_a_query_keeps_speech_already_under_way(mock_config):
    obj = make_voice_listener(mock_config, judged('what is the weather'))
    frame = np.ones(320, dtype=np.float32)
    obj.is_speech_active = True
    obj._utterance_frames = [frame]
    hear(obj, 'jarvis what is the weather', speech_end=time.time())
    assert obj.state_manager.is_collecting()
    assert obj.is_speech_active
    assert len(obj._utterance_frames) == 1
    obj.state_manager.stop()


def test_bare_wake_word_waits_for_the_request(mock_config):
    from jarvis.listening.intent_judge import IntentJudgment
    obj = make_voice_listener(mock_config, lambda **kw: IntentJudgment(True, '', False, 'low', 'wake only'))
    hear(obj, 'jarvis', speech_end=time.time() - 1.0)
    obj._check_query_timeout()
    assert obj.dispatched == [] and obj.state_manager.is_collecting()
    hear(obj, 'what is the weather', speech_end=time.time() - 0.5)
    obj._check_query_timeout()
    assert obj.dispatched == ['what is the weather']
    obj.state_manager.stop()


def test_manual_wake_waits_for_the_request_like_a_spoken_wake_word(mock_config):
    # Clicking the orb stands in for saying "Jarvis".
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    assert obj.state_manager.is_collecting() and obj.dispatched == []
    hear(obj, 'what is the weather', speech_end=time.time())
    time.sleep(0.4)  # the pause after the request is measured from its last speech
    obj._check_query_timeout()
    assert obj.dispatched == ['what is the weather']
    obj.state_manager.stop()


def test_manual_wake_is_consumed_once(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    obj.state_manager.clear_collection()
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_a_second_click_while_waiting_for_the_request_deactivates(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    assert obj.state_manager.is_collecting()
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    assert obj.dispatched == []
    obj.state_manager.stop()


def test_deactivating_discards_the_partly_heard_request(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    hear(obj, 'what is the', speech_end=time.time())
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    time.sleep(0.4)
    obj._check_query_timeout()
    assert obj.dispatched == []
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_a_click_during_the_hot_window_ends_it(mock_config):
    from jarvis.listening.state_manager import ListeningState
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.state_manager._state = ListeningState.HOT_WINDOW
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    assert obj.state_manager.get_state() == ListeningState.WAKE_WORD
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_a_third_click_wakes_again(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    for _ in range(3):
        obj.toggle_manual_wake()
        obj._check_query_timeout()
    assert obj.state_manager.is_collecting()
    obj.state_manager.stop()


class FakeSpeakingTTS:
    enabled = True

    def __init__(self, speaking):
        self.speaking = speaking

    def is_speaking(self):
        return self.speaking

    def interrupt(self):
        self.speaking = False


def test_a_click_while_thinking_stops_the_reply_and_does_not_wake(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    reply = threading.Event()
    obj._pending_replies = (reply,)
    obj.toggle_manual_wake()
    assert reply.is_set()  # stopped at once, not on the listener's next tick
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_a_click_while_speaking_silences_jarvis_and_does_not_wake(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.tts = FakeSpeakingTTS(speaking=True)
    obj.toggle_manual_wake()
    assert not obj.tts.is_speaking()
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_a_click_cancels_a_background_bridge_request(mock_config, monkeypatch):
    from jarvis.bridge import runtime
    cancelled = []
    monkeypatch.setattr(runtime, "request_active", lambda: True)
    monkeypatch.setattr(runtime, "cancel_active_request", lambda reason: cancelled.append(reason) or True)
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    assert cancelled and not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_the_click_after_a_stop_wakes_again(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj.tts = FakeSpeakingTTS(speaking=True)
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    obj.toggle_manual_wake()
    obj._check_query_timeout()
    assert obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_manual_wake_does_nothing_without_a_request(mock_config):
    obj = make_voice_listener(mock_config, judged('unused'))
    obj._check_query_timeout()
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


@pytest.mark.parametrize('utterance', ['jarvis', 'jarvis?', 'jarvis!', 'jarvis.'])
def test_bare_wake_word_is_not_turned_into_a_question_by_the_judge(mock_config, utterance):
    # A small judge can invent a query from the wake word alone ("Jarvis?" -> "what is Jarvis?").
    # Nothing but the wake word was said, so Jarvis waits for the request instead.
    from jarvis.listening.intent_judge import IntentJudgment
    obj = make_voice_listener(
        mock_config, lambda **kw: IntentJudgment(True, 'what is jarvis?', False, 'high', 'question'))
    hear(obj, utterance, speech_end=time.time() - 1.0)
    obj._check_query_timeout()
    assert obj.dispatched == [] and obj.state_manager.is_collecting()
    hear(obj, 'open notepad', speech_end=time.time() - 0.5)
    obj._check_query_timeout()
    assert obj.dispatched == ['open notepad']
    obj.state_manager.stop()


def test_listener_uses_configured_collection_pauses(mock_config):
    from jarvis.listening.listener import VoiceListener
    mock_config.voice_collect_seconds = 0.7
    mock_config.voice_wake_wait_seconds = 3.3
    listener = VoiceListener(None, mock_config, None, None)
    assert listener.state_manager.voice_collect_seconds == 0.7
    assert listener.state_manager.wake_wait_seconds == 3.3
    listener.state_manager.stop()


# --------------------------------------------------------------------------
# Replies are generated off the listener thread, so speech is still heard
# while a reply is being produced and an engaged stop can cancel it.


class BlockingEngine:
    """Stands in for run_reply_engine; each call waits until released."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, db, cfg, tts, query, dialogue_memory, language=None, **_):
        self.calls.append(query)
        self.started.set()
        assert self.release.wait(5), "engine never released"
        return f"reply to {query}"


@pytest.fixture
def engine(monkeypatch):
    from jarvis.reply import engine as engine_module
    fake = BlockingEngine()
    monkeypatch.setattr(engine_module, "run_reply_engine", fake)
    return fake


def make_speaking_listener(cfg, engine):
    from jarvis.listening.listener import VoiceListener
    obj = make_voice_listener(cfg, judged('unused'))
    obj._dispatch_query = VoiceListener._dispatch_query.__get__(obj)
    obj.db = obj.dialogue_memory = None
    obj.spoken = []
    obj.tts = SimpleNamespace(
        enabled=True, is_speaking=lambda: False,
        speak=lambda text, **callbacks: obj.spoken.append(text))
    obj.track_tts_start = lambda text: None
    return obj


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_dispatch_returns_while_the_reply_is_still_being_generated(mock_config, engine):
    obj = make_speaking_listener(mock_config, engine)
    started = time.monotonic()
    obj._dispatch_query('tell me a story')
    assert time.monotonic() - started < 0.5
    assert engine.started.wait(2)
    assert obj.spoken == []
    engine.release.set()
    assert wait_until(lambda: obj.spoken == ['reply to tell me a story'])
    obj.stop()


def test_engaged_stop_while_generating_drops_the_reply(mock_config, engine):
    obj = make_speaking_listener(mock_config, engine)
    obj._dispatch_query('tell me a long story')
    assert engine.started.wait(2)
    hear(obj, 'jarvis stop', speech_end=time.time())
    engine.release.set()
    time.sleep(0.3)
    assert obj.spoken == []
    assert engine.calls == ['tell me a long story']  # the stop was not dispatched as a query
    obj.stop()


def test_ambient_stop_while_generating_does_not_cancel(mock_config, engine):
    obj = make_speaking_listener(mock_config, engine)
    obj._dispatch_query('tell me a story')
    assert engine.started.wait(2)
    hear(obj, 'stop', speech_end=time.time())
    engine.release.set()
    assert wait_until(lambda: obj.spoken == ['reply to tell me a story'])
    obj.stop()


def test_queries_dispatched_while_busy_are_answered_in_order(mock_config, engine):
    obj = make_speaking_listener(mock_config, engine)
    obj._dispatch_query('first')
    obj._dispatch_query('second')
    engine.release.set()
    assert wait_until(lambda: obj.spoken == ['reply to first', 'reply to second'])
    obj.stop()


def test_back_to_back_replies_keep_their_own_callbacks(fake_audio):
    events = []
    done = threading.Event()
    tts = make_tts(FakeVoice())
    tts.start()
    try:
        tts.speak("First reply.", completion_callback=lambda: events.append("first done"),
                  first_audio_callback=lambda: events.append("first audio"))

        def _second_done():
            events.append("second done")
            done.set()

        tts.speak("Second reply.", completion_callback=_second_done,
                  first_audio_callback=lambda: events.append("second audio"))
        assert done.wait(5)
    finally:
        tts.stop()
    assert events == ["first audio", "first done", "second audio", "second done"]


def test_engaged_stop_while_collecting_cancels_the_request(mock_config):
    obj = make_voice_listener(mock_config, judged('tell me a long story'), collect_seconds=0.3)
    hear(obj, 'jarvis tell me a long story', speech_end=time.time())
    assert obj.state_manager.is_collecting()
    hear(obj, 'jarvis stop', speech_end=time.time())
    time.sleep(0.4)
    obj._check_query_timeout()
    assert obj.dispatched == []
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()

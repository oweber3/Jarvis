"""The wake word sound detector: early feedback from the sound of "Jarvis", confirmed by the transcript."""

import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState
from jarvis.listening import wake_sound
from jarvis.listening.wake_sound import CHUNK_SAMPLES, WakeSoundDetector, create_wake_sound_detector
from tests.test_hot_window_input import _create_listener

pytestmark = pytest.mark.unit

RATE = 16000


# ---------------------------------------------------------------------------
# Fake openWakeWord models: loud audio scores high, quiet audio scores zero
# ---------------------------------------------------------------------------

class _Input:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape


class _FakeMelspec:
    def get_inputs(self):
        return [_Input("input", ["batch", "samples"])]

    def run(self, _outputs, feeds):
        audio = feeds["input"]
        frames = max(1, audio.shape[1] // 160 - 3)
        level = float(np.mean(np.abs(audio))) / 1000.0
        return [np.full((frames, 1, 1, 32), level, dtype=np.float32)]


class _FakeEmbedding:
    def get_inputs(self):
        return [_Input("input_1", ["batch", 76, 32, 1])]

    def run(self, _outputs, feeds):
        windows = feeds["input_1"]
        recent = windows[:, -8:, :, 0].mean(axis=(1, 2))
        return [np.repeat(recent[:, None, None, None], 96, axis=3)]


class _FakePhrase:
    def get_inputs(self):
        return [_Input("x.1", [1, 16, 96])]

    def run(self, _outputs, feeds):
        newest = float(feeds["x.1"][0, -1].mean())
        return [np.array([[min(1.0, max(0.0, newest - 2.0))]], dtype=np.float32)]


def _fake_session(path: Path):
    return {"mel": _FakeMelspec(), "emb": _FakeEmbedding(), "phrase": _FakePhrase()}[path.name]


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _detector(clock=None):
    return WakeSoundDetector(Path("mel"), Path("emb"), Path("phrase"), threshold=0.5,
                             clock=clock or _Clock(), load_session=_fake_session)


def _silence(seconds):
    return np.zeros(int(RATE * seconds), dtype=np.int16)


def _loud(seconds):
    return np.full(int(RATE * seconds), 20000, dtype=np.int16)


def _feed(detector, audio, block=320):
    return sum(detector.feed(audio[i:i + block]) for i in range(0, len(audio), block))


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def test_silence_is_never_heard_as_the_wake_word():
    assert _feed(_detector(), _silence(3.0)) == 0


def test_one_spoken_phrase_is_one_detection():
    detector = _detector()
    heard = _feed(detector, np.concatenate((_silence(1.0), _loud(0.6), _silence(1.0))))
    assert heard == 1


def test_a_second_phrase_inside_the_cooldown_is_not_reported_again():
    clock = _Clock()
    detector = _detector(clock)
    phrase = np.concatenate((_loud(0.6), _silence(0.5)))
    _feed(detector, _silence(1.0))
    assert _feed(detector, phrase) == 1
    clock.now += 0.5
    assert _feed(detector, phrase) == 0
    clock.now += wake_sound.DEFAULT_COOLDOWN_SEC
    assert _feed(detector, phrase) == 1


def test_detection_does_not_depend_on_how_the_audio_is_split():
    audio = np.concatenate((_silence(1.0), _loud(0.6), _silence(1.0)))
    small, large = _detector(), _detector()
    assert _feed(small, audio, block=320) == _feed(large, audio, block=1000) == 1
    assert small.last_score == large.last_score


def test_reset_forgets_audio_already_heard():
    detector = _detector()
    detector.feed(_loud(0.3))
    detector.reset()
    assert _feed(detector, _silence(1.0)) == 0


def test_audio_is_processed_in_80_ms_steps():
    detector = _detector()
    detector.feed(_loud(0.0)[:0])
    detector.feed(np.full(CHUNK_SAMPLES - 1, 20000, dtype=np.int16))
    assert detector.last_score == 0.0  # not a full step yet


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _cfg(**overrides):
    values = dict(wake_sound_enabled=True, wake_sound_model="hey_jarvis", wake_sound_threshold=0.5,
                  wake_word="jarvis")
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.utils.model_files.models_dir", lambda: tmp_path)
    return tmp_path


def test_nothing_loads_or_downloads_when_switched_off(models_dir):
    def fetch(_url):
        raise AssertionError("must not download")
    assert create_wake_sound_detector(_cfg(wake_sound_enabled=False), fetch=fetch) is None


def test_a_failed_download_leaves_detection_off_without_breaking_startup(models_dir, capsys):
    def fetch(_url):
        raise OSError("offline")
    assert create_wake_sound_detector(_cfg(), fetch=fetch) is None
    assert "Wake word sound detection is off" in capsys.readouterr().out


def test_a_download_that_fails_its_checksum_is_never_saved(models_dir):
    assert create_wake_sound_detector(_cfg(), fetch=lambda _url: b"not the model") is None
    assert not any(models_dir.iterdir())


def test_the_built_in_model_stays_off_for_a_different_wake_word(models_dir, capsys):
    def fetch(_url):
        raise AssertionError("must not download")
    assert create_wake_sound_detector(_cfg(wake_word="friday"), fetch=fetch) is None
    assert "only knows" in capsys.readouterr().out


def test_a_custom_model_path_that_does_not_exist_leaves_detection_off(models_dir, monkeypatch):
    monkeypatch.setattr(wake_sound, "ensure_verified_file", lambda filename, *_a, **_k: models_dir / filename)
    cfg = _cfg(wake_sound_model=str(models_dir / "missing.onnx"), wake_word="friday")
    assert create_wake_sound_detector(cfg) is None


# ---------------------------------------------------------------------------
# Listener: early feedback, confirmed or withdrawn by the transcript
# ---------------------------------------------------------------------------

class _ScriptedDetector:
    """Hears the wake word on the feeds listed in ``hits`` (0-based)."""

    def __init__(self, hits=(0,), error=None):
        self.hits = set(hits)
        self.error = error
        self.feeds = 0
        self.resets = 0

    def feed(self, pcm):
        if self.error is not None:
            raise self.error
        heard = self.feeds in self.hits
        self.feeds += 1
        return heard

    def reset(self):
        self.resets += 1


@pytest.fixture
def face_states():
    assistant_state.reset()
    states = []
    assistant_state.subscribe(states.append)
    yield states
    assistant_state.reset()


def _listening_listener(detector):
    listener, tts = _create_listener()
    listener._configure_audio(20)
    listener._wake_sound = detector
    assistant_state.set_state(AssistantState.IDLE)
    return listener, tts


def _quiet_frame():
    return np.zeros((320, 1), dtype=np.float32)


def _hear(listener, text):
    now = time.time()
    listener._process_transcript(text, 0.01, now - 1.0, now - 0.5,
                                 captured_during_tts=False, captured_tts_start_time=0.0)


def test_the_face_reacts_as_soon_as_the_wake_word_is_heard(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    assert assistant_state.current() is AssistantState.LISTENING
    listener.state_manager.stop()


def test_the_face_returns_to_idle_when_the_transcript_does_not_confirm_it(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    _hear(listener, "Travis went to the store.")

    listener._settle_wake_sound(now=time.time() + 5.0)

    assert assistant_state.current() is AssistantState.IDLE
    assert not listener.state_manager.is_collecting()
    listener.state_manager.stop()


def test_the_face_waits_while_the_phrase_is_still_being_transcribed(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    listener.is_speech_active = True  # the user is still talking

    listener._settle_wake_sound(now=time.time() + 5.0)

    assert assistant_state.current() is AssistantState.LISTENING
    listener.state_manager.stop()


def test_a_confirmed_wake_word_carries_on_into_the_request(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    _hear(listener, "Jarvis.")

    listener._settle_wake_sound(now=time.time() + 5.0)

    assert listener.state_manager.is_collecting()
    assert assistant_state.current() is AssistantState.LISTENING
    listener.state_manager.stop()


def test_the_sound_alone_never_starts_a_request(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    assert not listener.state_manager.is_collecting()
    assert not listener._is_engaged()
    listener.state_manager.stop()


@pytest.mark.parametrize("state", [AssistantState.THINKING, AssistantState.SPEAKING, AssistantState.DICTATING])
def test_hearing_the_wake_word_never_overrides_another_state(face_states, state):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    assistant_state.set_state(state)
    listener._process_audio_block(_quiet_frame())
    listener._settle_wake_sound(now=time.time() + 5.0)
    assert assistant_state.current() is state
    listener.state_manager.stop()


def test_a_failing_detector_is_switched_off_and_listening_continues(face_states, capsys):
    listener, _ = _listening_listener(_ScriptedDetector(error=RuntimeError("onnx failed")))
    listener._process_audio_block(_quiet_frame())
    listener._process_audio_block(_quiet_frame())
    assert listener._wake_sound is None
    assert assistant_state.current() is AssistantState.IDLE
    assert "Wake word sound detection stopped" in capsys.readouterr().out
    listener.state_manager.stop()


def test_cleared_audio_also_clears_what_the_detector_heard(face_states):
    detector = _ScriptedDetector(hits=())
    listener, _ = _listening_listener(detector)
    listener._clear_audio_buffers()
    assert detector.resets == 1
    listener.state_manager.stop()


def _result(listener, text, start, end):
    from jarvis.listening.listener import _TranscriptionResult
    return _TranscriptionResult(text=text, language="en", low_confidence_events=(), start_time=start,
                                end_time=end, speech_end_time=end, energy=0.01,
                                dictation_generation=listener._dictation_generation,
                                captured_during_tts=False, captured_tts_start_time=0.0)


def test_continuing_speech_does_not_keep_the_face_lit_after_its_transcript(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    heard_at = listener._wake_sound_at
    listener.is_speech_active = True  # the room keeps talking

    listener._apply_transcription_result(_result(listener, "Travis went to the store.", heard_at - 1.0, heard_at + 0.5))

    assert assistant_state.current() is AssistantState.IDLE
    listener.state_manager.stop()


def test_an_earlier_utterance_transcribed_later_does_not_settle_the_detection(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    heard_at = listener._wake_sound_at
    listener.is_speech_active = True

    listener._apply_transcription_result(_result(listener, "something said before", heard_at - 3.0, heard_at - 1.0))

    assert assistant_state.current() is AssistantState.LISTENING
    listener.state_manager.stop()


def test_a_detection_whose_audio_never_reached_a_transcript_settles_eventually(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener._process_audio_block(_quiet_frame())
    listener.is_speech_active = True  # speech never stops, and that utterance never comes back

    listener._settle_wake_sound(now=time.time() + 2.0)
    assert assistant_state.current() is AssistantState.LISTENING
    listener._settle_wake_sound(now=time.time() + 60.0)
    assert assistant_state.current() is AssistantState.IDLE
    listener.state_manager.stop()


def test_the_wake_word_sound_during_the_hot_window_changes_nothing(face_states):
    listener, _ = _listening_listener(_ScriptedDetector(hits=(0,)))
    listener.state_manager.schedule_hot_window_activation()
    deadline = time.time() + 2.0
    while not listener.state_manager.is_hot_window_active() and time.time() < deadline:
        time.sleep(0.02)
    assert assistant_state.current() is AssistantState.LISTENING

    listener._process_audio_block(_quiet_frame())
    listener._settle_wake_sound(now=time.time() + 60.0)

    assert assistant_state.current() is AssistantState.LISTENING
    assert listener._wake_sound_at is None
    listener.state_manager.stop()

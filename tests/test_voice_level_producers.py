"""The microphone and Jarvis's voice publish how loud they are, so the orb can follow them."""

import numpy as np
import pytest

from jarvis import voice_levels
from jarvis.voice_levels import LevelChannel, Voice

pytestmark = pytest.mark.unit


@pytest.fixture
def channel(tmp_path, monkeypatch):
    channel = LevelChannel(str(tmp_path / "levels"))
    monkeypatch.setattr(voice_levels, "_channel", channel)
    return channel


def _tone(amplitude, samples, dtype=np.float32):
    wave = amplitude * np.sin(np.linspace(0, 40 * np.pi, samples))
    return wave.astype(dtype)


class TestMicrophone:
    def test_listening_publishes_how_loud_the_microphone_is(self, channel):
        from tests.test_audio_capture_pipeline import listener

        obj = listener(16000)
        obj._is_speech_frame(_tone(0.001, obj._frame_samples))
        quiet = channel.level(Voice.MICROPHONE)
        obj._is_speech_frame(_tone(0.3, obj._frame_samples))
        loud = channel.level(Voice.MICROPHONE)
        assert quiet is not None and loud is not None
        assert loud > quiet + 0.5

    def test_audio_the_listener_catches_up_on_is_not_shown_as_live(self, channel):
        import time
        from tests.test_audio_capture_pipeline import listener

        obj = listener(16000)
        frame = _tone(0.3, obj._frame_samples)
        obj._is_speech_frame(frame, captured_at=time.time() - 1.0)
        assert channel.level(Voice.MICROPHONE) is None        # a second old: too late to move the orb
        obj._is_speech_frame(frame, captured_at=time.time())
        assert channel.level(Voice.MICROPHONE) > 0.6

    def test_dictation_publishes_how_loud_the_microphone_is(self, channel):
        from jarvis.dictation.dictation_engine import DictationEngine

        engine = DictationEngine.__new__(DictationEngine)   # only the capture callback: no hotkey library needed
        engine._recording = True
        engine._audio_frames = []
        engine._audio_callback(_tone(0.3, 1600)[:, None], 1600, None, None)
        assert channel.level(Voice.MICROPHONE) > 0.6

    def test_dictation_publishes_nothing_while_not_recording(self, channel):
        from jarvis.dictation.dictation_engine import DictationEngine

        engine = DictationEngine.__new__(DictationEngine)   # only the capture callback: no hotkey library needed
        engine._recording = False
        engine._audio_frames = []
        engine._audio_callback(_tone(0.3, 1600)[:, None], 1600, None, None)
        assert channel.level(Voice.MICROPHONE) is None


class TestJarvisVoice:
    def _play(self, audio, blocks=1):
        out = np.zeros(1024, dtype=np.int16)
        for _ in range(blocks):
            audio.fill(out)
        return out

    def test_playing_a_reply_publishes_how_loud_it_is(self, channel):
        from jarvis.output.tts import _StreamedAudio

        audio = _StreamedAudio()
        audio.append(_tone(12000, 1024 * 4, np.int16))
        self._play(audio)
        assert channel.level(Voice.JARVIS) > 0.6

    def test_a_pause_in_the_reply_reads_as_quiet(self, channel):
        from jarvis.output.tts import _StreamedAudio

        audio = _StreamedAudio()
        audio.append(_tone(12000, 1024, np.int16))
        self._play(audio)
        self._play(audio)                     # nothing queued yet: an underrun plays silence
        assert channel.level(Voice.JARVIS) == 0.0

    def test_the_voice_still_moves_the_orb_when_the_pc_speakers_are_off(self, channel):
        from jarvis.output.tts import _StreamedAudio

        audio = _StreamedAudio(audible=False)
        audio.append(_tone(12000, 1024 * 4, np.int16))
        out = self._play(audio)
        assert not out.any()                  # the PC stays silent
        assert channel.level(Voice.JARVIS) > 0.6

    def test_a_ducked_reply_reads_quieter(self, channel):
        from jarvis.output.tts import _StreamedAudio

        def level_at(gain):
            audio = _StreamedAudio()
            audio.set_gain(gain)
            audio._gain = gain
            audio.append(_tone(12000, 1024 * 4, np.int16))
            self._play(audio)
            return channel.level(Voice.JARVIS)

        assert level_at(0.2) < level_at(1.0)

    def test_the_orb_follows_jarvis_while_it_speaks_over_the_microphone(self, channel):
        from jarvis.output.tts import _StreamedAudio
        from tests.test_audio_capture_pipeline import listener

        obj = listener(16000)
        audio = _StreamedAudio()
        audio.append(_tone(3000, 1024 * 4, np.int16))
        self._play(audio)
        obj._is_speech_frame(_tone(0.5, obj._frame_samples))   # the microphone hears something louder
        assert voice_levels.current() == pytest.approx(channel.level(Voice.JARVIS))

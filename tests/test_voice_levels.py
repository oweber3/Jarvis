"""The live voice loudness the daemon shares with the orb, across processes."""

import os
import subprocess
import sys

import pytest

from jarvis import voice_levels
from jarvis.voice_levels import LevelChannel, Voice, level_from_rms

pytestmark = pytest.mark.unit


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "levels")


class TestLevelChannel:
    def test_a_level_published_by_one_channel_is_read_by_another(self, path):
        writer, reader = LevelChannel(path), LevelChannel(path)
        writer.publish(Voice.MICROPHONE, 0.6, now=100.0)
        assert reader.current(now=100.1) == pytest.approx(0.6)

    def test_nothing_published_reads_as_absent(self, path):
        assert LevelChannel(path).current(now=100.0) is None

    def test_a_stale_level_reads_as_absent(self, path):
        channel = LevelChannel(path)
        channel.publish(Voice.MICROPHONE, 0.6, now=100.0)
        assert channel.current(now=100.0 + voice_levels.MAX_AGE_S + 0.1) is None

    def test_jarvis_voice_wins_while_it_is_playing(self, path):
        channel = LevelChannel(path)
        channel.publish(Voice.MICROPHONE, 0.2, now=100.0)
        channel.publish(Voice.JARVIS, 0.9, now=100.0)
        assert channel.current(now=100.05) == pytest.approx(0.9)

    def test_the_microphone_takes_over_once_jarvis_stops(self, path):
        channel = LevelChannel(path)
        channel.publish(Voice.JARVIS, 0.9, now=100.0)
        channel.publish(Voice.MICROPHONE, 0.3, now=101.0)
        assert channel.current(now=101.05) == pytest.approx(0.3)

    def test_levels_are_clamped(self, path):
        channel = LevelChannel(path)
        channel.publish(Voice.MICROPHONE, 7.0, now=100.0)
        assert channel.current(now=100.0) == 1.0
        channel.publish(Voice.MICROPHONE, -1.0, now=100.0)
        assert channel.current(now=100.0) == 0.0

    def test_an_unusable_location_never_raises(self, tmp_path):
        blocked = tmp_path / "a-folder"
        blocked.mkdir()                       # a folder where the file should be
        channel = LevelChannel(str(blocked))
        channel.publish(Voice.MICROPHONE, 0.5)
        assert channel.current() is None

    def test_another_process_sees_the_level(self, path):
        src = os.path.join(os.path.dirname(__file__), "..", "src")
        code = ("import sys; sys.path.insert(0, sys.argv[2]); from jarvis.voice_levels import LevelChannel, Voice; "
                "LevelChannel(sys.argv[1]).publish(Voice.JARVIS, 0.7, now=1000.0)")
        subprocess.run([sys.executable, "-c", code, path, src], check=True, timeout=60)
        assert LevelChannel(path).current(now=1000.1) == pytest.approx(0.7)


    def test_a_reader_in_another_process_never_sees_half_a_write(self, path):
        src = os.path.join(os.path.dirname(__file__), "..", "src")
        code = ("import sys, time; sys.path.insert(0, sys.argv[2]); from jarvis.voice_levels import LevelChannel, Voice; "
                "c = LevelChannel(sys.argv[1]); i = 0\n"
                "while True:\n"
                "    c.publish(Voice.JARVIS, 0.25 if i % 2 else 0.75); i += 1\n"
                "    time.sleep(0.001)\n")   # far faster than the real producers (about 20 to 50 levels a second)
        writer = subprocess.Popen([sys.executable, "-c", code, path, src])
        try:
            channel = LevelChannel(path)
            import time
            deadline = time.time() + 20
            while channel.level(Voice.JARVIS) is None and time.time() < deadline:
                time.sleep(0.01)
            seen, end = [], time.time() + 1.5
            while time.time() < end:
                seen.append(channel.level(Voice.JARVIS))
        finally:
            writer.kill()                     # it writes until stopped, so every read above is fresh
            writer.wait(timeout=30)
        assert len(seen) > 1000
        assert set(seen) <= {0.25, 0.75}, {v for v in seen if v not in (0.25, 0.75)}


class TestLoudness:
    def test_silence_is_zero_and_full_scale_is_one(self):
        assert level_from_rms(0.0) == 0.0
        assert level_from_rms(1.0) == 1.0

    def test_room_noise_stays_near_zero_and_speech_moves_well_up(self):
        assert level_from_rms(0.002) < 0.1          # about -54 dBFS: a quiet room
        assert 0.4 < level_from_rms(0.03) < 0.9      # about -30 dBFS: normal speech

    def test_louder_is_always_higher(self):
        levels = [level_from_rms(10 ** (db / 20)) for db in range(-50, 0, 5)]
        assert levels == sorted(levels) and len(set(levels)) == len(levels)

    def test_int16_audio_uses_its_own_full_scale(self):
        assert level_from_rms(32767 * 0.03, full_scale=32768) == pytest.approx(level_from_rms(0.03), abs=1e-3)


class TestDefaultChannel:
    def test_module_helpers_share_one_channel(self, path, monkeypatch):
        monkeypatch.setattr(voice_levels, "_channel", LevelChannel(path))
        voice_levels.publish(Voice.MICROPHONE, 0.5)
        assert voice_levels.current() == pytest.approx(0.5)

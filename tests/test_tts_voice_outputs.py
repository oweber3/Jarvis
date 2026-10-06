"""Extra voice outputs (``extensions/extensions.spec.md``, Voice outputs) inside the TTS engines.

Piper's output stream is replaced by one the test plays block by block, and the voice output by an in-memory
fake that records what each reply's sink receives. Nothing here opens an audio device or a socket.
"""
import sys
import threading
import time
import types
from types import SimpleNamespace

import numpy as np
import pytest

sd = pytest.importorskip("sounddevice")

from jarvis.output.tts import ChatterboxTTS, PiperTTS, create_tts_engine

RATE = 22050
WAIT = 3.0


# --------------------------------------------------------------------------
# Fakes


class ManualStream:
    """An output device whose callback the test runs one block at a time."""

    instances: list = []

    def __init__(self, samplerate, channels, dtype, blocksize, callback, **_):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.active = False
        self.aborted = False
        self.blocks: list = []
        ManualStream.instances.append(self)

    def start(self):
        self.active = True

    def play_block(self) -> bool:
        """Run the callback once; False once the stream has stopped."""
        if not self.active:
            return False
        out = np.zeros((self.blocksize, 1), dtype=np.int16)
        try:
            self.callback(out, self.blocksize, None, None)
        except sd.CallbackStop:
            self.blocks.append(out[:, 0].copy())  # a stopping block is still played
            self.active = False
            return False
        except sd.CallbackAbort:
            self.active = False
            return False
        self.blocks.append(out[:, 0].copy())
        return True

    def play_all(self, limit=10_000):
        while limit and self.play_block():
            limit -= 1

    @property
    def played(self) -> np.ndarray:
        return np.concatenate(self.blocks) if self.blocks else np.zeros(0, dtype=np.int16)

    def abort(self):
        self.aborted = True
        self.active = False

    def stop(self):
        self.active = False

    def close(self):
        self.active = False


class FakeSink:
    def __init__(self, fail_writes=False):
        self.received = bytearray()
        self.ended = False
        self.aborted = False
        self.writes_after_close = 0
        self.writer_threads = set()
        self._fail_writes = fail_writes

    def write(self, pcm):
        assert isinstance(pcm, bytes)
        if self._fail_writes:
            raise OSError("sink failed")
        if self.ended or self.aborted:
            self.writes_after_close += 1
        self.writer_threads.add(threading.get_ident())
        self.received += pcm

    def end(self):
        self.ended = True

    def abort(self):
        self.aborted = True


SILENCE = 128


def to_output_pcm(samples):
    """The fake output's format: one unsigned byte per PC sample."""
    return ((np.asarray(samples, dtype=np.int32) >> 8) + SILENCE).astype(np.uint8).tobytes()


class FakeOutput:
    """A voice output in memory, recording each reply's sink. One byte per PC sample, so positions line up."""

    name = "test speaker"

    def __init__(self, reachable=True, fail_open=False, fail_writes=False, on=True, lead_ms=100):
        self.reachable = reachable
        self.fail_open = fail_open
        self.fail_writes = fail_writes
        self.on = on
        self.lead = lead_ms
        self.sinks: list = []
        self.warmed_on: list = []

    def enabled(self):
        return self.on

    def lead_ms(self):
        return self.lead

    def warm(self):
        self.warmed_on.append(threading.current_thread())

    def open(self):
        if self.fail_open:
            raise OSError("no answer")
        if not self.reachable:
            return None
        sink = FakeSink(fail_writes=self.fail_writes)
        self.sinks.append(sink)
        return sink

    def convert(self, samples, rate):
        assert rate == RATE
        return to_output_pcm(samples)

    def scale(self, pcm, gains):
        levels = np.frombuffer(pcm, dtype=np.uint8).astype(np.float32) - SILENCE
        return np.clip(np.round(levels * gains) + SILENCE, 0, 255).astype(np.uint8).tobytes()


class FakeVoice:
    """Yields the given chunks, one per sentence; ``hold_before`` holds that chunk back until released."""

    def __init__(self, chunks, hold_before=None):
        self.chunks = [np.asarray(c, dtype=np.int16) for c in chunks]
        self.config = SimpleNamespace(sample_rate=RATE)
        self.hold_before = hold_before
        self.release = threading.Event()

    def synthesize(self, _text, _config):
        for index, chunk in enumerate(self.chunks):
            if index == self.hold_before:
                assert self.release.wait(WAIT), "the held sentence was never released"
            yield SimpleNamespace(audio_int16_array=chunk)


def sentences(*specs):
    """Constant-level chunks, e.g. ``sentences((2000, 1000), (3000, 2000))`` for (length, level) pairs."""
    return [np.full(length, level, dtype=np.int16) for length, level in specs]


@pytest.fixture
def streams(monkeypatch):
    ManualStream.instances = []
    monkeypatch.setattr(sd, "OutputStream", ManualStream)
    return ManualStream.instances


@pytest.fixture
def engines():
    created = []

    def make(chunks, *, pc=True, outputs=(), voice=None):
        tts = PiperTTS(enabled=True, pc_speakers_enabled=pc, voice_outputs=list(outputs))
        voice = voice or FakeVoice(chunks)
        tts._voice = voice
        tts._sample_rate = voice.config.sample_rate
        tts._initialized = True
        tts.start()
        created.append(tts)
        return tts

    yield make
    for tts in created:
        tts.stop()


class Reply:
    """One spoken reply: its callbacks and the stream that plays it."""

    def __init__(self, tts, streams, text="Reply.", wait_for_synthesis=True):
        self.tts = tts
        self.durations: list = []
        self.synthesised = threading.Event()
        self.completed = threading.Event()
        count = len(streams)
        tts.speak(text, completion_callback=self.completed.set, duration_callback=self._on_duration)
        deadline = time.monotonic() + WAIT
        while len(streams) == count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(streams) > count, "playback never started"
        self.stream = streams[-1]
        if wait_for_synthesis:
            assert self.synthesised.wait(WAIT), "synthesis never finished"

    def _on_duration(self, seconds):
        self.durations.append(seconds)
        self.synthesised.set()

    def play_to_end(self):
        self.stream.play_all()
        assert self.completed.wait(WAIT), "reply never completed"


def lead_samples(sync_ms):
    return round(sync_ms * RATE / 1000)


def output_audio(chunks):
    return b"".join(to_output_pcm(chunk) for chunk in chunks)


def output_bytes_for(pc_samples):
    return pc_samples


def first_voiced(samples):
    return int(np.flatnonzero(samples)[0])


# --------------------------------------------------------------------------
# Both voices on


def test_both_voices_play_with_the_pc_delayed_by_the_lead(streams, engines):
    chunks = sentences((2000, 1000), (3000, 2000))
    output = FakeOutput(lead_ms=100)
    reply = Reply(engines(chunks, outputs=[output]), streams)
    reply.play_to_end()

    lead = lead_samples(100)
    played = reply.stream.played
    assert first_voiced(played) == lead
    assert played[lead:lead + 5000].tolist() == [1000] * 2000 + [2000] * 3000
    assert not played[lead + 5000:].any()

    (sink,) = output.sinks
    assert bytes(sink.received) == output_audio(chunks)
    assert sink.ended and not sink.aborted
    assert sink.writes_after_close == 0


def test_the_output_runs_ahead_of_the_pc_by_the_lead(streams, engines):
    chunks = sentences((6000, 1000), (6000, 2000))
    total = 12000
    output = FakeOutput(lead_ms=150)
    reply = Reply(engines(chunks, outputs=[output]), streams)
    (sink,) = output.sinks
    lead = lead_samples(150)

    elapsed = 0
    while reply.stream.play_block():
        elapsed = min(elapsed + reply.stream.blocksize, lead + total)
        pc_voiced = int(np.count_nonzero(reply.stream.played))
        # The output speaks from the first block, in step with the PC's clock...
        assert len(sink.received) == pytest.approx(output_bytes_for(min(total, elapsed)), abs=3)
        if elapsed <= lead:
            assert pc_voiced == 0, "the PC is still in its lead-in"
        else:
            # ...and stays the lead ahead of what the PC has voiced.
            assert len(sink.received) == pytest.approx(output_bytes_for(min(total, pc_voiced + lead)), abs=3)
    assert reply.completed.wait(WAIT)


def test_the_output_regains_its_lead_after_a_synthesis_underrun(streams, engines):
    chunks = sentences((3000, 1000), (8000, 2000))
    voice = FakeVoice(chunks, hold_before=1)
    output = FakeOutput(lead_ms=100)
    reply = Reply(engines(chunks, outputs=[output], voice=voice), streams, wait_for_synthesis=False)
    (sink,) = output.sinks
    lead = lead_samples(100)

    # Play past the first sentence on the PC too: both outputs are now waiting for the second.
    while np.count_nonzero(reply.stream.played) < 3000:
        assert reply.stream.play_block()
    assert reply.stream.play_block() and not reply.stream.blocks[-1].any()
    assert bytes(sink.received) == to_output_pcm(chunks[0])

    voice.release.set()
    assert reply.synthesised.wait(WAIT)
    assert reply.stream.play_block()
    pc_voiced = int(np.count_nonzero(reply.stream.played))
    assert len(sink.received) == pytest.approx(output_bytes_for(pc_voiced + lead), abs=3)
    reply.play_to_end()
    assert bytes(sink.received) == output_audio(chunks)


def test_echo_timing_includes_the_lead(streams, engines):
    reply = Reply(engines(sentences((4000, 500)), outputs=[FakeOutput(lead_ms=120)]), streams)
    reply.play_to_end()
    assert reply.durations == [pytest.approx((4000 + lead_samples(120)) / RATE)]


@pytest.mark.parametrize("asked, used", [(-50, 0), (900, 500)])
def test_the_lead_is_kept_between_zero_and_half_a_second(streams, engines, asked, used):
    reply = Reply(engines(sentences((3000, 500)), outputs=[FakeOutput(lead_ms=asked)]), streams)
    reply.play_to_end()
    assert first_voiced(reply.stream.played) == lead_samples(used)


def test_output_audio_is_released_from_the_audio_callback(streams, engines):
    output = FakeOutput()
    reply = Reply(engines(sentences((3000, 700)), outputs=[output]), streams)
    reply.play_to_end()
    (sink,) = output.sinks
    assert sink.writer_threads == {threading.get_ident()}  # the thread that ran the stream callback


def test_the_first_output_that_opens_takes_the_reply(streams, engines):
    off, unreachable, first, second = (FakeOutput(on=False), FakeOutput(reachable=False), FakeOutput(),
                                       FakeOutput())
    reply = Reply(engines(sentences((3000, 700)), outputs=[off, unreachable, first, second]), streams)
    reply.play_to_end()
    assert off.sinks == [] and unreachable.sinks == [] and second.sinks == []
    assert len(first.sinks) == 1 and first.sinks[0].ended


# --------------------------------------------------------------------------
# One voice on


def test_pc_voice_off_plays_silence_while_the_output_speaks(streams, engines):
    chunks = sentences((5000, 1000), (3000, 2000))
    output = FakeOutput(lead_ms=100)
    reply = Reply(engines(chunks, pc=False, outputs=[output]), streams)
    (sink,) = output.sinks

    taken = 0
    while reply.stream.play_block():
        taken = min(taken + reply.stream.blocksize, 8000)
        assert len(sink.received) == pytest.approx(output_bytes_for(taken), abs=3)
    assert reply.completed.wait(WAIT)

    assert not reply.stream.played.any()
    assert len(reply.stream.blocks) == -(-8000 // reply.stream.blocksize), "no lead-in without the PC voice"
    assert bytes(sink.received) == output_audio(chunks)
    assert sink.ended and not sink.aborted
    assert reply.durations == [pytest.approx(8000 / RATE)]


def test_a_switched_off_output_leaves_the_pc_only(streams, engines):
    output = FakeOutput(on=False)
    reply = Reply(engines(sentences((3000, 1000)), outputs=[output]), streams)
    reply.play_to_end()
    assert output.sinks == []
    assert first_voiced(reply.stream.played) == 0
    assert reply.durations == [pytest.approx(3000 / RATE)]


def test_without_outputs_the_pc_voice_is_unchanged(streams, engines):
    reply = Reply(engines(sentences((3000, 1000))), streams)
    reply.play_to_end()
    assert first_voiced(reply.stream.played) == 0
    assert reply.durations == [pytest.approx(3000 / RATE)]


# --------------------------------------------------------------------------
# Unavailable output


def test_an_unavailable_output_leaves_the_reply_on_the_pc_without_delay(streams, engines):
    output = FakeOutput(reachable=False)
    reply = Reply(engines(sentences((3000, 1000)), outputs=[output]), streams)
    reply.play_to_end()
    assert output.sinks == []
    played = reply.stream.played
    assert first_voiced(played) == 0
    assert (played != 0).sum() == 3000
    assert reply.durations == [pytest.approx(3000 / RATE)]


def test_a_failed_open_counts_as_unavailable(streams, engines):
    reply = Reply(engines(sentences((3000, 1000)), outputs=[FakeOutput(fail_open=True)]), streams)
    reply.play_to_end()
    assert first_voiced(reply.stream.played) == 0
    assert reply.durations == [pytest.approx(3000 / RATE)]


def test_unavailable_output_with_the_pc_voice_off_is_silent_with_unchanged_timing(streams, engines):
    chunks = sentences((5000, 1000), (2000, 2000))
    pc_only = Reply(engines(chunks), streams)
    pc_only.play_to_end()

    output = FakeOutput(reachable=False)
    silent = Reply(engines(chunks, pc=False, outputs=[output]), streams)
    silent.play_to_end()

    assert output.sinks == []
    assert not silent.stream.played.any()
    assert len(silent.stream.blocks) == len(pc_only.stream.blocks)
    assert silent.durations == pc_only.durations


def test_a_failing_output_never_disturbs_the_pc_voice(streams, engines):
    output = FakeOutput(fail_writes=True, lead_ms=0)
    reply = Reply(engines(sentences((3000, 1000), (2000, 2000)), outputs=[output]), streams)
    reply.play_to_end()
    played = reply.stream.played
    assert played[played != 0].tolist() == [1000] * 3000 + [2000] * 2000


def test_a_failing_conversion_stops_only_the_output(streams, engines):
    output = FakeOutput(lead_ms=0)

    def broken(samples, rate):
        raise ValueError("bad audio")

    output.convert = broken
    reply = Reply(engines(sentences((3000, 1000)), outputs=[output]), streams)
    reply.play_to_end()
    (sink,) = output.sinks
    assert sink.aborted and sink.received == bytearray()
    assert (reply.stream.played != 0).sum() == 3000


# --------------------------------------------------------------------------
# Interruption and ducking


def test_interrupting_aborts_the_output_with_the_stream(streams, engines):
    output = FakeOutput()
    tts = engines(sentences((20_000, 1000), (20_000, 2000)), outputs=[output])
    reply = Reply(tts, streams)
    for _ in range(3):
        assert reply.stream.play_block()
    (sink,) = output.sinks
    received = len(sink.received)

    tts.interrupt()

    assert sink.aborted and not sink.ended
    assert reply.stream.aborted
    assert not reply.stream.play_block()
    deadline = time.monotonic() + WAIT
    while tts.is_speaking() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not tts.is_speaking()
    assert len(sink.received) == received
    assert sink.writes_after_close == 0
    assert not sink.ended


def test_ducking_quietens_the_pc_and_the_output_together(streams, engines):
    level = 8000
    chunk = np.full(40 * 1024, level, dtype=np.int16)
    output = FakeOutput(lead_ms=0)
    tts = engines([chunk], outputs=[output])
    reply = Reply(tts, streams)
    (sink,) = output.sinks
    expected = np.frombuffer(to_output_pcm(chunk), dtype=np.uint8).astype(np.int16) - SILENCE

    def block_and_output():
        before = len(sink.received)
        assert reply.stream.play_block()
        heard = np.frombuffer(bytes(sink.received[before:]), dtype=np.uint8).astype(np.int16) - SILENCE
        return reply.stream.blocks[-1], heard, before

    for _ in range(2):
        block_and_output()
    tts.duck(0.25)
    block_and_output()  # the fade
    pc, heard, start = block_and_output()
    assert pc.tolist() == [round(level * 0.25)] * len(pc)
    assert heard.tolist() == pytest.approx((expected[start:start + len(heard)] * 0.25).tolist(), abs=1)

    tts.unduck()
    block_and_output()
    pc, heard, start = block_and_output()
    assert pc.tolist() == [level] * len(pc)
    assert heard.tolist() == expected[start:start + len(heard)].tolist()
    reply.play_to_end()


# --------------------------------------------------------------------------
# Switch changes and other engines


def test_switch_changes_apply_from_the_next_reply(streams, engines):
    output = FakeOutput()
    tts = engines(sentences((3000, 1000)), outputs=[output])
    Reply(tts, streams).play_to_end()

    tts.pc_speakers_enabled = False
    second = Reply(tts, streams)
    second.play_to_end()
    assert not second.stream.played.any()
    assert len(output.sinks) == 2 and len(output.sinks[1].received) > 0

    output.on = False
    tts.pc_speakers_enabled = True
    third = Reply(tts, streams)
    third.play_to_end()
    assert len(output.sinks) == 2
    assert first_voiced(third.stream.played) == 0


def test_chatterbox_with_an_enabled_output_says_once_that_it_needs_piper(capsys):
    engine = create_tts_engine(engine="chatterbox", enabled=True, voice_outputs=[FakeOutput()])
    assert isinstance(engine, ChatterboxTTS)
    notices = [line for line in capsys.readouterr().out.splitlines() if line and not line[0].isspace()]
    assert len(notices) == 1 and "Piper" in notices[0] and FakeOutput.name in notices[0]


@pytest.mark.parametrize("kwargs", [
    {"engine": "chatterbox", "voice_outputs": [FakeOutput(on=False)]},
    {"engine": "chatterbox", "voice_outputs": []},
    {"engine": "chatterbox", "voice_outputs": [FakeOutput()], "enabled": False},
    {"engine": "piper", "voice_outputs": [FakeOutput()]},
])
def test_no_piper_notice_when_no_output_is_wanted_or_needed(capsys, kwargs):
    create_tts_engine(**{"enabled": True, **kwargs})
    assert "Piper" not in capsys.readouterr().out


def test_piper_factory_passes_the_voice_switches_and_outputs(streams):
    output = FakeOutput(lead_ms=80)
    tts = create_tts_engine(engine="piper", enabled=True, pc_speakers_enabled=False, voice_outputs=[output])
    voice = FakeVoice(sentences((3000, 1000)))
    tts._voice = voice
    tts._sample_rate = RATE
    tts._initialized = True
    tts.start()
    try:
        reply = Reply(tts, streams)
        reply.play_to_end()
    finally:
        tts.stop()
    assert not reply.stream.played.any()
    assert len(output.sinks) == 1 and output.sinks[0].ended


class _FakeMusic:
    def __init__(self):
        self.volume = 1.0
        self.played_at_volume = None
        self._busy = 0

    def load(self, _path):
        pass

    def set_volume(self, value):
        self.volume = value

    def play(self):
        self.played_at_volume = self.volume
        self._busy = 2

    def get_busy(self):
        self._busy -= 1
        return self._busy > 0

    def stop(self):
        self._busy = 0


@pytest.fixture
def fake_pygame(monkeypatch):
    music = _FakeMusic()
    mixer = types.SimpleNamespace(init=lambda **_: None, quit=lambda: None, music=music)
    pygame = types.ModuleType("pygame")
    pygame.mixer = mixer
    pygame.time = types.SimpleNamespace(wait=lambda _ms: None)
    torchaudio = types.ModuleType("torchaudio")
    torchaudio.save = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "pygame", pygame)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    return music


def _chatterbox(pc):
    tts = ChatterboxTTS(enabled=True, pc_speakers_enabled=pc)
    tts._model = SimpleNamespace(sr=24000, generate=lambda *_a, **_k: np.zeros((1, 24000), dtype=np.float32))
    tts._initialized = True
    return tts


@pytest.mark.parametrize("pc, volume", [(True, 1.0), (False, 0.0)])
def test_chatterbox_honours_the_pc_voice_switch_with_unchanged_timing(fake_pygame, pc, volume):
    tts = _chatterbox(pc)
    durations, completed = [], []
    tts._duration_callback = durations.append
    tts._completion_callback = lambda: completed.append(True)
    tts._speak_once("Hello there.")
    assert fake_pygame.played_at_volume == volume
    assert durations == [pytest.approx(1.0)]
    assert completed == [True]


def test_outputs_are_warmed_on_the_starting_thread(engines):
    """Converters may load DLLs; off-thread that can deadlock with the listener's microphone probe."""
    output = FakeOutput()
    engines([], outputs=[output])
    assert output.warmed_on == [threading.current_thread()]


def test_an_output_without_warm_up_still_starts(engines, streams):
    output = FakeOutput()
    output.warm = None
    reply = Reply(engines(sentences((2000, 500)), outputs=[output]), streams)
    reply.play_to_end()
    assert len(output.sinks) == 1

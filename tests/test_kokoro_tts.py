"""The optional Kokoro voice engine: sentence streaming, model files fetched only when chosen, and Piper
standing in whenever Kokoro cannot speak.

The ONNX session is replaced by a fake that returns audio sized by its input, so these tests need no model
files; the last test synthesises with the real model when it has been downloaded.
"""
import os
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sd = pytest.importorskip("sounddevice")

from jarvis.output import kokoro
from jarvis.output.tts import KokoroTTS, PiperTTS, create_tts_engine

WAIT = 5.0


class FakeSession:
    """Returns 100 samples of audio per token and records every run."""

    def __init__(self):
        self.runs = []

    def run(self, _outputs, inputs):
        self.runs.append(inputs)
        tokens = inputs["tokens"].shape[1]
        return [np.full((1, tokens * 100), 0.5, dtype=np.float32)]


def _voice_table():
    rows = np.arange(kokoro.MAX_TOKENS + 2, dtype=np.float32).reshape(-1, 1, 1) * np.ones((1, 1, 256), np.float32)
    return {name: rows + index * 1000 for index, name in enumerate(kokoro.KOKORO_VOICES)}


def _fake_voice(voice=kokoro.KOKORO_DEFAULT_VOICE, speed=1.0):
    session = FakeSession()
    vocab = {chr(c): c for c in range(32, 0x2FF)}
    vocab.update({ch: 0x300 + i for i, ch in enumerate("ˈˌːɪʊəɛɔæɑɒʌɜɐɹɾʃʒθðŋɡʔᵻɚ")})
    return kokoro.KokoroVoice(session, _voice_table(), vocab, voice=voice, speed=speed), session


class AutoStream:
    """An output device that plays its callback to the end on a thread."""

    instances: list = []

    def __init__(self, samplerate, channels, dtype, blocksize, callback, **_):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.active = False
        self.played = []
        AutoStream.instances.append(self)

    def start(self):
        self.active = True
        threading.Thread(target=self._play, daemon=True).start()

    def _play(self):
        while self.active:
            out = np.zeros((self.blocksize, 1), dtype=np.int16)
            try:
                self.callback(out, self.blocksize, None, None)
            except (sd.CallbackStop, sd.CallbackAbort):
                self.played.append(out[:, 0].copy())
                break
            self.played.append(out[:, 0].copy())
        self.active = False

    def abort(self):
        self.active = False

    def close(self):
        self.active = False


@pytest.fixture
def streams(monkeypatch):
    AutoStream.instances = []
    monkeypatch.setattr(sd, "OutputStream", AutoStream)
    return AutoStream.instances


class FakePiperVoice:
    config = SimpleNamespace(sample_rate=22050)

    def synthesize(self, _text, _config):
        yield SimpleNamespace(audio_int16_array=np.full(2205, 700, dtype=np.int16))


@pytest.fixture
def piper_loads(monkeypatch):
    """Piper's loader gives a fake voice, so a fallback needs no model file."""
    loaded = []

    def load(self):
        loaded.append(self)
        self._voice = FakePiperVoice()
        self._sample_rate = 22050

    monkeypatch.setattr(PiperTTS, "_load_voice", load)
    return loaded


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing may be downloaded here")

    monkeypatch.setattr("requests.get", refuse)


# --------------------------------------------------------------------------
# Voices and files


@pytest.mark.unit
def test_the_offered_voices_are_english_and_include_the_default():
    assert kokoro.KOKORO_DEFAULT_VOICE in kokoro.KOKORO_VOICES
    assert all(name[0] in "ab" for name in kokoro.KOKORO_VOICES)
    assert all(label for label in kokoro.KOKORO_VOICES.values())


@pytest.mark.unit
def test_every_model_file_is_pinned_with_a_sha256(tmp_path):
    for f in kokoro.KOKORO_FILES:
        assert f.url.startswith("https://") and len(f.sha256) == 64 and f.size > 0
    assert not kokoro.kokoro_files_present(tmp_path)
    for f in kokoro.KOKORO_FILES:
        (tmp_path / f.name).write_bytes(b"x")
    assert not kokoro.kokoro_files_present(tmp_path), "a file of the wrong size is not a downloaded model"


@pytest.mark.unit
def test_a_failed_download_leaves_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(kokoro, "download_verified", lambda *a, **k: False)
    assert not kokoro.download_kokoro(tmp_path)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Synthesis


@pytest.mark.unit
def test_each_sentence_is_one_chunk_spoken_with_the_chosen_voice_and_speed():
    voice, session = _fake_voice(voice="af_heart", speed=1.3)
    chunks = [c.audio_int16_array for c in voice.synthesize("Good evening. The kettle is on! Shall I wait?", None)]

    assert len(chunks) == 3
    assert all(c.dtype == np.int16 and np.abs(c).max() > 0 for c in chunks)
    table = _voice_table()["af_heart"]
    for run in session.runs:
        tokens = run["tokens"]
        assert tokens[0, 0] == 0 and tokens[0, -1] == 0, "every window is padded at both ends"
        assert np.array_equal(run["style"], table[tokens.shape[1] - 2])
        assert run["speed"][0] == pytest.approx(1.3)


@pytest.mark.unit
def test_the_first_sentence_starts_with_its_first_clause_so_speech_starts_sooner():
    voice, session = _fake_voice()
    text = "I have checked your calendar, and you have three meetings tomorrow. The first is at nine, in London."
    chunks = list(voice.synthesize(text, None))
    assert len(chunks) == 3, "first sentence by clause, later sentences whole"
    first_window = session.runs[0]["tokens"].shape[1]
    whole_first_sentence = sum(run["tokens"].shape[1] - 2 for run in session.runs[:2]) + 2
    assert first_window < whole_first_sentence


@pytest.mark.unit
def test_a_sentence_too_long_for_the_model_is_split_without_dropping_it():
    voice, session = _fake_voice()
    long_sentence = "The quick brown fox jumps over the lazy dog, " * 25 + "and then it rests."
    chunks = list(voice.synthesize(long_sentence, None))
    assert len(chunks) > 1
    assert all(run["tokens"].shape[1] <= kokoro.MAX_TOKENS + 2 for run in session.runs)
    spoken = sum(run["tokens"].shape[1] - 2 for run in session.runs)
    assert spoken > kokoro.MAX_TOKENS


@pytest.mark.unit
def test_the_models_padding_silence_is_trimmed_to_a_short_pause():
    class PaddedSession(FakeSession):
        def run(self, _outputs, inputs):
            self.runs.append(inputs)
            rate = kokoro.SAMPLE_RATE
            speech = 0.5 * np.sin(np.linspace(0, 400 * np.pi, rate))  # one second of speech
            return [np.concatenate([np.zeros(rate // 4), speech, np.zeros(rate // 2)]).astype(np.float32)[None]]

    voice, _ = _fake_voice()
    voice._session = PaddedSession()
    (chunk,) = list(voice.synthesize("Hello there.", None))
    samples = np.abs(chunk.audio_int16_array.astype(np.int32))
    loud = np.nonzero(samples > 300)[0]
    lead = loud[0] / kokoro.SAMPLE_RATE
    tail = (len(samples) - loud[-1]) / kokoro.SAMPLE_RATE
    assert lead <= 0.06, "speech starts at once"
    assert 0.1 <= tail <= 0.35, "a short pause stays between sentences"
    assert len(loud) / kokoro.SAMPLE_RATE > 0.9, "no speech is cut"


@pytest.mark.unit
def test_text_without_speakable_phonemes_yields_nothing():
    voice, session = _fake_voice()
    assert list(voice.synthesize("   ", None)) == []
    assert session.runs == []


# --------------------------------------------------------------------------
# The engine: choosing, downloading and falling back


@pytest.mark.unit
def test_the_factory_builds_kokoro_only_when_chosen(no_network):
    assert type(create_tts_engine(engine="piper", enabled=True)) is PiperTTS
    engine = create_tts_engine(engine="kokoro", enabled=True, kokoro_voice="bf_emma", kokoro_speed=1.2)
    assert isinstance(engine, KokoroTTS)
    assert (engine.kokoro_voice, engine.kokoro_speed) == ("bf_emma", 1.2)


@pytest.mark.unit
def test_piper_users_never_fetch_kokoro_files(tmp_path, monkeypatch, piper_loads):
    fetched = []
    monkeypatch.setattr(kokoro, "download_kokoro", lambda *a, **k: fetched.append(a) or False)
    tts = create_tts_engine(engine="piper", enabled=True)
    tts.start()
    tts.stop()
    assert fetched == []


@pytest.mark.unit
def test_missing_kokoro_files_speak_with_piper_and_say_why(tmp_path, piper_loads, capsys, no_network):
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    assert tts._ensure_initialized()
    assert tts.engine_in_use == "piper"
    assert tts.fallback_reason and "download" in tts.fallback_reason.lower()
    out = capsys.readouterr().out
    assert "Kokoro" in out and "Piper" in out


@pytest.mark.unit
def test_a_kokoro_load_error_speaks_with_piper(tmp_path, monkeypatch, piper_loads, capsys):
    def broken(*args, **kwargs):
        raise kokoro.KokoroUnavailable("onnxruntime could not load the model")

    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: True)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", broken)
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    assert tts._ensure_initialized()
    assert tts.engine_in_use == "piper"
    assert "could not load" in tts.fallback_reason
    assert "Piper" in capsys.readouterr().out


@pytest.mark.unit
def test_kokoro_speaks_when_its_files_are_present(tmp_path, monkeypatch, piper_loads, streams):
    voice, _session = _fake_voice()
    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: True)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", lambda *a, **k: voice)
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    done = threading.Event()
    tts.start()
    try:
        tts.speak("Hello there. All is well.", completion_callback=done.set)
        assert done.wait(WAIT)
    finally:
        tts.stop()
    assert tts.engine_in_use == "kokoro" and piper_loads == []
    assert streams[-1].samplerate == kokoro.SAMPLE_RATE
    assert np.abs(np.concatenate(streams[-1].played)).max() > 0


@pytest.mark.unit
def test_files_downloaded_after_start_take_over_from_the_next_reply(tmp_path, monkeypatch, piper_loads, streams):
    voice, _session = _fake_voice()
    present = {"now": False}
    finish_download = threading.Event()

    def download(model_dir, progress=None):
        assert finish_download.wait(WAIT)
        present["now"] = True
        return True

    def load(*args, **kwargs):
        if not present["now"]:
            raise kokoro.KokoroUnavailable("the Kokoro voice files are not downloaded yet")
        return voice

    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: present["now"])
    monkeypatch.setattr(kokoro, "download_kokoro", download)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", load)
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    tts.start()
    try:
        first = threading.Event()
        tts.speak("While downloading.", completion_callback=first.set)
        assert first.wait(WAIT)
        assert streams[-1].samplerate == 22050, "Piper speaks until Kokoro is ready"

        finish_download.set()
        deadline = time.monotonic() + WAIT
        while not tts.kokoro_downloaded.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        second = threading.Event()
        tts.speak("Downloaded now.", completion_callback=second.set)
        assert second.wait(WAIT)
    finally:
        tts.stop()
    assert streams[-1].samplerate == kokoro.SAMPLE_RATE
    assert tts.engine_in_use == "kokoro" and tts.fallback_reason is None


@pytest.mark.unit
def test_voice_outputs_hear_kokoro_too(tmp_path, monkeypatch, piper_loads, streams, capsys):
    voice, _session = _fake_voice()
    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: True)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", lambda *a, **k: voice)

    class Output:
        name = "test speaker"

        def __init__(self):
            self.rates, self.received, self.ended = [], bytearray(), False

        def enabled(self):
            return True

        def lead_ms(self):
            return 0

        def open(self):
            output = self

            class Sink:
                def write(self, pcm):
                    output.received += pcm

                def end(self):
                    output.ended = True

                def abort(self):
                    pass

            return Sink()

        def convert(self, samples, rate):
            self.rates.append(rate)
            return np.asarray(samples, dtype=np.int16).tobytes()

        def scale(self, pcm, gains):
            return pcm

    output = Output()
    tts = create_tts_engine(engine="kokoro", enabled=True, voice_outputs=[output])
    tts.kokoro_model_dir = tmp_path
    done = threading.Event()
    tts.start()
    try:
        tts.speak("Through the speaker.", completion_callback=done.set)
        assert done.wait(WAIT)
    finally:
        tts.stop()
    assert output.rates and set(output.rates) == {kokoro.SAMPLE_RATE}
    assert len(output.received) > 0 and output.ended
    assert "needs the Piper or Kokoro" not in capsys.readouterr().out


@pytest.mark.unit
def test_a_disabled_kokoro_engine_loads_and_fetches_nothing(monkeypatch, no_network):
    monkeypatch.setattr(kokoro, "load_kokoro_voice", lambda *a, **k: pytest.fail("must not load"))
    tts = KokoroTTS(enabled=False)
    tts.start()
    tts.speak("Silent.")
    assert not tts._ensure_initialized()


# --------------------------------------------------------------------------
# The real model, when this PC has downloaded it


@pytest.mark.integration
def test_the_real_model_synthesises_speech(tmp_path):
    models = os.environ.get("JARVIS_TEST_KOKORO_DIR") or None  # a downloaded copy elsewhere, for CI machines
    if not kokoro.kokoro_files_present(models):
        pytest.skip("Kokoro model files are not downloaded on this PC")
    voice = kokoro.load_kokoro_voice(models, voice="bm_george", speed=1.0)
    text = "Good evening. Your meeting starts in ten minutes."
    audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text, None)])
    seconds = len(audio) / kokoro.SAMPLE_RATE
    assert 1.5 < seconds < 8.0
    assert np.sqrt(np.mean(audio.astype(np.float32) ** 2)) > 300, "the audio must not be silent"
    path = Path(tmp_path) / "kokoro.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(kokoro.SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    assert path.stat().st_size > 44


# --------------------------------------------------------------------------
# Robustness


@pytest.mark.unit
def test_kokoro_downloads_never_overlap(tmp_path, monkeypatch):
    active, overlaps = [0], []

    def fetch(*args, **kwargs):
        active[0] += 1
        overlaps.append(active[0])
        time.sleep(0.05)
        active[0] -= 1
        return True

    monkeypatch.setattr(kokoro, "download_verified", fetch)
    threads = [threading.Thread(target=kokoro.download_kokoro, args=(tmp_path,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(overlaps) == 1


@pytest.mark.unit
def test_restarting_the_engine_does_not_start_a_second_download(tmp_path, monkeypatch, piper_loads):
    started, release = [], threading.Event()

    def download(*args, **kwargs):
        started.append(1)
        release.wait(WAIT)
        return False

    monkeypatch.setattr(kokoro, "download_kokoro", download)
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    try:
        tts.start()
        tts.stop()
        tts.start()
        time.sleep(0.1)
    finally:
        release.set()
        tts.stop()
    assert len(started) == 1


@pytest.mark.unit
def test_an_interrupt_while_kokoro_loads_drops_that_reply(tmp_path, monkeypatch, piper_loads, streams):
    voice, _session = _fake_voice()
    loading, finish_loading = threading.Event(), threading.Event()
    present = {"now": False}

    def load(*args, **kwargs):
        if not present["now"]:
            raise kokoro.KokoroUnavailable("the Kokoro voice files are not downloaded yet")
        loading.set()
        assert finish_loading.wait(WAIT)
        return voice

    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: present["now"])
    monkeypatch.setattr(kokoro, "download_kokoro", lambda *a, **k: False)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", load)
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    tts.start()
    try:
        present["now"] = True
        tts.kokoro_downloaded.set()
        completed = threading.Event()
        tts.speak("Interrupt me.", completion_callback=completed.set)
        assert loading.wait(WAIT)
        tts.interrupt()
        finish_loading.set()
        time.sleep(0.3)
    finally:
        tts.stop()
    assert streams == [] and not completed.is_set()
    assert tts.engine_in_use == "kokoro", "the voice is still ready for the next reply"


@pytest.mark.unit
def test_kokoro_failing_mid_session_hands_the_reply_to_piper(tmp_path, monkeypatch, piper_loads, streams, capsys):
    class BrokenSession(FakeSession):
        def run(self, _outputs, inputs):
            raise RuntimeError("CUDA out of memory")

    voice, _ = _fake_voice()
    voice._session = BrokenSession()
    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: True)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", lambda *a, **k: voice)
    tts = KokoroTTS(enabled=True, kokoro_model_dir=tmp_path)
    tts.start()
    try:
        first, second = threading.Event(), threading.Event()
        tts.speak("Say this anyway.", completion_callback=first.set)
        assert first.wait(WAIT), "the reply is still spoken"
        tts.speak("And this.", completion_callback=second.set)
        assert second.wait(WAIT)
    finally:
        tts.stop()
    assert [s.samplerate for s in streams] == [22050, 22050]
    assert tts.engine_in_use == "piper" and "out of memory" in tts.fallback_reason
    assert "Piper" in capsys.readouterr().out


@pytest.mark.unit
def test_a_damaged_model_file_is_removed_so_it_downloads_again(tmp_path, monkeypatch):
    for f in kokoro.KOKORO_FILES:
        (tmp_path / f.name).write_bytes(b"\0" * 10)
    monkeypatch.setattr(kokoro, "KOKORO_FILES", tuple(
        kokoro.KokoroFile(f.name, f.url, 10, f.sha256) for f in kokoro.KOKORO_FILES))
    with pytest.raises(kokoro.KokoroUnavailable, match="download"):
        kokoro.load_kokoro_voice(tmp_path)
    assert not kokoro.kokoro_files_present(tmp_path)


# --------------------------------------------------------------------------
# Where Kokoro runs


class _FakeOrt:
    """Stands in for onnxruntime, recording the providers each session asked for."""

    def __init__(self, available, cuda_fails=False):
        self.available, self.cuda_fails, self.asked = available, cuda_fails, []

    def get_available_providers(self):
        return list(self.available)

    class SessionOptions:
        log_severity_level = 0

    def InferenceSession(self, path, options=None, providers=()):
        self.asked.append(list(providers))
        if self.cuda_fails and providers[0] == "CUDAExecutionProvider":
            raise RuntimeError("no CUDA")
        return SimpleNamespace(get_providers=lambda: list(providers))


@pytest.mark.unit
@pytest.mark.parametrize("device, available, cuda_fails, expected", [
    ("gpu", ["CUDAExecutionProvider", "CPUExecutionProvider"], False, "graphics card"),
    ("gpu", ["CPUExecutionProvider"], False, "processor"),
    ("gpu", ["CUDAExecutionProvider", "CPUExecutionProvider"], True, "processor"),
    ("cpu", ["CUDAExecutionProvider", "CPUExecutionProvider"], False, "processor"),
])
def test_kokoro_runs_where_chosen_and_falls_back_to_the_processor(monkeypatch, tmp_path, device, available,
                                                                  cuda_fails, expected):
    import sys
    fake = _FakeOrt(available, cuda_fails)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _session, where = kokoro._create_session(tmp_path / "model.onnx", device)
    assert where == expected
    if device == "cpu":
        assert all(p == ["CPUExecutionProvider"] for p in fake.asked)


@pytest.mark.unit
def test_the_factory_passes_the_chosen_device(no_network):
    engine = create_tts_engine(engine="kokoro", enabled=True, kokoro_device="cpu")
    assert engine.kokoro_device == "cpu"

"""Drive the real ``VoiceListener`` frame loop from arrays instead of a microphone.

The harness pushes audio through ``_audio_frames`` -> VAD -> utterance
finalisation -> the transcription job queue -> a transcriber -> the
listener's normal transcript handling. Time is simulated: the listener's
clock advances with the audio, so a run is faster than real time and
reproducible. Nothing opens an audio device, settings come from defaults
in a throwaway config, and face states are recorded instead of written to
the state file a running Jarvis's orb reads.

Transcribers receive ``(audio, start_time, end_time)`` and return text.
The default is an oracle that returns the text of every clip the harness
played inside that span, which isolates segmentation from speech
recognition. ``WhisperTranscriber`` runs real faster-whisper.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .audio import SAMPLE_RATE, mix, resample

Transcriber = Callable[[np.ndarray, float, float], str]


class SimClock:
    """A stand-in for the ``time`` module whose clock only moves when told to."""

    def __init__(self, start: float) -> None:
        import time as _time

        self._time = _time
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self._time.perf_counter()

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._time, name)


@dataclass
class WhisperCall:
    """One utterance the listener handed to Whisper."""

    audio: np.ndarray
    start_time: float
    end_time: float
    text: str = ""

    @property
    def duration(self) -> float:
        return len(self.audio) / float(SAMPLE_RATE)


@dataclass
class Played:
    """A clip the harness fed to the listener, on the simulated clock."""

    start: float
    end: float
    text: str


class OracleTranscriber:
    """Return the text of every labelled clip overlapping the utterance span."""

    def __init__(self, harness: "ListenerHarness", min_overlap: float = 0.15) -> None:
        self.harness = harness
        self.min_overlap = min_overlap

    def __call__(self, audio: np.ndarray, start: float, end: float) -> str:
        texts = []
        for played in self.harness.played:
            overlap = min(end, played.end) - max(start, played.start)
            if played.text and overlap >= min(self.min_overlap, played.end - played.start):
                texts.append(played.text)
        return " ".join(texts)


class WhisperTranscriber:
    """Real faster-whisper transcription (CPU by default, so the GPU stays free)."""

    def __init__(self, model_name: str = "small", device: str = "cpu", compute_type: str = "int8") -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def __call__(self, audio: np.ndarray, start: float, end: float) -> str:
        segments, _ = self.model.transcribe(audio, language="en", vad_filter=False)
        return " ".join(seg.text.strip() for seg in segments).strip()


class FakeTTS:
    """A stand-in for the TTS engine that plays nothing and records what the listener asks of it.

    ``events`` holds ``(simulated_time, name)`` for ``duck``, ``unduck`` and
    ``interrupt``, so tests can measure how quickly the listener reacts.
    """

    enabled = True

    def __init__(self, clock: "SimClock") -> None:
        self._clock = clock
        self.speaking = False
        self.ducked = False
        self.events: list[tuple[float, str]] = []
        self.spoken: list[str] = []

    def is_speaking(self) -> bool:
        return self.speaking

    def speak(self, text: str, **_kwargs: Any) -> None:
        self.spoken.append(text)

    def interrupt(self) -> None:
        self.events.append((self._clock.now, "interrupt"))
        self.speaking = False
        self.ducked = False

    def duck(self, gain: float = 0.15) -> None:
        self.events.append((self._clock.now, "duck"))
        self.ducked = True

    def unduck(self) -> None:
        self.events.append((self._clock.now, "unduck"))
        self.ducked = False

    def time_of(self, name: str) -> Optional[float]:
        return next((when for when, event in self.events if event == name), None)


class _FaceRecorder:
    """Stands in for the orb state singleton and records state names."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def set_state(self, state: Any) -> None:
        self._sink.append(getattr(state, "value", str(state)))


class ListenerHarness:
    """A ``VoiceListener`` fed from arrays.

    Use as a context manager: the listener's clock is simulated, and the
    user's config is out of reach, only inside the ``with`` block.
    ``cfg_overrides`` replace fields of the default settings.
    """

    def __init__(self, cfg_overrides: Optional[dict] = None, *,
                 transcriber: Optional[Transcriber] = None,
                 capture_rate: int = SAMPLE_RATE,
                 block_ms: int = 20,
                 start_time: float = 1_700_000_000.0,
                 intent_judge: Any = None,
                 speaker_verifier: Any = None,
                 with_tts: bool = False) -> None:
        self.cfg_overrides = dict(cfg_overrides or {})
        self.capture_rate = int(capture_rate)
        self.block_samples = max(1, self.capture_rate * int(block_ms) // 1000)
        self.clock = SimClock(start_time)
        self.transcriber: Transcriber = transcriber or OracleTranscriber(self)
        self._intent_judge = intent_judge
        self._speaker_verifier = speaker_verifier
        self.tts: Optional[FakeTTS] = FakeTTS(self.clock) if with_tts else None
        self.played: list[Played] = []
        self.whisper_calls: list[WhisperCall] = []
        self.collections: list[str] = []
        self.dispatched: list[str] = []
        self.face_states: list[str] = []
        self.audio_seconds = 0.0
        self.listener = None
        self._job = None
        self._patched: list[tuple[Any, str, Any]] = []

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "ListenerHarness":
        import jarvis.listening.echo_detection as echo_mod
        import jarvis.listening.latency as latency_mod
        import jarvis.listening.listener as listener_mod
        import jarvis.listening.state_manager as state_mod
        from jarvis.config import load_settings

        for module in (listener_mod, state_mod, latency_mod, echo_mod):
            self._patch(module, "time", self.clock)
        try:
            import desktop_app.face_widget as face_widget
        except Exception:
            face_widget = None
        if face_widget is not None:
            self._patch(face_widget, "get_jarvis_state", lambda: _FaceRecorder(self.face_states))

        self._config_dir = tempfile.TemporaryDirectory(prefix="jarvis-harness-")
        config_path = Path(self._config_dir.name) / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self._saved_config_env = os.environ.get("JARVIS_CONFIG_PATH")
        os.environ["JARVIS_CONFIG_PATH"] = str(config_path)
        cfg = load_settings()
        overrides = {"voice_debug": False, "hot_window_enabled": True, **self.cfg_overrides}
        cfg = dataclasses.replace(cfg, **overrides)

        listener = listener_mod.VoiceListener(db=None, cfg=cfg, tts=self.tts, dialogue_memory=None)
        listener._intent_judge = self._intent_judge
        listener._speaker_verifier = self._speaker_verifier
        listener._stream_samplerate = self.capture_rate
        listener._configure_audio(int(getattr(cfg, "vad_frame_ms", 20)))

        listener._transcribe_audio = self._transcribe  # type: ignore[method-assign]
        original_job = listener._transcribe_job

        def _transcribe_job(job) -> None:
            self._job = job
            original_job(job)

        listener._transcribe_job = _transcribe_job  # type: ignore[method-assign]
        original_begin = listener._begin_collection

        def _begin_collection(query: str, text_lower: str) -> None:
            self.collections.append(query)
            original_begin(query, text_lower)

        listener._begin_collection = _begin_collection  # type: ignore[method-assign]
        listener._dispatch_query = self.dispatched.append  # type: ignore[method-assign]
        self.listener = listener
        return self

    def __exit__(self, *exc) -> None:
        if self.listener is not None:
            self.listener.state_manager.stop()
        for module, name, value in reversed(self._patched):
            setattr(module, name, value)
        self._patched.clear()
        if self._saved_config_env is None:
            os.environ.pop("JARVIS_CONFIG_PATH", None)
        else:
            os.environ["JARVIS_CONFIG_PATH"] = self._saved_config_env
        self._config_dir.cleanup()

    def _patch(self, module: Any, name: str, value: Any) -> None:
        self._patched.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    # -- driving ---------------------------------------------------------
    @property
    def now(self) -> float:
        return self.clock.now

    def play(self, audio: np.ndarray, *, text: str = "", bed: Optional[np.ndarray] = None,
             snr_db: Optional[float] = None) -> None:
        """Feed 16 kHz ``audio`` (optionally over ``bed``) in real-time-sized blocks.

        ``text`` labels the clip for the oracle transcriber.
        """
        audio = np.asarray(audio, dtype=np.float32)
        if bed is not None:
            audio = mix(audio, bed, snr_db=snr_db)
        start = self.clock.now
        self.played.append(Played(start, start + len(audio) / SAMPLE_RATE, text))
        self.audio_seconds += len(audio) / SAMPLE_RATE
        captured = resample(audio, SAMPLE_RATE, self.capture_rate)
        for offset in range(0, len(captured), self.block_samples):
            block = captured[offset:offset + self.block_samples]
            self.clock.advance(len(block) / self.capture_rate)
            self.listener._process_audio_block(block.reshape(-1, 1))
            self._drain()

    def _drain(self) -> None:
        """Run queued Whisper jobs and hand their results back, in order."""
        listener = self.listener
        while True:
            try:
                job = listener._transcription_jobs_q.get_nowait()
            except Exception:
                break
            try:
                listener._transcribe_job(job)
            finally:
                listener._transcription_jobs_q.task_done()
        while True:
            try:
                result = listener._transcription_results_q.get_nowait()
            except Exception:
                break
            listener._handle_transcription_result(result)

    def _transcribe(self, audio: np.ndarray):
        job = self._job
        text = self.transcriber(audio, job.start_time, job.end_time)
        self.whisper_calls.append(WhisperCall(audio=audio, start_time=job.start_time,
                                              end_time=job.end_time, text=text))
        return text, "en", ()

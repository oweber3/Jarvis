"""Acoustic wake word: hears the sound of the wake phrase while it is being said.

openWakeWord's models run on ONNX Runtime (CPU): a melspectrogram model and
Google's speech embedding model turn every 80 ms of 16 kHz audio into one
feature vector, and a small phrase model scores the last 16 of them (about
1.3 s). Everything runs locally; audio never leaves the process. The models
are fetched once from openWakeWord's releases and checked against pinned
SHA-256 hashes before they are saved or used.

Detection only gives early feedback. The transcript-based wake detection in
the listener still decides whether Jarvis was addressed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..debug import debug_log
from ..utils.onnx_privacy import disable_onnxruntime_telemetry
from ..utils.model_files import ModelUnavailable, ensure_verified_file

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280  # 80 ms: one feature vector per chunk
_MEL_CONTEXT = 160 * 3  # extra samples the melspectrogram needs before each chunk
_MEL_WINDOW = 76  # melspectrogram frames per speech embedding
_MEL_BINS = 32
_FEATURE_HISTORY = 120  # about 10 s of feature vectors
_WARMUP_SCORES = 5  # the first scores after a reset see seeded features, not audio
DEFAULT_THRESHOLD = 0.5
DEFAULT_COOLDOWN_SEC = 1.5  # one phrase scores high for several chunks; it is one detection

_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str


MELSPEC_MODEL = ModelFile("openwakeword_melspectrogram.onnx", _RELEASE + "melspectrogram.onnx",
                          "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f")
EMBEDDING_MODEL = ModelFile("openwakeword_embedding_model.onnx", _RELEASE + "embedding_model.onnx",
                            "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f")
# Phrase models fetched by name; any other value of ``wake_sound_model`` is a path to an openWakeWord .onnx file.
PHRASE_MODELS = {
    "hey_jarvis": ModelFile("openwakeword_hey_jarvis_v0.1.onnx", _RELEASE + "hey_jarvis_v0.1.onnx",
                            "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb"),
}
# The wake word each built-in model hears; hey_jarvis also scores "Jarvis" on its own.
PHRASE_WAKE_WORDS = {"hey_jarvis": "jarvis"}
DEFAULT_PHRASE_MODEL = "hey_jarvis"


def _session(path: Path):
    disable_onnxruntime_telemetry()
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


class WakeSoundDetector:
    """Streams 16 kHz PCM through openWakeWord's models and reports each new detection of the phrase.

    ``feed`` is called from one thread only (the listener thread)."""

    def __init__(self, melspec_path: Path, embedding_path: Path, phrase_path: Path,
                 threshold: float = DEFAULT_THRESHOLD, cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
                 clock: Callable[[], float] = time.monotonic, load_session: Callable[[Path], object] = _session):
        self._melspec = load_session(melspec_path)
        self._embedding = load_session(embedding_path)
        self._phrase = load_session(phrase_path)
        self._melspec_input = self._melspec.get_inputs()[0].name
        self._embedding_input = self._embedding.get_inputs()[0].name
        self._phrase_input = self._phrase.get_inputs()[0]
        self._phrase_frames = int(self._phrase_input.shape[1])
        self.threshold = float(threshold)
        self._cooldown = float(cooldown_sec)
        self._clock = clock
        # Seed for the feature history: low-level noise, so the phrase model has a full window from the start.
        noise = np.random.default_rng(0).integers(-1000, 1000, SAMPLE_RATE * 4).astype(np.int16)
        self._seed_features = self._embed_clip(noise)
        self.reset()

    def reset(self) -> None:
        """Forget all audio heard so far."""
        self._raw = np.zeros(0, dtype=np.int16)
        self._pending = 0
        self._mel = np.ones((_MEL_WINDOW, _MEL_BINS), dtype=np.float32)
        self._features = self._seed_features.copy()
        self._scores_seen = 0
        self._above = False
        self._last_detection = float("-inf")
        self.last_score = 0.0

    def _melspectrogram(self, pcm: np.ndarray) -> np.ndarray:
        out = self._melspec.run(None, {self._melspec_input: pcm.astype(np.float32)[None, :]})[0]
        return np.squeeze(out).reshape(-1, _MEL_BINS) / 10.0 + 2.0

    def _embed(self, windows: np.ndarray) -> np.ndarray:
        out = self._embedding.run(None, {self._embedding_input: windows[..., None].astype(np.float32)})[0]
        return out.reshape(windows.shape[0], -1)

    def _embed_clip(self, pcm: np.ndarray) -> np.ndarray:
        mel = self._melspectrogram(pcm)
        starts = range(0, mel.shape[0] - _MEL_WINDOW + 1, 8)
        return self._embed(np.stack([mel[i:i + _MEL_WINDOW] for i in starts]))

    def _score_chunk(self) -> float:
        end = len(self._raw) - self._pending + CHUNK_SAMPLES
        start = max(0, end - CHUNK_SAMPLES - _MEL_CONTEXT)
        self._mel = np.vstack((self._mel, self._melspectrogram(self._raw[start:end])))[-_MEL_WINDOW * 2:]
        self._features = np.vstack((self._features, self._embed(self._mel[None, -_MEL_WINDOW:])))[-_FEATURE_HISTORY:]
        window = self._features[None, -self._phrase_frames:].astype(np.float32)
        score = float(np.squeeze(self._phrase.run(None, {self._phrase_input.name: window})[0]))
        self._scores_seen += 1
        return score if self._scores_seen > _WARMUP_SCORES else 0.0

    def feed(self, pcm: np.ndarray) -> bool:
        """Add 16 kHz int16 audio; True when the phrase has just been heard (once per phrase)."""
        if pcm.size == 0:
            return False
        self._raw = np.concatenate((self._raw, pcm.astype(np.int16, copy=False)))
        self._pending += pcm.size
        heard = False
        while self._pending >= CHUNK_SAMPLES:
            score = self._score_chunk()
            self._pending -= CHUNK_SAMPLES
            self.last_score = score
            above = score >= self.threshold
            if above and not self._above and self._clock() - self._last_detection >= self._cooldown:
                self._last_detection = self._clock()
                heard = True
            self._above = above
        keep = self._pending + CHUNK_SAMPLES + _MEL_CONTEXT
        if len(self._raw) > keep:
            self._raw = self._raw[-keep:]
        return heard


def phrase_model_path(name: str, fetch: Optional[Callable[[str], bytes]] = None) -> Path:
    """The phrase model for ``name``: a built-in name (fetched and verified) or a path to an .onnx file."""
    builtin = PHRASE_MODELS.get(name.strip().lower())
    if builtin is not None:
        return ensure_verified_file(builtin.filename, builtin.url, builtin.sha256, "the wake word sound model", fetch)
    path = Path(name).expanduser()
    if not path.is_file():
        raise ModelUnavailable(f"the wake word sound model {path.name} was not found")
    return path


def create_wake_sound_detector(cfg, fetch: Optional[Callable[[str], bytes]] = None) -> Optional[WakeSoundDetector]:
    """The detector when ``wake_sound_enabled`` is on and its models load; None (with a warning) otherwise."""
    if getattr(cfg, "wake_sound_enabled", False) is not True:
        return None
    model_name = str(getattr(cfg, "wake_sound_model", "") or DEFAULT_PHRASE_MODEL).strip()
    wake_word = str(getattr(cfg, "wake_word", "") or "").strip().lower()
    builtin_word = PHRASE_WAKE_WORDS.get(model_name.lower())
    if builtin_word is not None and wake_word != builtin_word:
        print(f"  ⚠️  Wake word sound detection is off: the built-in sound model only knows "
              f"\"{builtin_word.title()}\"", flush=True)
        return None
    try:
        melspec = ensure_verified_file(MELSPEC_MODEL.filename, MELSPEC_MODEL.url, MELSPEC_MODEL.sha256,
                                       "the wake word sound features model", fetch)
        embedding = ensure_verified_file(EMBEDDING_MODEL.filename, EMBEDDING_MODEL.url, EMBEDDING_MODEL.sha256,
                                         "the wake word sound embedding model", fetch)
        phrase = phrase_model_path(model_name, fetch)
        detector = WakeSoundDetector(melspec, embedding, phrase,
                                     threshold=float(getattr(cfg, "wake_sound_threshold", DEFAULT_THRESHOLD)))
    except ModelUnavailable as exc:
        print(f"  ⚠️  Wake word sound detection is off: {exc}", flush=True)
        return None
    except Exception as exc:  # onnxruntime missing or a model it cannot load
        print(f"  ⚠️  Wake word sound detection is off: its model could not be loaded ({type(exc).__name__})",
              flush=True)
        debug_log(f"wake sound detector failed to load: {exc}", "voice")
        return None
    debug_log(f"wake sound detector ready (threshold {detector.threshold})", "voice")
    return detector

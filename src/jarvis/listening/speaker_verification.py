"""Speaker verification: is this audio the enrolled owner's voice?

A WeSpeaker ResNet34 speaker-embedding model (ONNX Runtime, CPU, CC-BY-4.0)
turns speech into a 256-dimension vector. The score is the cosine similarity
between that vector and the owner's voiceprint. Everything runs locally;
audio and embeddings never leave the process.

Features are Kaldi-style log-mel filterbanks computed with numpy, so no
PyTorch is needed at runtime.
"""

from __future__ import annotations

import enum
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from ..debug import debug_log
from . import voiceprint

SAMPLE_RATE = 16000
MIN_VERIFY_SECONDS = 0.5  # shorter audio carries too little voice to judge
_VOICEPRINT_CHECK_SEC = 1.0  # how often a running verifier looks for its deleted voiceprint
MAX_VERIFY_SECONDS = 8.0  # longer audio adds latency, not accuracy
DEFAULT_REJECT_THRESHOLD = 0.25  # soft mode: below this is confidently another speaker
DEFAULT_ACCEPT_THRESHOLD = 0.45  # strict mode: at or above this is the owner

MODEL_FILENAME = "voxceleb_resnet34_LM.onnx"
MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"

_FRAME_LENGTH = 400  # 25 ms
_FRAME_SHIFT = 160  # 10 ms
_FFT_SIZE = 512
_NUM_MEL = 80
_LOW_FREQ = 20.0
_PREEMPHASIS = 0.97
_FLOOR = float(np.finfo(np.float32).eps)

_mel_banks_cache: Optional[np.ndarray] = None
_window_cache: Optional[np.ndarray] = None


def _mel(freq):
    return 1127.0 * np.log(1.0 + freq / 700.0)


def _mel_banks() -> np.ndarray:
    global _mel_banks_cache
    if _mel_banks_cache is None:
        nyquist = SAMPLE_RATE / 2.0
        mel_low, mel_high = _mel(_LOW_FREQ), _mel(nyquist)
        delta = (mel_high - mel_low) / (_NUM_MEL + 1)
        bins = np.arange(_NUM_MEL)[:, None]
        left = mel_low + bins * delta
        centre = left + delta
        right = centre + delta
        mel = _mel(np.arange(_FFT_SIZE // 2) * (SAMPLE_RATE / _FFT_SIZE))[None, :]
        up = (mel - left) / (centre - left)
        down = (right - mel) / (right - centre)
        _mel_banks_cache = np.maximum(0.0, np.minimum(up, down)).astype(np.float32)
    return _mel_banks_cache


def _window() -> np.ndarray:
    global _window_cache
    if _window_cache is None:
        n = np.arange(_FRAME_LENGTH)
        _window_cache = (0.54 - 0.46 * np.cos(2 * np.pi * n / (_FRAME_LENGTH - 1))).astype(np.float32)
    return _window_cache


def compute_fbank(audio) -> np.ndarray:
    """Mean-normalised 80-bin log-mel filterbanks of 16 kHz mono audio, shape (frames, 80)."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1) * 32768.0
    if len(samples) < _FRAME_LENGTH:
        return np.zeros((0, _NUM_MEL), dtype=np.float32)
    count = 1 + (len(samples) - _FRAME_LENGTH) // _FRAME_SHIFT
    index = np.arange(_FRAME_LENGTH)[None, :] + _FRAME_SHIFT * np.arange(count)[:, None]
    frames = samples[index]
    frames = frames - frames.mean(axis=1, keepdims=True)
    previous = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = (frames - _PREEMPHASIS * previous) * _window()
    power = np.abs(np.fft.rfft(frames, n=_FFT_SIZE, axis=1))[:, : _FFT_SIZE // 2] ** 2
    feats = np.log(np.maximum(power @ _mel_banks().T, _FLOOR))
    feats = feats - feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32)


def default_model_path() -> Path:
    """Where the speaker model lives (``JARVIS_SPEAKER_MODEL`` overrides it)."""
    env = os.environ.get("JARVIS_SPEAKER_MODEL")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "jarvis" / "models" / "speaker" / MODEL_FILENAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model() -> Path:
    """Return the model path, downloading the 26 MB file once if it is missing.

    Called only from explicit enrolment, never from the listening loop.
    """
    path = default_model_path()
    if path.is_file():
        return path
    from huggingface_hub import hf_hub_download

    path.parent.mkdir(parents=True, exist_ok=True)
    fetched = Path(hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILENAME,
                                   local_dir=str(path.parent)))
    if _sha256(fetched) != MODEL_SHA256:
        fetched.unlink(missing_ok=True)
        raise RuntimeError("speaker model failed its checksum; it was removed")
    if fetched != path:
        fetched.replace(path)
    return path


class SpeakerEmbedder:
    """Speaker embeddings from the WeSpeaker ONNX model (CPU)."""

    def __init__(self, model_path) -> None:
        from ..utils.onnx_privacy import disable_onnxruntime_telemetry

        disable_onnxruntime_telemetry()
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2  # leave the rest of the CPU free
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), sess_options=options,
                                             providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def embed(self, audio) -> np.ndarray:
        """Unit-length embedding of 16 kHz mono audio."""
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        samples = samples[-int(MAX_VERIFY_SECONDS * SAMPLE_RATE):]
        feats = compute_fbank(samples)
        if len(feats) == 0:
            raise ValueError("audio too short to embed")
        out = self._session.run(None, {self._input: feats[None, :, :]})[0][0]
        return out / max(float(np.linalg.norm(out)), 1e-9)


class Verdict(enum.Enum):
    OWNER = "owner"
    OTHER = "other"
    UNKNOWN = "unknown"  # too short, or the model failed: never grounds for ignoring speech


class SpeakerVerifier:
    """Decide whether audio is the owner's voice.

    ``soft`` rejects only a confidently different speaker (score below
    ``reject_threshold``); ``strict`` requires the owner's voice (score at or
    above ``accept_threshold``). Audio too short to judge, and any model
    failure, yield ``UNKNOWN`` and are allowed in both modes.
    """

    def __init__(self, embedder, owner_embedding, *, mode: str = "soft",
                 reject_threshold: float = DEFAULT_REJECT_THRESHOLD,
                 accept_threshold: float = DEFAULT_ACCEPT_THRESHOLD,
                 voiceprint_file=None, clock=time.monotonic) -> None:
        self.embedder = embedder
        owner = np.asarray(owner_embedding, dtype=np.float32)
        self._owner: Optional[np.ndarray] = owner / max(float(np.linalg.norm(owner)), 1e-9)
        # The stored voiceprint this template came from: once it is deleted the template is dropped
        # too, so deletion takes effect at once (checked at most once a second).
        self._voiceprint_file = voiceprint_file
        self._clock = clock
        self._next_file_check = float("-inf")
        self.mode = "strict" if mode == "strict" else "soft"
        self.reject_threshold = float(reject_threshold)
        self.accept_threshold = float(accept_threshold)
        self.last_score: Optional[float] = None  # newest score from check(), for debug logs only

    def score(self, audio, min_seconds: float = MIN_VERIFY_SECONDS) -> Optional[float]:
        """Cosine similarity to the voiceprint, or ``None`` when it cannot be judged.

        ``min_seconds`` is the shortest audio worth judging; barge-in passes a
        shorter window than the utterance gate because it must act within
        a few hundred milliseconds.
        """
        if not self.enrolled:
            return None
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(samples) < min_seconds * SAMPLE_RATE:
            return None
        try:
            vector = np.asarray(self.embedder.embed(samples), dtype=np.float32)
            # A voiceprint from a model of another size raises here; a malformed one gives NaN.
            score = float(vector @ self._owner / max(float(np.linalg.norm(vector)), 1e-9))
        except Exception as exc:
            debug_log(f"speaker embedding failed ({type(exc).__name__}); not blocking speech", "voice")
            return None
        if not np.isfinite(score):
            debug_log("speaker score not finite; not blocking speech", "voice")
            return None
        return score

    @property
    def enrolled(self) -> bool:
        """False once the stored voiceprint has been deleted; the in-memory template is then gone."""
        if self._owner is None:
            return False
        if self._voiceprint_file is not None and self._clock() >= self._next_file_check:
            self._next_file_check = self._clock() + _VOICEPRINT_CHECK_SEC
            if not Path(self._voiceprint_file).is_file():
                self._owner = None
                debug_log("voiceprint deleted; speaker verification stopped", "voice")
                return False
        return True

    def check(self, audio) -> Verdict:
        score = self.score(audio)
        self.last_score = score
        if score is None:
            return Verdict.UNKNOWN
        if self.mode == "strict":
            return Verdict.OWNER if score >= self.accept_threshold else Verdict.OTHER
        return Verdict.OTHER if score < self.reject_threshold else Verdict.OWNER

    def allows(self, audio) -> bool:
        return self.check(audio) is not Verdict.OTHER


_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(message, flush=True)


def create_verifier(cfg) -> Optional[SpeakerVerifier]:
    """Build the verifier for ``cfg``, or ``None`` when verification is off or unavailable.

    A missing voiceprint, model or runtime logs once and leaves speech
    ungated, so the offline baseline never breaks.
    """
    mode = str(getattr(cfg, "speaker_verification", "off") or "off").lower()
    if mode not in ("soft", "strict"):
        return None
    owner = voiceprint.load_voiceprint()
    if owner is None:
        _warn_once("voiceprint", "  ⚠️ Speaker verification is on but no voice is enrolled; "
                                 "run scripts/enrol_voice.py. Continuing without it.")
        return None
    model_path = default_model_path()
    if not model_path.is_file():
        _warn_once("model", "  ⚠️ Speaker verification model not found; "
                            "run scripts/enrol_voice.py to fetch it. Continuing without it.")
        return None
    try:
        embedder = SpeakerEmbedder(model_path)
    except Exception as exc:
        debug_log(f"speaker model failed to load ({type(exc).__name__})", "voice")
        _warn_once("load", "  ⚠️ Speaker verification model could not be loaded. Continuing without it.")
        return None
    verifier = SpeakerVerifier(
        embedder, owner, mode=mode, voiceprint_file=voiceprint.voiceprint_path(),
        reject_threshold=float(getattr(cfg, "speaker_reject_threshold", DEFAULT_REJECT_THRESHOLD)),
        accept_threshold=float(getattr(cfg, "speaker_accept_threshold", DEFAULT_ACCEPT_THRESHOLD)),
    )
    debug_log(f"speaker verification ready (mode={verifier.mode})", "voice")
    return verifier

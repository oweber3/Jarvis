"""Kokoro-82M voices on ONNX Runtime (``output/tts.spec.md``, Kokoro).

Kokoro runs on the ONNX Runtime that Piper already needs, with Piper's bundled espeak-ng for phonemes, so it
adds no Python package and no PyTorch. Its three model files are fetched only when Kokoro is chosen, from
pinned addresses, and kept only when their SHA-256 matches; after that everything runs offline.

Importing this module loads nothing heavy: ONNX Runtime, the model and the phonemiser load in
``load_kokoro_voice``.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, Iterator, List, Optional

from ..debug import debug_log
from .model_download import download_verified

SAMPLE_RATE = 24000
MAX_TOKENS = 509  # phoneme tokens per model run; the voice tables have a style row for each length
KOKORO_DEFAULT_VOICE = "bm_george"
KOKORO_SPEED_RANGE = (0.5, 2.0)
# The model pads each window with about 0.25 s of silence before and 0.5 s after; keep a little of each.
_LEAD_KEEP_S = 0.03
_TAIL_KEEP_S = 0.05
SENTENCE_PAUSE_S = 0.15  # silence after a sentence, so sentences do not run together
CLAUSE_PAUSE_S = 0.06  # silence after a clause spoken on its own (the first sentence's clauses)
_SILENCE_LEVEL = 0.01  # below this (of full scale) a sample counts as silence

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_CONFIG_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"  # hexgrad/Kokoro-82M (Apache-2.0)


@dataclass(frozen=True)
class KokoroFile:
    name: str
    url: str
    size: int
    sha256: str


KOKORO_FILES = (
    KokoroFile("kokoro-v1.0.onnx", f"{_RELEASE}/kokoro-v1.0.onnx", 325532387,
               "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    KokoroFile("voices-v1.0.bin", f"{_RELEASE}/voices-v1.0.bin", 28214398,
               "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
    KokoroFile("config.json", f"https://huggingface.co/hexgrad/Kokoro-82M/resolve/{_CONFIG_REVISION}/config.json",
               2351, "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f"),
)
KOKORO_DOWNLOAD_MB = round(sum(f.size for f in KOKORO_FILES) / 1_000_000)

# The English voices of Kokoro v1.0. The first letter picks the accent: a = American, b = British.
KOKORO_VOICES: Dict[str, str] = {
    "af_heart": "Heart (American, female)",
    "af_alloy": "Alloy (American, female)",
    "af_aoede": "Aoede (American, female)",
    "af_bella": "Bella (American, female)",
    "af_jessica": "Jessica (American, female)",
    "af_kore": "Kore (American, female)",
    "af_nicole": "Nicole (American, female, whispery)",
    "af_nova": "Nova (American, female)",
    "af_river": "River (American, female)",
    "af_sarah": "Sarah (American, female)",
    "af_sky": "Sky (American, female)",
    "am_adam": "Adam (American, male)",
    "am_echo": "Echo (American, male)",
    "am_eric": "Eric (American, male)",
    "am_fenrir": "Fenrir (American, male)",
    "am_liam": "Liam (American, male)",
    "am_michael": "Michael (American, male)",
    "am_onyx": "Onyx (American, male)",
    "am_puck": "Puck (American, male)",
    "am_santa": "Santa (American, male)",
    "bf_alice": "Alice (British, female)",
    "bf_emma": "Emma (British, female)",
    "bf_isabella": "Isabella (British, female)",
    "bf_lily": "Lily (British, female)",
    "bm_daniel": "Daniel (British, male)",
    "bm_fable": "Fable (British, male)",
    "bm_george": "George (British, male)",
    "bm_lewis": "Lewis (British, male)",
}
_ESPEAK_VOICES = {"a": "en-us", "b": "en"}

# espeak-ng keeps process-wide state; Piper uses the same library and sets its own voice before each use.
_espeak_lock = threading.Lock()
_espeak_ready = False


class KokoroUnavailable(Exception):
    """Kokoro cannot speak; the message says why in plain words."""


def kokoro_models_dir() -> Path:
    return Path.home() / ".local" / "share" / "jarvis" / "models" / "kokoro"


def kokoro_files_present(models_dir: Optional[Path] = None) -> bool:
    """Every model file is on disk with its pinned size (each was verified when downloaded)."""
    base = Path(models_dir or kokoro_models_dir())
    return all((base / f.name).is_file() and (base / f.name).stat().st_size == f.size for f in KOKORO_FILES)


def download_kokoro(models_dir: Optional[Path] = None, progress: Optional[Callable[[str], None]] = None) -> bool:
    """Fetch and verify the model files; True once all are present."""
    base = Path(models_dir or kokoro_models_dir())
    for f in KOKORO_FILES:
        if not download_verified(f.url, base / f.name, size=f.size, digest=f.sha256, algorithm="sha256",
                                 label=f"Kokoro {f.name}", progress=progress):
            return False
    return True


def gpu_acceleration_available() -> bool:
    """Whether the installed ONNX Runtime can run Kokoro on an NVIDIA graphics card (CUDA)."""
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _create_session(model_path: Path):
    """An ONNX session on the graphics card when this ONNX Runtime offers CUDA, otherwise the processor."""
    from ..utils.onnx_privacy import disable_onnxruntime_telemetry
    disable_onnxruntime_telemetry()
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3  # errors only; CUDA prints noisy placement warnings
    if "CUDAExecutionProvider" in ort.get_available_providers():
        try:
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()
            session = ort.InferenceSession(str(model_path), options,
                                           providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            if session.get_providers()[0] == "CUDAExecutionProvider":
                return session, "graphics card"
        except Exception as e:
            debug_log(f"Kokoro could not start on the graphics card ({type(e).__name__}); using the processor", "tts")
    return ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"]), "processor"


def load_kokoro_voice(models_dir: Optional[Path] = None, voice: str = KOKORO_DEFAULT_VOICE,
                      speed: float = 1.0) -> "KokoroVoice":
    """Load the model for ``voice``; raises ``KokoroUnavailable`` with the reason when it cannot."""
    base = Path(models_dir or kokoro_models_dir())
    if not kokoro_files_present(base):
        raise KokoroUnavailable("the Kokoro voice files are not downloaded yet")
    if voice not in KOKORO_VOICES:
        raise KokoroUnavailable(f"{voice} is not one of the Kokoro voices Jarvis offers")
    try:
        import numpy as np
        session, device = _create_session(base / "kokoro-v1.0.onnx")
        with np.load(base / "voices-v1.0.bin") as table:
            voices = {voice: table[voice]}
        with open(base / "config.json", encoding="utf-8") as fh:
            vocab = json.load(fh)["vocab"]
    except ImportError as e:
        raise KokoroUnavailable(f"ONNX Runtime is not installed ({e})") from e
    except Exception as e:
        raise KokoroUnavailable(f"the Kokoro model could not be loaded ({type(e).__name__}: {e})") from e
    loaded = KokoroVoice(session, voices, vocab, voice=voice, speed=speed)
    loaded.device = device
    try:
        # One short run now, so the first reply does not pay ONNX Runtime's (on a graphics card, CUDA's)
        # first-run set-up.
        for _ in loaded.synthesize("Ready."):
            pass
    except Exception as e:
        raise KokoroUnavailable(f"the Kokoro model could not speak ({type(e).__name__}: {e})") from e
    debug_log(f"Kokoro loaded on the {device}", "tts")
    return loaded


def _espeak_phonemes(text: str, espeak_voice: str) -> List[List[str]]:
    """Phonemes per sentence, as clauses ending in their punctuation (Piper's espeak-ng)."""
    from piper import espeakbridge
    from piper.phonemize_espeak import ESPEAK_DATA_DIR

    sentences: List[List[str]] = []
    clauses: List[str] = []
    global _espeak_ready
    with _espeak_lock:
        if not _espeak_ready:
            espeakbridge.initialize(str(ESPEAK_DATA_DIR))
            _espeak_ready = True
        espeakbridge.set_voice(espeak_voice)
        parts = espeakbridge.get_phonemes(text)
    for phonemes, terminator, end_of_sentence in parts:
        phonemes = re.sub(r"\([^)]+\)", "", phonemes)  # language switch flags
        clauses.append(unicodedata.normalize("NFC", phonemes + terminator) + " ")
        if end_of_sentence:
            sentences.append(clauses)
            clauses = []
    if clauses:
        sentences.append(clauses)
    return sentences


def _trim(samples, pause: float):
    """Cut the model's padding silence down to a short lead-in, then add ``pause`` seconds of silence."""
    import numpy as np

    voiced = np.nonzero(np.abs(samples) > _SILENCE_LEVEL)[0]
    if not len(voiced):
        return samples[:0]
    start = max(0, voiced[0] - int(_LEAD_KEEP_S * SAMPLE_RATE))
    end = min(len(samples), voiced[-1] + 1 + int(_TAIL_KEEP_S * SAMPLE_RATE))
    return np.concatenate([samples[start:end], np.zeros(int(pause * SAMPLE_RATE), dtype=samples.dtype)])


class KokoroVoice:
    """One Kokoro voice ready to speak. ``synthesize`` follows ``PiperVoice.synthesize``: one chunk per sentence,
    each with ``audio_int16_array``, so the streaming player treats both engines alike."""

    sample_rate = SAMPLE_RATE

    def __init__(self, session, voices: Dict[str, "object"], vocab: Dict[str, int], *,
                 voice: str = KOKORO_DEFAULT_VOICE, speed: float = 1.0) -> None:
        self._session = session
        self._style_table = voices[voice]
        self._vocab = vocab
        self.voice = voice
        low, high = KOKORO_SPEED_RANGE
        self.speed = max(low, min(high, float(speed)))
        self._espeak_voice = _ESPEAK_VOICES.get(voice[:1], "en-us")
        self.config = SimpleNamespace(sample_rate=SAMPLE_RATE)
        self.device = "processor"

    def _tokens(self, phonemes: str) -> List[int]:
        return [self._vocab[ch] for ch in phonemes if ch in self._vocab]

    def _windows(self, clauses: List[str]) -> Iterator[List[int]]:
        """Token windows for one sentence: whole when it fits, else clause groups, else word-split pieces."""
        tokens = self._tokens("".join(clauses).strip())
        if len(tokens) <= MAX_TOKENS:
            if tokens:
                yield tokens
            return
        pending: List[int] = []
        for clause in clauses:
            piece = self._tokens(clause)
            if len(pending) + len(piece) <= MAX_TOKENS:
                pending += piece
                continue
            if pending:
                yield pending
            while len(piece) > MAX_TOKENS:
                cut = MAX_TOKENS
                space = self._vocab.get(" ")
                if space is not None and space in piece[:MAX_TOKENS]:
                    cut = MAX_TOKENS - piece[:MAX_TOKENS][::-1].index(space)
                yield piece[:cut]
                piece = piece[cut:]
            pending = piece
        if pending:
            yield pending

    def synthesize(self, text: str, _config=None) -> Iterator[SimpleNamespace]:
        if not text.strip():
            return
        for index, clauses in enumerate(_espeak_phonemes(text, self._espeak_voice)):
            # The first sentence goes clause by clause so speech starts after its first clause is ready;
            # later sentences are synthesised while earlier ones play, and stay whole for smoother prosody.
            groups = [[clause] for clause in clauses] if index == 0 and len(clauses) > 1 else [clauses]
            for group_index, group in enumerate(groups):
                pause = SENTENCE_PAUSE_S if group_index == len(groups) - 1 else CLAUSE_PAUSE_S
                yield from self._speak(group, pause)

    def _speak(self, clauses: List[str], pause: float) -> Iterator[SimpleNamespace]:
        import numpy as np

        for tokens in self._windows(clauses):
            audio = self._session.run(None, {
                "tokens": np.array([[0, *tokens, 0]], dtype=np.int64),
                "style": np.asarray(self._style_table[len(tokens)], dtype=np.float32).reshape(1, -1),
                "speed": np.array([self.speed], dtype=np.float32),
            })[0]
            samples = _trim(np.clip(np.asarray(audio, dtype=np.float32).ravel(), -1.0, 1.0), pause)
            if len(samples):
                yield SimpleNamespace(audio_int16_array=(samples * 32767).astype(np.int16))

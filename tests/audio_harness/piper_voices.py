"""Speech synthesis with Piper into 16 kHz arrays, never to the speakers."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .audio import SAMPLE_RATE, resample


def default_voices_dir() -> Path:
    """Where harness voices live: ``JARVIS_HARNESS_VOICES`` or ``.tmp/wake-word/voices``."""
    env = os.environ.get("JARVIS_HARNESS_VOICES")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / ".tmp" / "wake-word" / "voices"


@dataclass(frozen=True)
class Voice:
    """One Piper voice, optionally a single speaker of a multi-speaker model."""

    model: str  # file stem, e.g. "en_GB-alan-medium"
    speaker: Optional[int] = None

    @property
    def label(self) -> str:
        return self.model if self.speaker is None else f"{self.model}#{self.speaker}"


def available_models(voices_dir: Optional[Path] = None) -> list[str]:
    folder = Path(voices_dir or default_voices_dir())
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.onnx") if (folder / f"{p.name}.json").exists())


@functools.lru_cache(maxsize=8)
def _load(model_path: str):
    from piper.voice import PiperVoice

    return PiperVoice.load(model_path, model_path + ".json")


def num_speakers(model: str, voices_dir: Optional[Path] = None) -> int:
    voice = _load(str(Path(voices_dir or default_voices_dir()) / f"{model}.onnx"))
    return int(getattr(voice.config, "num_speakers", 1) or 1)


def synthesise(text: str, voice: Voice, *, length_scale: float = 1.0,
               noise_scale: float = 0.667, noise_w: float = 0.8,
               voices_dir: Optional[Path] = None) -> np.ndarray:
    """Speak ``text`` with ``voice`` and return 16 kHz mono float32 audio."""
    from piper.config import SynthesisConfig

    piper_voice = _load(str(Path(voices_dir or default_voices_dir()) / f"{voice.model}.onnx"))
    config = SynthesisConfig(
        speaker_id=voice.speaker,
        length_scale=length_scale,
        noise_scale=noise_scale,
        noise_w_scale=noise_w,
    )
    chunks = [chunk.audio_float_array for chunk in piper_voice.synthesize(text, config)]
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return resample(audio, piper_voice.config.sample_rate, SAMPLE_RATE)

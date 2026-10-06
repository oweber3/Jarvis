"""WAV harness: feed recorded or synthesised audio through the voice listener.

No microphone, no speakers. Typical use::

    from tests.audio_harness import ListenerHarness, fixture, silence

    clip = fixture("wake_command__jarvis_open_word__alan.wav")
    with ListenerHarness() as harness:
        harness.play(clip.audio, text=clip.text)
        harness.play(silence(1.5))
    harness.whisper_calls   # utterances that reached Whisper
    harness.collections     # requests the listener started
    harness.dispatched      # queries handed to the reply engine

See ``README.md`` in this folder for the fixture corpus and how to
regenerate it with Piper.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .audio import (
    SAMPLE_RATE,
    concat,
    duration,
    fit,
    load_wav,
    mix,
    music_bed,
    pad,
    pink_noise,
    resample,
    rms,
    save_wav,
    silence,
    to_int16,
    white_noise,
)
from .listener_harness import (
    FakeTTS,
    ListenerHarness,
    OracleTranscriber,
    Played,
    SimClock,
    WhisperCall,
    WhisperTranscriber,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class Fixture:
    name: str
    kind: str  # "wake", "wake_command", "near_miss" or "speech"
    text: str
    voice: str
    audio: np.ndarray

    @property
    def duration(self) -> float:
        return duration(self.audio)


@lru_cache(maxsize=1)
def _manifest() -> dict:
    return json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))


def fixture(name: str) -> Fixture:
    meta = _manifest()[name]
    return Fixture(name, meta["kind"], meta["text"], meta["voice"], load_wav(FIXTURES_DIR / name))


def fixtures_of_kind(kind: str) -> list[Fixture]:
    return [fixture(name) for name, meta in _manifest().items() if meta["kind"] == kind]


def all_fixtures() -> list[Fixture]:
    return [fixture(name) for name in _manifest()]


__all__ = [
    "SAMPLE_RATE", "FakeTTS", "Fixture", "FIXTURES_DIR", "ListenerHarness", "OracleTranscriber", "Played",
    "SimClock", "WhisperCall", "WhisperTranscriber", "all_fixtures", "concat", "duration",
    "fit", "fixture", "fixtures_of_kind", "load_wav", "mix", "music_bed", "pad",
    "pink_noise", "resample", "rms", "save_wav", "silence", "to_int16", "white_noise",
]

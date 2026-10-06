"""Speaker-verification corpus built from Piper voices, never from a microphone.

One Piper voice plays "the owner", the others play other people and Jarvis's
own TTS. Needs the speaker model (``JARVIS_SPEAKER_MODEL`` or the default
model path) and Piper voices (``JARVIS_HARNESS_VOICES``); ``available()``
says whether both are present so tests can skip cleanly.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Optional

import numpy as np

from .audio import SAMPLE_RATE, mix, music_bed, pink_noise, rms
from .piper_voices import Voice, available_models, synthesise

OWNER = Voice("en_GB-alan-medium")
JARVIS_TTS = Voice("en_US-lessac-medium")  # what Jarvis sounds like in the room
OTHER_MODELS = ["en_GB-alba-medium", "en_US-ryan-medium", "en_US-amy-medium", "en_US-joe-medium",
                "en_US-hfc_male-medium", "en_GB-cori-medium", "en_GB-jenny_dioco-medium",
                "en_GB-northern_english_male-medium"]
OTHER_LIBRITTS = [12, 305, 450, 600, 750]
ENROL_PHRASES = [
    "Jarvis, what time is it?", "Open Word and pause the music.", "What is the weather like today?",
    "Set the volume to thirty percent.", "Tell me something interesting about space.",
    "Remind me to call the dentist tomorrow morning.",
]
HELD_OUT_PHRASES = [
    "Jarvis, open the calendar please.", "I would like to hear the news this evening.",
    "Can you turn the brightness down a little?", "Stop talking and listen to me for a second.",
    "What is using the most memory on my computer?",
]
JARVIS_REPLY = ("The time is ten past four in the afternoon. It is currently raining in London, "
                "with a high of fourteen degrees and a light breeze from the west.")


def model_path() -> Optional[Path]:
    from jarvis.listening.speaker_verification import default_model_path

    path = default_model_path()
    return path if path.is_file() else None


def available() -> bool:
    models = set(available_models())
    needed = {OWNER.model, JARVIS_TTS.model, "en_US-libritts_r-medium", *OTHER_MODELS}
    return model_path() is not None and needed <= models


def skip_reason() -> str:
    return ("needs the speaker model (JARVIS_SPEAKER_MODEL) and Piper voices "
            "(JARVIS_HARNESS_VOICES); see tests/audio_harness/README.md")


@functools.lru_cache(maxsize=None)
def speak(text: str, voice: Voice) -> np.ndarray:
    return synthesise(text, voice)


@functools.lru_cache(maxsize=1)
def embedder():
    from jarvis.listening.speaker_verification import SpeakerEmbedder

    return SpeakerEmbedder(model_path())


def other_voices() -> list[Voice]:
    return [Voice(m) for m in OTHER_MODELS] + [Voice("en_US-libritts_r-medium", s) for s in OTHER_LIBRITTS]


@functools.lru_cache(maxsize=None)
def enrolled_centroid(voice: Voice = OWNER) -> np.ndarray:
    rows = np.stack([embedder().embed(speak(t, voice)) for t in ENROL_PHRASES])
    centroid = rows.mean(axis=0)
    return centroid / np.linalg.norm(centroid)


def make_verifier(mode: str = "soft", **kwargs):
    from jarvis.listening.speaker_verification import SpeakerVerifier

    return SpeakerVerifier(embedder(), enrolled_centroid(), mode=mode, **kwargs)


def with_bed(audio: np.ndarray, condition: Optional[str], seed: int = 0) -> np.ndarray:
    """Mix ``audio`` over a bed: ``None`` (clean), ``"music10"``, ``"noise10"`` or ``"noise5"``."""
    if condition is None:
        return audio
    seconds = len(audio) / SAMPLE_RATE + 1.0
    bed = music_bed(seconds, seed=seed) if condition.startswith("music") else pink_noise(seconds, seed=seed)
    return mix(audio, bed, snr_db=float(condition.lstrip("musicnoe")))


def room_with_jarvis_talking(tts_audio: np.ndarray, user_audio: Optional[np.ndarray] = None, *,
                             user_offset: float = 1.0, user_over_tts_db: float = 0.0,
                             tts_level: float = 0.04) -> np.ndarray:
    """What the microphone hears while Jarvis speaks and, optionally, someone talks over it.

    The TTS leaks in at RMS ``tts_level``; the user's speech is placed
    ``user_over_tts_db`` dB above it, starting ``user_offset`` seconds in.
    """
    tts = np.asarray(tts_audio, dtype=np.float32)
    out = tts * (tts_level / max(rms(tts), 1e-9))
    if user_audio is not None:
        user = np.asarray(user_audio, dtype=np.float32)
        user = user * (tts_level * 10 ** (user_over_tts_db / 20.0) / max(rms(user), 1e-9))
        start = int(user_offset * SAMPLE_RATE)
        end = start + len(user)
        if end > len(out):
            out = np.concatenate([out, np.zeros(end - len(out), dtype=np.float32)])
        out[start:end] += user
    return out

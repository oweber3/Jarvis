"""Enrol the owner's voice from a handful of recorded phrases or WAV files.

Only the speaker embedding of each clip is kept (see ``voiceprint``); the
audio itself is never written anywhere by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

from ..debug import debug_log
from . import voiceprint
from .speaker_verification import MIN_VERIFY_SECONDS, SAMPLE_RATE

MIN_CLIPS = 3
MIN_CLIP_SECONDS = 1.0
SAME_VOICE_MEAN = 0.4  # mean cosine between enrolled clips; one voice scores about 0.7
OUTLIER_THRESHOLD = 0.3  # cosine to the other clips' centroid below which a clip is not the same voice


class EnrolmentError(Exception):
    """The clips cannot make a trustworthy voiceprint."""


@dataclass
class EnrolmentResult:
    accepted: int
    rejected: list = field(default_factory=list)  # (clip index, reason)


def enrol(clips: Iterable, embedder, *, min_clips: int = MIN_CLIPS,
          min_seconds: float = max(MIN_CLIP_SECONDS, MIN_VERIFY_SECONDS),
          outlier_threshold: float = OUTLIER_THRESHOLD) -> EnrolmentResult:
    """Embed 16 kHz mono ``clips``, drop unusable ones and store the voiceprint.

    Clips shorter than ``min_seconds`` are skipped. A clip that does not match
    the others (someone else spoke, or it is noise) is dropped as an outlier.
    Raises ``EnrolmentError`` and stores nothing when fewer than ``min_clips``
    remain.
    """
    rejected: list = []
    kept: list = []  # (index, embedding)
    for index, clip in enumerate(clips):
        audio = np.asarray(clip, dtype=np.float32).reshape(-1)
        if len(audio) < min_seconds * SAMPLE_RATE:
            rejected.append((index, "too short"))
            continue
        try:
            vector = np.asarray(embedder.embed(audio), dtype=np.float32)
        except Exception as exc:
            rejected.append((index, f"could not be analysed ({type(exc).__name__})"))
            continue
        kept.append((index, vector / max(float(np.linalg.norm(vector)), 1e-9)))

    while len(kept) > min_clips:
        matrix = np.stack([v for _, v in kept])
        total = matrix.sum(axis=0)
        scores = []
        for row in matrix:
            others = total - row
            scores.append(float(row @ others / max(float(np.linalg.norm(others)), 1e-9)))
        worst = int(np.argmin(scores))
        if scores[worst] >= outlier_threshold:
            break
        rejected.append((kept[worst][0], "does not sound like the other clips"))
        kept.pop(worst)

    if len(kept) < min_clips:
        raise EnrolmentError(
            f"only {len(kept)} usable clip(s); at least {min_clips} clips of at least "
            f"{min_seconds:.0f} s from the same voice are needed")
    matrix = np.stack([v for _, v in kept])
    similarity = matrix @ matrix.T
    pairs = len(kept) * (len(kept) - 1)
    if (float(similarity.sum()) - len(kept)) / pairs < SAME_VOICE_MEAN:
        raise EnrolmentError("the clips do not sound like one voice; record them again in a quiet room")
    voiceprint.save_voiceprint(np.stack([v for _, v in kept]))
    rejected.sort()
    debug_log(f"enrolment finished: {len(kept)} accepted, {len(rejected)} rejected", "voice")
    return EnrolmentResult(accepted=len(kept), rejected=rejected)


def enrol_from_wavs(paths: Iterable, embedder, **kwargs) -> EnrolmentResult:
    """Enrol from 16-bit PCM WAV files (any rate or channel count; read as 16 kHz mono)."""
    import wave

    clips = []
    for path in paths:
        with wave.open(str(path), "rb") as handle:
            channels, width, rate = handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
            raw = handle.readframes(handle.getnframes())
        if width != 2:
            raise EnrolmentError(f"{path}: only 16-bit PCM WAV files are supported")
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        if rate != SAMPLE_RATE:
            from math import gcd

            from scipy.signal import resample_poly

            divisor = gcd(int(rate), SAMPLE_RATE)
            audio = resample_poly(audio, SAMPLE_RATE // divisor, int(rate) // divisor).astype(np.float32)
        clips.append(audio)
    return enrol(clips, embedder, **kwargs)


DEFAULT_PROMPTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Jarvis, what is the weather going to be like this evening?",
    "Please open the calendar and read me tomorrow's meetings.",
    "Set the volume to forty percent and pause the music.",
    "Remind me to call my sister when I get home tonight.",
    "How many kilometres is it from here to the airport?",
]


def _record_from_microphone(seconds: float):
    """Record ``seconds`` of 16 kHz mono audio from the configured microphone."""
    import sounddevice as sd

    from ..config import load_settings
    from ..utils.audio_capture import resolve_input_device

    device = resolve_input_device(sd, load_settings().voice_device)
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1,
                   dtype="float32", **device)
    sd.wait()
    return audio.reshape(-1)


def main(argv=None, *, recorder=None, embedder=None, out=print) -> int:
    """Command line for ``scripts/enrol_voice.py``. Returns the process exit code."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Enrol your voice so Jarvis can tell it from other people and from its own speech. "
                    "The voiceprint stays beside your config file and is never sent anywhere.")
    parser.add_argument("--wav", nargs="+", metavar="FILE",
                        help="enrol from 16-bit WAV files instead of the microphone")
    parser.add_argument("--status", action="store_true", help="say whether a voice is enrolled")
    parser.add_argument("--delete", action="store_true", help="delete the stored voiceprint")
    parser.add_argument("--seconds", type=float, default=5.0, help="recording length per phrase (default 5)")
    parser.add_argument("--phrases-file", metavar="FILE", help="read the prompts from a text file, one per line")
    args = parser.parse_args(argv)

    if args.status:
        if voiceprint.has_voiceprint():
            out("✅ Voice enrolled")
            out(f"   📁 Stored at {voiceprint.voiceprint_path()}")
        else:
            out("ℹ️  Voice not enrolled")
        return 0
    if args.delete:
        if voiceprint.delete_voiceprint():
            out("🗑️  Voiceprint deleted")
        else:
            out("ℹ️  No voiceprint to delete")
        return 0

    if embedder is None:
        from .speaker_verification import SpeakerEmbedder, default_model_path, ensure_model

        if not default_model_path().is_file():
            out("📥 Downloading the speaker model once (26 MB, WeSpeaker, CC-BY-4.0)...")
        try:
            embedder = SpeakerEmbedder(ensure_model())
        except Exception as exc:
            out(f"❌ Could not load the speaker model: {exc}")
            return 1

    out("🎙️  Voice enrolment")
    try:
        if args.wav:
            result = enrol_from_wavs(args.wav, embedder)
        else:
            prompts = DEFAULT_PROMPTS
            if args.phrases_file:
                with open(args.phrases_file, encoding="utf-8") as handle:
                    prompts = [line.strip() for line in handle if line.strip()] or DEFAULT_PROMPTS
            record = recorder or _record_from_microphone
            out(f"   📝 Read each phrase aloud in your normal voice: {len(prompts)} phrases, "
                f"{args.seconds:.0f} seconds each.")
            clips = []
            for number, phrase in enumerate(prompts, 1):
                out(f"   ▶️  Phrase {number} of {len(prompts)}: \"{phrase}\"")
                out(f"      🔴 Recording for {args.seconds:.0f} seconds...")
                clips.append(record(args.seconds))
                out("      ✅ Captured")
            out("   🧠 Analysing your voice...")
            result = enrol(clips, embedder)
    except EnrolmentError as exc:
        out(f"❌ Enrolment failed: {exc}")
        return 1
    except Exception as exc:
        out(f"❌ Enrolment failed ({type(exc).__name__}): {exc}")
        return 1
    for index, reason in result.rejected:
        out(f"   ⚠️  Clip {index + 1} skipped: {reason}")
    out(f"✅ Voice enrolled from {result.accepted} clips")
    out("   ➡️  Turn it on in Settings, Voice Input, Speaker verification (soft or strict)")
    return 0

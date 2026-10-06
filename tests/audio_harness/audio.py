"""Audio helpers for the WAV harness: 16 kHz mono float32 in [-1, 1].

Everything here is deterministic (seeded) so tests and metrics are
reproducible. Nothing is played or recorded.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

SAMPLE_RATE = 16000


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Band-limited resampling (polyphase) to ``dst_rate``."""
    audio = np.asarray(audio, dtype=np.float32)
    if src_rate == dst_rate:
        return audio
    from math import gcd
    from scipy.signal import resample_poly

    g = gcd(int(src_rate), int(dst_rate))
    return resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)


def load_wav(path: str | Path, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Read a PCM WAV file as mono float32 at ``rate``."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        src_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM is supported")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return resample(audio, src_rate, rate)


def save_wav(path: str | Path, audio: np.ndarray, rate: int = SAMPLE_RATE) -> Path:
    """Write mono float32 audio as 16-bit PCM."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return path


def duration(audio: np.ndarray, rate: int = SAMPLE_RATE) -> float:
    return len(audio) / float(rate)


def rms(audio: np.ndarray) -> float:
    audio = np.asarray(audio, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


def silence(seconds: float, rate: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(int(round(seconds * rate)), dtype=np.float32)


def white_noise(seconds: float, level: float = 0.01, seed: int = 0,
                rate: int = SAMPLE_RATE) -> np.ndarray:
    """Gaussian noise with RMS ``level``."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(round(seconds * rate))) * level).astype(np.float32)


def pink_noise(seconds: float, level: float = 0.01, seed: int = 0,
               rate: int = SAMPLE_RATE) -> np.ndarray:
    """1/f noise (fan, room tone) with RMS ``level``."""
    n = int(round(seconds * rate))
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.arange(len(spectrum), dtype=np.float64)
    freqs[0] = 1.0
    shaped = np.fft.irfft(spectrum / np.sqrt(freqs), n=n).astype(np.float32)
    return shaped * (level / max(rms(shaped), 1e-9))


def music_bed(seconds: float, level: float = 0.05, seed: int = 0,
              rate: int = SAMPLE_RATE) -> np.ndarray:
    """A synthetic music bed: chord changes with harmonics, a bass line and a beat."""
    rng = np.random.default_rng(seed)
    n = int(round(seconds * rate))
    t = np.arange(n, dtype=np.float64) / rate
    out = np.zeros(n, dtype=np.float64)
    beat = 0.5  # 120 bpm
    roots = [220.0, 246.94, 196.0, 261.63, 174.61, 293.66]
    bar = 4 * beat
    for start in np.arange(0.0, seconds, bar):
        root = roots[int(rng.integers(len(roots)))]
        sl = slice(int(start * rate), min(n, int((start + bar) * rate)))
        tt = t[sl] - start
        env = np.minimum(1.0, tt * 20) * np.exp(-tt * 0.6)
        for ratio in (1.0, 1.25, 1.5, 2.0):
            for harmonic, gain in ((1, 1.0), (2, 0.4), (3, 0.2)):
                out[sl] += gain * env * np.sin(2 * np.pi * root * ratio * harmonic * tt)
        out[sl] += 1.2 * env * np.sin(2 * np.pi * root / 2 * tt)
    for start in np.arange(0.0, seconds, beat):
        sl = slice(int(start * rate), min(n, int((start + 0.12) * rate)))
        tt = t[sl] - start
        out[sl] += 3.0 * np.exp(-tt * 40) * np.sin(2 * np.pi * 60 * tt)
        out[sl] += 0.6 * np.exp(-tt * 60) * rng.standard_normal(len(tt))
    out = out.astype(np.float32)
    return out * (level / max(rms(out), 1e-9))


def fit(audio: np.ndarray, seconds: float, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Tile or trim ``audio`` to exactly ``seconds``."""
    n = int(round(seconds * rate))
    if len(audio) == 0:
        return np.zeros(n, dtype=np.float32)
    reps = int(np.ceil(n / len(audio)))
    return np.tile(audio, reps)[:n].astype(np.float32)


def mix(foreground: np.ndarray, background: Optional[np.ndarray], snr_db: Optional[float] = None,
        offset: float = 0.0, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Overlay ``foreground`` on ``background`` starting at ``offset`` seconds.

    With ``snr_db`` the background is scaled so that the foreground's RMS
    sits that many dB above it. The result is as long as the longer input.
    """
    fg = np.asarray(foreground, dtype=np.float32)
    if background is None:
        return fg.copy()
    bg = np.asarray(background, dtype=np.float32)
    if snr_db is not None:
        target = rms(fg) / (10 ** (snr_db / 20.0))
        bg = bg * (target / max(rms(bg), 1e-9))
    start = int(round(offset * rate))
    out = np.zeros(max(len(bg), start + len(fg)), dtype=np.float32)
    out[:len(bg)] += bg
    out[start:start + len(fg)] += fg
    return np.clip(out, -1.0, 1.0)


def concat(clips: Iterable[np.ndarray], gap: float = 0.0, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Join clips with ``gap`` seconds of silence between them."""
    parts: list[np.ndarray] = []
    pause = silence(gap, rate)
    for i, clip in enumerate(clips):
        if i and gap > 0:
            parts.append(pause)
        parts.append(np.asarray(clip, dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def pad(audio: np.ndarray, before: float = 0.0, after: float = 0.0,
        rate: int = SAMPLE_RATE) -> np.ndarray:
    return concat([silence(before, rate), audio, silence(after, rate)])


def to_int16(audio: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767).astype(np.int16)

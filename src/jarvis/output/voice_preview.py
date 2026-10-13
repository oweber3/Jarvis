"""Speak a sample line with a voice before it is saved (``output/tts.spec.md``, Preview).

The preview builds the same engine the daemon would from the given config values, so what plays is what
Jarvis will sound like, including Piper standing in when Kokoro cannot speak. It fetches what the chosen
voice needs first (verified, from the pinned addresses) and never changes the saved settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from ..debug import debug_log
from . import kokoro
from .piper_voices import PIPER_DEFAULT_VOICE, piper_voice, piper_voice_downloaded

SAMPLE_LINE = "Hello, I'm {name}. This is how I will sound when I answer you."


class PreviewUnavailable(Exception):
    """The preview cannot play; the message says why in plain words."""


@dataclass(frozen=True)
class PreviewResult:
    samples: Any  # int16 numpy array
    sample_rate: int
    engine: str  # the engine that spoke: "piper" or "kokoro"
    note: Optional[str]  # why Kokoro did not speak, when Piper stood in


def render_preview(values: Mapping[str, Any], text: str,
                   progress: Optional[Callable[[str], None]] = None) -> PreviewResult:
    """Synthesise ``text`` with the voice the config ``values`` describe; raises ``PreviewUnavailable``."""
    from .tts import _download_piper_voice, create_tts_engine

    def get(key: str, default: Any = None) -> Any:
        value = values.get(key)
        return default if value in (None, "") else value

    engine = str(get("tts_engine", "piper")).lower()
    if engine == "chatterbox":
        raise PreviewUnavailable("Preview is available for Piper and Kokoro voices.")
    if engine == "kokoro" and not kokoro.kokoro_files_present():
        kokoro.download_kokoro(progress=progress)
    piper_key = str(get("tts_piper_voice", PIPER_DEFAULT_VOICE))
    if not get("tts_piper_model_path") and piper_voice(piper_key) and not piper_voice_downloaded(piper_key):
        _download_piper_voice(piper_key, progress)

    tts = create_tts_engine(
        engine=engine,
        enabled=True,
        piper_model_path=get("tts_piper_model_path"),
        piper_voice=piper_key,
        piper_speaker=get("tts_piper_speaker"),
        piper_length_scale=float(get("tts_piper_length_scale", 0.65)),
        piper_noise_scale=float(get("tts_piper_noise_scale", 0.8)),
        piper_noise_w=float(get("tts_piper_noise_w", 1.0)),
        kokoro_voice=str(get("tts_kokoro_voice", kokoro.KOKORO_DEFAULT_VOICE)),
        kokoro_speed=float(get("tts_kokoro_speed", 1.0)),
        kokoro_device=str(get("tts_kokoro_device", "gpu")),
    )
    samples, rate = tts.render(text)
    if samples is None or not len(samples):
        raise PreviewUnavailable(tts._init_error or "The voice produced no sound.")
    used = getattr(tts, "engine_in_use", None) or "piper"
    debug_log(f"Voice preview rendered with {used}", "tts")
    return PreviewResult(samples=samples, sample_rate=rate, engine=used, note=getattr(tts, "fallback_reason", None))


def play(result: PreviewResult) -> None:
    """Play the preview on this PC's default output and wait for it to finish."""
    import sounddevice as sd
    from ..utils.audio_lock import portaudio_lock

    with portaudio_lock:
        sd.play(result.samples, result.sample_rate)
    sd.wait()

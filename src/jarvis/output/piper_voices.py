"""The Piper voices Jarvis offers and downloads (``output/tts.spec.md``, Voice files).

``piper_voices.json`` is a pinned copy of the English entries of the Piper voice catalogue (release v1.0.0):
each voice's two files with the size and MD5 digest the catalogue publishes. A voice downloads only from that
release and is kept only when both files match. Nothing is fetched to build the list.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

PIPER_DEFAULT_VOICE = "en_GB-alan-medium"
PIPER_VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

_ACCENTS = {"en_GB": "British English", "en_US": "American English"}


@dataclass(frozen=True)
class PiperVoiceFile:
    name: str  # file name on disk, e.g. "en_GB-alan-medium.onnx"
    url: str
    size: int
    md5: str


@dataclass(frozen=True)
class PiperVoice:
    key: str
    language: str
    quality: str
    speakers: int
    files: Tuple[PiperVoiceFile, ...]
    label: str

    @property
    def download_mb(self) -> int:
        return max(1, round(sum(f.size for f in self.files) / 1_000_000))


def _label(name: str, language: str, quality: str, speakers: int) -> str:
    title = " ".join(part.capitalize() for part in name.split("_"))
    accent = _ACCENTS.get(language, language)
    extra = f", {speakers} speakers" if speakers > 1 else ""
    return f"{title} ({accent}, {quality} quality{extra})"


@lru_cache(maxsize=1)
def _catalogue() -> dict:
    with open(Path(__file__).with_name("piper_voices.json"), encoding="utf-8") as fh:
        return json.load(fh)["voices"]


def _file_checks(key: str) -> Dict[str, Tuple[int, str]]:
    """``{file name: (size, md5)}`` for a catalogued voice."""
    entry = _catalogue()[key]
    return {Path(rel).name: (meta["size"], meta["md5"]) for rel, meta in entry["files"].items()}


def _build(key: str) -> PiperVoice:
    entry = _catalogue()[key]
    checks = _file_checks(key)
    files = tuple(
        PiperVoiceFile(name=Path(rel).name, url=f"{PIPER_VOICE_BASE_URL}/{rel}",
                       size=checks[Path(rel).name][0], md5=checks[Path(rel).name][1])
        for rel in sorted(entry["files"])
    )
    return PiperVoice(key=key, language=entry["language"], quality=entry["quality"], speakers=entry["speakers"],
                      files=files, label=_label(entry["name"], entry["language"], entry["quality"],
                                                entry["speakers"]))


def piper_voices() -> Dict[str, PiperVoice]:
    """Every offered voice by key, in catalogue order."""
    return {key: _build(key) for key in _catalogue()}


def piper_voice(key: str) -> Optional[PiperVoice]:
    return _build(key) if key in _catalogue() else None


def piper_models_dir() -> Path:
    """Where Piper voices are stored."""
    base = Path.home() / ".local" / "share" / "jarvis" / "models" / "piper"
    base.mkdir(parents=True, exist_ok=True)
    return base


def piper_voice_path(key: str, models_dir: Optional[Path] = None) -> Path:
    return Path(models_dir or piper_models_dir()) / f"{key}.onnx"


def piper_voice_downloaded(key: str, models_dir: Optional[Path] = None) -> bool:
    """Both of the voice's files are on disk (they are verified when downloaded)."""
    path = piper_voice_path(key, models_dir)
    return path.is_file() and Path(str(path) + ".json").is_file()

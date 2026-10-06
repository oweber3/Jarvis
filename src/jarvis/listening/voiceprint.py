"""Local storage for the owner's voiceprint.

A voiceprint is biometric data. It is written only beside ``config.json``,
is never logged or printed, is never passed to a reply mode, and can be
deleted at any time (Settings, or ``scripts/enrol_voice.py --delete``).
Nothing here records the embedding values or the audio they came from.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np

from ..debug import debug_log

VOICEPRINT_FILENAME = "voiceprint.npz"


def voiceprint_path() -> Path:
    """The voiceprint file, in the directory of the active config file."""
    from ..config import default_config_path

    env = os.environ.get("JARVIS_CONFIG_PATH")
    config_path = Path(env).expanduser() if env else default_config_path()
    return config_path.parent / VOICEPRINT_FILENAME


def has_voiceprint() -> bool:
    return voiceprint_path().is_file()


def save_voiceprint(embeddings) -> None:
    """Store the enrolment embeddings (one row per phrase) and their unit centroid."""
    rows = np.atleast_2d(np.asarray(embeddings, dtype=np.float32))
    if rows.size == 0:
        raise ValueError("no embeddings to enrol")
    rows = rows / np.maximum(np.linalg.norm(rows, axis=1, keepdims=True), 1e-9)
    centroid = rows.mean(axis=0)
    centroid = centroid / max(float(np.linalg.norm(centroid)), 1e-9)
    path = voiceprint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        np.savez(handle, centroid=centroid.astype(np.float32), embeddings=rows)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    debug_log(f"voiceprint saved ({len(rows)} enrolment phrases)", "voice")


def load_voiceprint() -> Optional[np.ndarray]:
    """The unit-length voiceprint centroid, or ``None`` when absent or unreadable."""
    path = voiceprint_path()
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            centroid = np.asarray(data["centroid"], dtype=np.float32)
    except Exception as exc:
        debug_log(f"voiceprint unreadable ({type(exc).__name__}); treating as not enrolled", "voice")
        return None
    return centroid


def delete_voiceprint() -> bool:
    """Remove the voiceprint. Returns whether a file was deleted."""
    path = voiceprint_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    debug_log("voiceprint deleted", "voice")
    return True

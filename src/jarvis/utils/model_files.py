"""Model files fetched once and verified against a pinned SHA-256 before they are saved or used."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

from ..debug import debug_log


class ModelUnavailable(Exception):
    """A model file is missing and could not be fetched and verified."""


def models_dir() -> Path:
    return Path.home() / ".local" / "share" / "jarvis" / "models"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(url: str) -> bytes:
    import requests

    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def ensure_verified_file(filename: str, url: str, sha256: str, what: str,
                         fetch: Optional[Callable[[str], bytes]] = None) -> Path:
    """The path of ``filename`` in the models folder, fetched from ``url`` when it is missing or does not
    match ``sha256``. ``what`` names the model in errors. Nothing is saved unless it matches."""
    path = models_dir() / filename
    try:
        if path.is_file() and _sha256(path.read_bytes()) == sha256:
            return path
    except OSError:
        pass
    debug_log(f"fetching {what}", "models")
    try:
        data = (fetch or _download)(url)
    except Exception as exc:
        raise ModelUnavailable(f"{what} could not be downloaded ({exc})") from None
    if _sha256(data) != sha256:
        raise ModelUnavailable(f"{what} failed its checksum after downloading")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        partial.write_bytes(data)
        os.replace(partial, path)
    except OSError as exc:
        raise ModelUnavailable(f"{what} could not be saved ({exc})") from None
    return path

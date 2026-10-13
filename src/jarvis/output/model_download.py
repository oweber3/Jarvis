"""Verified downloads of voice model files (``output/tts.spec.md``, Voice files).

A file is fetched only from the pinned address its caller gives, written to a temporary name, and moved into
place only when its size and digest match the pinned values. Nothing is sent but the request for the file.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..debug import debug_log

MAX_RETRIES = 4  # retries after HTTP 429 (rate limited), waiting 2, 4, 8, 16 seconds


def _matches(path: Path, size: int, digest: str, algorithm: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest() == digest.lower()


def download_verified(url: str, target: Path, *, size: int, digest: str, algorithm: str, label: str,
                      progress: Optional[Callable[[str], None]] = None) -> bool:
    """Fetch ``url`` to ``target`` and keep it only when its size and digest match; True when ``target`` is good.

    An existing file that already matches is kept without a request. Progress lines use the
    ``📥 <label>: NN%|| ...`` form the desktop log viewer turns into a progress bar.
    """
    import requests

    def say(message: str) -> None:
        if progress:
            try:
                progress(message)
            except Exception as e:  # a closed window must not abort the download
                debug_log(f"{label} progress report failed ({type(e).__name__})", "tts")

    target = Path(target)
    if _matches(target, size, digest, algorithm):
        debug_log(f"{label} already present", "tts")
        return True
    # A name of its own per download, so two downloads of one file never write the same partial file.
    temp_path = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        response = None
        for attempt in range(MAX_RETRIES + 1):
            response = requests.get(url, stream=True, timeout=60)
            try:
                response.raise_for_status()
                break
            except requests.exceptions.HTTPError as http_err:
                response.close()
                status = getattr(http_err.response, "status_code", None)
                if status == 429 and attempt < MAX_RETRIES:
                    wait = 2 ** (attempt + 1)
                    say(f"  ⏳ Rate limited, retrying in {wait}s ({attempt + 1}/{MAX_RETRIES})...")
                    time.sleep(wait)
                    continue
                raise

        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0
        started_at = time.monotonic()
        last_update = started_at

        def report(complete: bool = False) -> None:
            elapsed = max(time.monotonic() - started_at, 0.001)
            mb = downloaded / 1_000_000
            rate = mb / elapsed
            if total_size:
                pct = 100 if complete else min(99, int(downloaded * 100 / total_size))
                say(f"📥 {label}: {pct}%|| {mb:.1f}/{total_size / 1_000_000:.1f} MB · {rate:.1f} MB/s")
            elif complete:
                say(f"📥 {label}: 100%|| {mb:.1f} MB received · total size unknown")
            else:
                say(f"📥 {label}: {mb:.1f} MB [total size unknown · {rate:.1f} MB/s]")

        report()
        hasher = hashlib.new(algorithm)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if time.monotonic() - last_update >= 1:
                    report()
                    last_update = time.monotonic()
        response.close()

        if downloaded != size or hasher.hexdigest() != digest.lower():
            say(f"  ❌ {label} failed its integrity check and was discarded")
            debug_log(f"{label} rejected: size or digest mismatch", "tts")
            return False
        temp_path.replace(target)
        report(complete=True)
        debug_log(f"{label} downloaded and verified", "tts")
        return True
    except requests.RequestException as e:
        say(f"  ❌ {label} download failed: {e}")
        debug_log(f"{label} download failed: {type(e).__name__}", "tts")
    except Exception as e:
        say(f"  ❌ {label} could not be saved: {e}")
        debug_log(f"{label} save failed: {type(e).__name__}", "tts")
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            debug_log(f"{label} partial file could not be removed", "tts")
    # Another download may have put a good copy in place meanwhile.
    return _matches(target, size, digest, algorithm)

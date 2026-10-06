"""Paired phones: one-time pairing codes and device tokens, kept as hashes next to the database.

The desktop dialog, the terminal command and the running daemon each use their own ``DeviceStore`` on the
same files, so pairing and revocation work whether or not the daemon runs in the desktop app's process.
See ``remote.spec.md``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..debug import debug_log

DEVICES_FILE = "remote_devices.json"
PAIRING_FILE = "remote_pairing.json"
CODE_TTL_SEC = 300
CODE_DIGITS = 6
MAX_CODE_ATTEMPTS = 5
NAME_MAX_CHARS = 40
DEFAULT_DEVICE_NAME = "Phone"
LAST_SEEN_WRITE_SEC = 300


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: float
    last_seen: float


@dataclass(frozen=True)
class PairingResult:
    token: str
    device_id: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # another process is reading the file on Windows
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        debug_log(f"remote: unreadable {path.name}: {type(exc).__name__}", "remote")
        return None


class DeviceStore:
    """Pairing codes and paired devices in ``directory``. Thread-safe."""

    def __init__(self, directory, clock: Callable[[], float] = time.time) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._lock = threading.RLock()
        self._devices: List[dict] = []
        self._signature = None
        self._seen: Dict[str, float] = {}

    @property
    def devices_path(self) -> Path:
        return self._dir / DEVICES_FILE

    @property
    def pairing_path(self) -> Path:
        return self._dir / PAIRING_FILE

    # -- devices file ----------------------------------------------------------

    def _file_signature(self):
        try:
            st = os.stat(self.devices_path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return None

    def _refresh_locked(self) -> None:
        signature = self._file_signature()
        if signature == self._signature:
            return
        self._signature = signature
        data = _read_json(self.devices_path) if signature is not None else None
        devices = data.get("devices") if data else None
        self._devices = [d for d in devices if isinstance(d, dict) and isinstance(d.get("id"), str)
                         and isinstance(d.get("token_hash"), str)] if isinstance(devices, list) else []

    def _save_locked(self) -> None:
        _atomic_write(self.devices_path, {"devices": self._devices})
        self._signature = self._file_signature()

    def _device(self, raw: dict) -> Device:
        last_seen = max(float(raw.get("last_seen") or 0.0), self._seen.get(raw["id"], 0.0))
        return Device(id=raw["id"], name=str(raw.get("name") or DEFAULT_DEVICE_NAME),
                      created_at=float(raw.get("created_at") or 0.0), last_seen=last_seen)

    def devices(self) -> List[Device]:
        with self._lock:
            self._refresh_locked()
            return [self._device(d) for d in self._devices]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            self._refresh_locked()
            kept = [d for d in self._devices if d["id"] != device_id]
            if len(kept) == len(self._devices):
                return False
            self._devices = kept
            self._seen.pop(device_id, None)
            self._save_locked()
        debug_log("remote: device revoked", "remote")
        return True

    def authenticate(self, token) -> Optional[Device]:
        """The device ``token`` belongs to, or ``None``. Records the device as seen."""
        if not isinstance(token, str) or not token:
            return None
        token_hash = _sha256(token)
        with self._lock:
            self._refresh_locked()
            for raw in self._devices:
                if hmac.compare_digest(raw["token_hash"], token_hash):
                    now = self._clock()
                    self._seen[raw["id"]] = now
                    if now - float(raw.get("last_seen") or 0.0) >= LAST_SEEN_WRITE_SEC:
                        raw["last_seen"] = now
                        try:
                            self._save_locked()
                        except OSError as exc:
                            debug_log(f"remote: last-seen not saved: {type(exc).__name__}", "remote")
                    return self._device(raw)
        return None

    # -- pairing -----------------------------------------------------------------

    def start_pairing(self, ttl_sec: float = CODE_TTL_SEC) -> str:
        """Create a new six-digit pairing code, replacing any other. Returns the code."""
        code = "".join(str(secrets.randbelow(10)) for _ in range(CODE_DIGITS))
        salt = secrets.token_hex(16)
        with self._lock:
            _atomic_write(self.pairing_path, {"salt": salt, "code_hash": _sha256(salt + code),
                                              "expires_at": self._clock() + ttl_sec, "attempts": 0})
        debug_log("remote: pairing code created", "remote")
        return code

    def _active_pairing_locked(self) -> Optional[dict]:
        data = _read_json(self.pairing_path)
        if not data or not all(k in data for k in ("salt", "code_hash", "expires_at")):
            return None
        if self._clock() > float(data["expires_at"]) or int(data.get("attempts", 0)) >= MAX_CODE_ATTEMPTS:
            self._end_pairing_locked()
            return None
        return data

    def _end_pairing_locked(self) -> None:
        try:
            os.remove(self.pairing_path)
        except FileNotFoundError:
            pass

    def pairing_active(self) -> bool:
        with self._lock:
            return self._active_pairing_locked() is not None

    def pairing_seconds_left(self) -> float:
        with self._lock:
            data = self._active_pairing_locked()
            return max(0.0, float(data["expires_at"]) - self._clock()) if data else 0.0

    def cancel_pairing(self) -> None:
        with self._lock:
            self._end_pairing_locked()

    def complete_pairing(self, code, name) -> Optional[PairingResult]:
        """Exchange a valid pairing code for a new device token. A code works once."""
        with self._lock:
            data = self._active_pairing_locked()
            if data is None:
                debug_log("remote: pairing refused, no active code", "remote")
                return None
            valid_shape = isinstance(code, str) and len(code) == CODE_DIGITS and code.isdigit()
            if not valid_shape or not hmac.compare_digest(_sha256(data["salt"] + code), data["code_hash"]):
                data["attempts"] = int(data.get("attempts", 0)) + 1
                if data["attempts"] >= MAX_CODE_ATTEMPTS:
                    self._end_pairing_locked()
                else:
                    _atomic_write(self.pairing_path, data)
                debug_log(f"remote: pairing refused, wrong code ({data['attempts']}/{MAX_CODE_ATTEMPTS})", "remote")
                return None
            self._end_pairing_locked()
            token = secrets.token_urlsafe(32)
            device_id = secrets.token_hex(8)
            clean_name = (name.strip() if isinstance(name, str) else "")[:NAME_MAX_CHARS].strip()
            now = self._clock()
            self._refresh_locked()
            self._devices.append({"id": device_id, "name": clean_name or DEFAULT_DEVICE_NAME,
                                  "token_hash": _sha256(token), "created_at": now, "last_seen": now})
            self._save_locked()
        debug_log("remote: device paired", "remote")
        return PairingResult(token=token, device_id=device_id)

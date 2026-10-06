"""API keys in the operating system's credential store, never in ``config.json``.

On Windows this is Windows Credential Manager, through ``keyring``. Each key is stored under the
service ``jarvis`` with the setting's name as the user name. Values are never logged or printed.
When no store is available, reads return empty and writes report failure, so callers can keep a
key where it was rather than lose it.
"""
from __future__ import annotations

from typing import Any, Optional

from .debug import debug_log

SERVICE = "jarvis"
SECRET_KEYS = ("llm_api_key", "embedding_api_key", "brave_search_api_key")

_backend: Any = None


def set_backend(backend: Any) -> None:
    """Use ``backend`` (anything with keyring's get/set/delete_password) instead of ``keyring``."""
    global _backend
    _backend = backend


def _store() -> Any:
    if _backend is not None:
        return _backend
    import keyring
    return keyring


def _check(key: str) -> None:
    if key not in SECRET_KEYS:
        raise ValueError(f"not a secret setting: {key}")


def get_secret(key: str) -> str:
    """The stored value, or ``""`` when absent or when no store is available."""
    _check(key)
    try:
        return _store().get_password(SERVICE, key) or ""
    except Exception as exc:
        debug_log(f"credential store read failed for {key}: {type(exc).__name__}", "config")
        return ""


def has_secret(key: str) -> bool:
    return bool(get_secret(key))


def set_secret(key: str, value: str) -> bool:
    """Store ``value``; an empty value removes the key. Returns whether it worked."""
    _check(key)
    if not value:
        return delete_secret(key)
    try:
        _store().set_password(SERVICE, key, value)
        debug_log(f"credential stored for {key}", "config")
        return True
    except Exception as exc:
        debug_log(f"credential store write failed for {key}: {type(exc).__name__}", "config")
        return False


def delete_secret(key: str) -> bool:
    """Remove the key. Removing a key that is not stored succeeds."""
    _check(key)
    try:
        if not _store().get_password(SERVICE, key):
            return True
        _store().delete_password(SERVICE, key)
        debug_log(f"credential removed for {key}", "config")
        return True
    except Exception as exc:
        debug_log(f"credential store delete failed for {key}: {type(exc).__name__}", "config")
        return False


def available() -> bool:
    """Whether a credential store can be read."""
    try:
        _store().get_password(SERVICE, "availability-check")
        return True
    except Exception:
        return False


def resolve(key: str, config_value: Optional[str]) -> str:
    """A key's effective value: a plaintext value still in the config wins (the store was unavailable
    when it should have moved), otherwise the stored one."""
    text = str(config_value or "").strip()
    return text or get_secret(key)


def drop_stored_plaintext(config: dict, key: str) -> None:
    """Remove ``key`` from a config dict about to be written, unless the file is its only copy.

    A plaintext key stays when the store does not hold it (no store, or a failed move), so saving
    settings never loses it; ``resolve`` reads it from the config until it can move."""
    _check(key)
    if has_secret(key) or not str(config.get(key) or "").strip():
        config.pop(key, None)

"""API keys in the operating system's credential store, never in ``config.json``.

On Windows this is Windows Credential Manager, through ``keyring``. Each key is stored under the
service ``jarvis`` with the setting's name as the user name. Per-account secrets (a calendar's secret
address, an email password, a sign-in token) use ``account_key(kind, name)``, such as
``calendar_url:Work``. A value longer than the store accepts is split over numbered entries
(``<key>#1``, ``<key>#2``, ...). Values are never logged or printed.
When no store is available, reads return empty and writes report failure, so callers can keep a
key where it was rather than lose it.
"""
from __future__ import annotations

from typing import Any, Optional

from .debug import debug_log

SERVICE = "jarvis"
SECRET_KEYS = ("llm_api_key", "embedding_api_key", "brave_search_api_key")
# Per-account secrets: ``<kind>:<account name>``.
ACCOUNT_SECRET_KINDS = ("calendar_url", "email_password", "email_graph_token")
# Windows Credential Manager holds at most 2560 bytes (1280 UTF-16 characters) per entry.
_CHUNK_CHARS = 1000
_CHUNK_MARKER = "\x00chunks:"

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


def account_key(kind: str, name: str) -> str:
    """The credential name of one account's secret, such as ``calendar_url:Work``."""
    if kind not in ACCOUNT_SECRET_KINDS:
        raise ValueError(f"not an account secret kind: {kind}")
    name = str(name or "").strip()
    if not name:
        raise ValueError("an account secret needs an account name")
    return f"{kind}:{name}"


def _check(key: str) -> None:
    if key in SECRET_KEYS:
        return
    kind, _, name = key.partition(":")
    if kind not in ACCOUNT_SECRET_KINDS or not name.strip():
        raise ValueError(f"not a secret setting: {key}")


def _chunk_count(value: Optional[str]) -> int:
    if value and value.startswith(_CHUNK_MARKER):
        try:
            return int(value[len(_CHUNK_MARKER):])
        except ValueError:
            return 0
    return 0


def get_secret(key: str) -> str:
    """The stored value, or ``""`` when absent or when no store is available."""
    _check(key)
    try:
        value = _store().get_password(SERVICE, key) or ""
        chunks = _chunk_count(value)
        if chunks:
            return "".join(_store().get_password(SERVICE, f"{key}#{n}") or "" for n in range(1, chunks + 1))
        return value
    except Exception as exc:
        debug_log(f"credential store read failed for {_label(key)}: {type(exc).__name__}", "config")
        return ""


def has_secret(key: str) -> bool:
    return bool(get_secret(key))


def set_secret(key: str, value: str) -> bool:
    """Store ``value``; an empty value removes the key. Returns whether it worked."""
    _check(key)
    if not value:
        return delete_secret(key)
    try:
        _delete_chunks(key)
        if len(value) <= _CHUNK_CHARS:
            _store().set_password(SERVICE, key, value)
        else:
            pieces = [value[i:i + _CHUNK_CHARS] for i in range(0, len(value), _CHUNK_CHARS)]
            for number, piece in enumerate(pieces, start=1):
                _store().set_password(SERVICE, f"{key}#{number}", piece)
            _store().set_password(SERVICE, key, f"{_CHUNK_MARKER}{len(pieces)}")
        debug_log(f"credential stored for {_label(key)}", "config")
        return True
    except Exception as exc:
        debug_log(f"credential store write failed for {_label(key)}: {type(exc).__name__}", "config")
        return False


def delete_secret(key: str) -> bool:
    """Remove the key. Removing a key that is not stored succeeds."""
    _check(key)
    try:
        _delete_chunks(key)
        if not _store().get_password(SERVICE, key):
            return True
        _store().delete_password(SERVICE, key)
        debug_log(f"credential removed for {_label(key)}", "config")
        return True
    except Exception as exc:
        debug_log(f"credential store delete failed for {_label(key)}: {type(exc).__name__}", "config")
        return False


def _delete_chunks(key: str) -> None:
    """Remove the numbered entries of a split value (the main entry stays for the caller)."""
    chunks = _chunk_count(_store().get_password(SERVICE, key))
    for number in range(1, chunks + 1):
        if _store().get_password(SERVICE, f"{key}#{number}") is not None:
            _store().delete_password(SERVICE, f"{key}#{number}")


def _label(key: str) -> str:
    """The key as logs show it: an account secret's kind only, never the account name."""
    return key.partition(":")[0] if ":" in key else key


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

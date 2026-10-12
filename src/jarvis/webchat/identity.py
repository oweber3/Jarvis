"""Proof that the web chat page on the loopback port is Jarvis's own server, not another program.

Each server run holds a random token known only to the daemon and the desktop app. The desktop app
sends a fresh nonce and the server answers with an HMAC of it, so the token itself never travels.
See ``webchat.spec.md`` (Desktop window).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

_NONCE = re.compile(r"^[0-9a-f]{32,128}$")


def new_token() -> str:
    return secrets.token_hex(32)


def new_nonce() -> str:
    return secrets.token_hex(16)


def is_valid_nonce(nonce: object) -> bool:
    return isinstance(nonce, str) and _NONCE.match(nonce) is not None


def proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode("ascii"), nonce.encode("ascii"), hashlib.sha256).hexdigest()


def is_valid_proof(token: str, nonce: str, answer: object) -> bool:
    return isinstance(answer, str) and hmac.compare_digest(proof(token, nonce), answer)

"""A throwaway self-signed certificate for HTTPS tests, made with the ``openssl`` program at test time.

Nothing is committed: the key exists only in the test's temporary folder. Tests that need it skip when
``openssl`` is not installed.
"""
from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Tuple

import pytest


def make_certificate(folder: Path, name: str = "localhost") -> Tuple[Path, Path]:
    """Write ``<name>.crt`` and ``<name>.key`` (valid for localhost and 127.0.0.1) and return their paths."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    cert, key = folder / f"{name}.crt", folder / f"{name}.key"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True, timeout=60)
    return cert, key


def client_context(cert: Path) -> ssl.SSLContext:
    """A client that trusts exactly ``cert``."""
    return ssl.create_default_context(cafile=str(cert))


def make_encrypted_certificate(folder: Path) -> Tuple[Path, Path]:
    """Like ``make_certificate`` but the key is protected by a passphrase (which nothing here supplies)."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    cert, key = folder / "locked.crt", folder / "locked.key"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=localhost", "-passout", "pass:secret"],
        check=True, capture_output=True, timeout=60)
    return cert, key

"""A certificate for phone access from the locally installed Tailscale (``python -m jarvis.remote cert``).

Jarvis only runs the user's own ``tailscale`` program; nothing else leaves the PC. Every failure raises
``CertProblem`` with the exact steps the owner has to take. See ``remote.spec.md``, HTTPS.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from ..debug import debug_log

CERT_FILE = "remote_access.crt"
KEY_FILE = "remote_access.key"
COMMAND_TIMEOUT_SEC = 90

_INSTALLED = (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe")

Runner = Callable[[Sequence[str], float], Tuple[int, str, str]]


class CertProblem(Exception):
    """The certificate could not be issued; ``lines`` say what the owner needs to do."""

    def __init__(self, *lines: str) -> None:
        super().__init__(lines[0] if lines else "")
        self.lines: List[str] = list(lines)


@dataclass(frozen=True)
class IssuedCertificate:
    host: str
    cert: Path
    key: Path


def find_tailscale(which: Callable[[str], Optional[str]] = shutil.which,
                   exists: Callable[[str], bool] = os.path.exists) -> Optional[str]:
    """The ``tailscale`` program: on the path, else where its Windows installer puts it."""
    on_path = which("tailscale")
    if on_path:
        return on_path
    return next((path for path in _INSTALLED if exists(path)), None)


def _run(args: Sequence[str], timeout: float) -> Tuple[int, str, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        done = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, creationflags=flags,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", ""
    return done.returncode, done.stdout or "", done.stderr or ""


def _read_status(program: str, run: Runner) -> dict:
    code, out, _ = run([program, "status", "--json"], 20)
    try:
        document = json.loads(out) if code == 0 else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise CertProblem("Tailscale did not answer.",
                          "Start the Tailscale app and sign in, then run this command again.")
    return document


def issue_certificate(folder: Path, *, run: Runner = _run,
                      find: Callable[[], Optional[str]] = find_tailscale) -> IssuedCertificate:
    """Ask the local Tailscale for this PC's certificate and write it into ``folder``."""
    program = find()
    if program is None:
        raise CertProblem("Tailscale is not installed on this PC.",
                          "Install it from tailscale.com/download, sign in, then run this command again.")
    document = _read_status(program, run)
    state = str(document.get("BackendState") or "unknown")
    if state != "Running":
        raise CertProblem(f"Tailscale is not connected (state: {state}).",
                          "Open the Tailscale app, sign in and connect, then run this command again.")
    host = str((document.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if not host:
        raise CertProblem("This PC has no Tailscale name.",
                          "Turn on MagicDNS on the DNS page of the Tailscale admin console, then run this "
                          "command again.")
    domains = [str(name).rstrip(".").lower() for name in (document.get("CertDomains") or [])]
    if host not in domains:
        raise CertProblem("HTTPS certificates are not enabled for your tailnet.",
                          "Open the Tailscale admin console (login.tailscale.com/admin/dns), scroll to "
                          "HTTPS Certificates, press Enable HTTPS, then run this command again.")
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder / CERT_FILE, folder / KEY_FILE
    code, _, err = run([program, "cert", "--cert-file", str(cert), "--key-file", str(key), host],
                       COMMAND_TIMEOUT_SEC)
    if code != 0:
        first = next((line.strip() for line in err.splitlines() if line.strip()), "no message")
        raise CertProblem("Tailscale could not issue the certificate:", f"   {first}")
    if not (cert.is_file() and key.is_file() and cert.stat().st_size and key.stat().st_size):
        raise CertProblem("Tailscale reported success but wrote no certificate.",
                          "Run `tailscale cert " + host + "` yourself to see why.")
    debug_log("remote: HTTPS certificate issued by the local Tailscale", "remote")
    return IssuedCertificate(host, cert, key)


def remember(issued: IssuedCertificate) -> bool:
    """Write the three HTTPS settings to ``config.json`` (nothing else changes). True on success."""
    from ..config import update_config_values
    return update_config_values({"remote_access_tls_cert": str(issued.cert),
                                 "remote_access_tls_key": str(issued.key),
                                 "remote_access_tls_host": issued.host})

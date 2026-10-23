"""Phone access from a terminal: ``python -m jarvis.remote pair | devices | revoke <id> | cert``."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from .pairing import CODE_TTL_SEC, DeviceStore
from .runtime import devices_directory, phone_links
from .tailscale import CertProblem, IssuedCertificate, issue_certificate, remember


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "never"


def _cert(cfg, issue: Callable[[Path], IssuedCertificate]) -> int:
    from ..config import _config_path
    print("🔐 Getting an HTTPS certificate for phone access")
    print("   🔎 Asking the local Tailscale for this PC's name and certificate...")
    try:
        issued = issue(_config_path().parent)
    except CertProblem as problem:
        print(f"   ❌ {problem.lines[0]}")
        for line in problem.lines[1:]:
            print(f"      💡 {line}")
        return 1
    if not remember(issued):
        print("   ❌ The certificate was saved but config.json could not be updated.")
        print(f"      💡 Set remote_access_tls_cert, remote_access_tls_key and remote_access_tls_host "
              f"({issued.host}) in Settings > Extras > Phone access.")
        return 1
    print(f"   ✅ Certificate saved for {issued.host}")
    print("   ⚙️ The three HTTPS settings are saved in config.json")
    print(f"   🔗 Open on your phone: https://{issued.host}:{cfg.remote_access_port}/")
    print("   ⏳ The certificate lasts about 90 days. Run this command again to renew it; no restart is needed.")
    print("   ♻️ If phone access is already running, restart Jarvis once so it serves HTTPS.")
    return 0


def main(argv=None, *, issue: Callable[[Path], IssuedCertificate] = issue_certificate) -> int:
    from ..config import load_settings
    for stream in (sys.stdout, sys.stderr):  # a Windows console may not default to UTF-8, and lines start with emoji
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    cfg = load_settings()
    store = DeviceStore(devices_directory(cfg))
    command = args[0] if args else "devices"

    if command == "pair":
        code = store.start_pairing()
        print("📱 Pair a phone with Jarvis")
        if not cfg.remote_access_enabled:
            print("   ⚠️ Phone access is off. Turn it on in Settings > Extras > Phone access and restart Jarvis.")
        print("   🔗 Open one of these on the phone:")
        for link in phone_links(cfg):
            print(f"      {link}")
        if not cfg.remote_access_tls_cert or not cfg.remote_access_tls_key:
            print("   🎙️ Talking to Jarvis from the phone needs HTTPS: run python -m jarvis.remote cert")
        print(f"   🔑 Pairing code: {code}")
        print(f"   ⏳ Valid for {CODE_TTL_SEC // 60} minutes, once.")
        return 0

    if command == "devices":
        devices = store.devices()
        print(f"📱 Paired phones: {len(devices)}")
        for device in devices:
            print(f"   📲 {device.name}  (id {device.id})")
            print(f"      ➕ added {_when(device.created_at)}   👀 last seen {_when(device.last_seen)}")
        return 0

    if command == "revoke" and len(args) == 2:
        if store.revoke(args[1]):
            print(f"🗑️ Removed phone {args[1]}")
            return 0
        print(f"❌ No paired phone with id {args[1]}")
        return 1

    if command == "cert":
        return _cert(cfg, issue)

    print("ℹ️ Usage: python -m jarvis.remote pair | devices | revoke <id> | cert")
    return 2


if __name__ == "__main__":
    sys.exit(main())

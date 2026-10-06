"""Pair and manage phones from a terminal: ``python -m jarvis.remote pair | devices | revoke <id>``."""
from __future__ import annotations

import sys
from datetime import datetime

from .pairing import CODE_TTL_SEC, DeviceStore
from .runtime import devices_directory, phone_links


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "never"


def main(argv=None) -> int:
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
            print("   ⚠️ Phone access is off. Turn it on in Settings > Phone Access and restart Jarvis.")
        print("   🔗 Open one of these on the phone:")
        for link in phone_links(cfg.remote_access_port):
            print(f"      {link}")
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

    print("ℹ️ Usage: python -m jarvis.remote pair | devices | revoke <id>")
    return 2


if __name__ == "__main__":
    sys.exit(main())

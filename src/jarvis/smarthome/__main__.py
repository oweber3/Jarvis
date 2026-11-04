"""``python -m jarvis.smarthome test|devices``: check the connections and list devices.

Never prints tokens or passwords, and no address beyond what the user typed into the settings.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from ..debug import debug_log
from . import service

WAIT_FOR_DEVICES_SEC = 10.0


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _test(cfg) -> int:
    from .. import credentials
    from . import home_assistant, mqtt
    failures = 0
    print("🏠 Smart home connection test")
    if not cfg.smarthome_enabled:
        print("   ⚠️ Smart home is off (smarthome_enabled). Testing the saved connections anyway.")
    if cfg.smarthome_ha_url:
        try:
            count = home_assistant.test_connection(cfg.smarthome_ha_url, credentials.get_secret(service.HA_TOKEN_KEY),
                                                   verify_tls=cfg.smarthome_ha_verify_tls)
            print(f"   ✅ Home Assistant: {_count(count, 'exposed device')}")
        except home_assistant.HomeAssistantError as exc:
            failures += 1
            print(f"   ❌ Home Assistant: {exc}")
    else:
        print("   ➖ Home Assistant: not set up")
    if cfg.smarthome_mqtt_host:
        try:
            count = mqtt.test_connection(cfg.smarthome_mqtt_host, cfg.smarthome_mqtt_port,
                                         username=cfg.smarthome_mqtt_username,
                                         password=credentials.get_secret(service.MQTT_PASSWORD_KEY),
                                         tls=cfg.smarthome_mqtt_tls,
                                         discovery_prefix=cfg.smarthome_mqtt_discovery_prefix)
            print(f"   ✅ MQTT: {_count(count, 'discovered device')}")
        except mqtt.MqttError as exc:
            failures += 1
            print(f"   ❌ MQTT: {exc}")
    else:
        print("   ➖ MQTT: not set up")
    return 1 if failures else 0


def _devices(cfg) -> int:
    if not cfg.smarthome_enabled:
        print("🏠 Smart home is off. Turn it on in Settings > Smart Home (smarthome_enabled).")
        return 1
    home = service.build(cfg)
    if home is None or not home.connectors:
        print("🏠 No Home Assistant or MQTT connection is set up.")
        return 1
    home.start()
    try:
        deadline = time.monotonic() + WAIT_FOR_DEVICES_SEC
        while time.monotonic() < deadline and not all(
                getattr(c, "connected", False) or getattr(c, "status", "") in ("auth", "refused")
                for c in home.connectors.values()):
            time.sleep(0.1)
        time.sleep(1.0)  # retained MQTT announcements arrive just after connecting
        for source, status in home.status().items():
            if status != "connected":
                print(f"⚠️ {'Home Assistant' if source == 'ha' else 'MQTT'} is not connected ({status}).")
        devices = home.registry.devices()
        print(f"🏠 {_count(len(devices), 'device')}")
        for area in sorted({d.area for d in devices}, key=str.casefold):
            print(f"   📍 {area or 'No room'}")
            for dev in sorted((d for d in devices if d.area == area), key=lambda d: d.name.casefold()):
                print(f"      • {dev.name}  ({dev.kind}, {dev.source}): {dev.state or 'unknown'}")
    finally:
        home.stop()
    return 0


def _configure_console() -> None:
    # A Windows console on a legacy code page cannot print the emoji lines.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(prog="python -m jarvis.smarthome", description="Check Jarvis's smart home.")
    parser.add_argument("command", choices=("test", "devices"))
    args = parser.parse_args(argv)
    from ..config import load_settings
    cfg = load_settings()
    debug_log(f"smarthome command line: {args.command}", "smarthome")
    return _test(cfg) if args.command == "test" else _devices(cfg)


if __name__ == "__main__":
    sys.exit(main())

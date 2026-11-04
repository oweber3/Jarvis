"""The running smart home: one registry and the connectors that fill it, started from the settings.

Nothing starts while ``smarthome_enabled`` is false. A refused address leaves its connector off with a
console warning. Secrets are read from the credential store here and handed only to the connectors.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from ..debug import debug_log
from .model import Command, Device, Outcome, Registry

HA_TOKEN_KEY = "smarthome.ha_token"
MQTT_PASSWORD_KEY = "smarthome.mqtt_password"
_TAG = "smarthome"


class SmartHomeError(Exception):
    """A command did not happen; the message says why, without addresses or secrets."""


class SmartHome:
    def __init__(self, registry: Optional[Registry] = None, connectors: Optional[Dict[str, Any]] = None):
        self.registry = registry or Registry()
        self.connectors: Dict[str, Any] = dict(connectors or {})

    def start(self) -> None:
        for connector in self.connectors.values():
            connector.start()

    def stop(self) -> None:
        for connector in self.connectors.values():
            try:
                connector.stop()
            except Exception as exc:  # noqa: BLE001 - stopping one never stops the others
                debug_log(f"smart home connector stop failed ({type(exc).__name__})", _TAG)

    def status(self) -> Dict[str, str]:
        return {source: getattr(c, "status", "unknown") for source, c in self.connectors.items()}

    def send(self, device: Device, command: Command) -> Outcome:
        connector = self.connectors.get(device.source)
        if connector is None:
            raise SmartHomeError("That device's hub is not connected.")
        from .home_assistant import HomeAssistantError
        from .mqtt import MqttError
        try:
            return connector.send(device, command)
        except (HomeAssistantError, MqttError) as exc:
            raise SmartHomeError(str(exc)) from None


_lock = threading.Lock()
_home: Optional[SmartHome] = None


def get_home() -> Optional[SmartHome]:
    return _home


def set_home(home: Optional[SmartHome]) -> None:
    """Replace the running smart home (stopping the old one)."""
    global _home
    with _lock:
        old, _home = _home, home
    if old is not None and old is not home:
        old.stop()


def build(cfg: Any, *, warn=print) -> Optional[SmartHome]:
    """The smart home the settings describe, not yet started, or None while it is off."""
    if getattr(cfg, "smarthome_enabled", False) is not True:
        return None
    from .. import credentials
    from . import network
    registry = Registry(hidden=getattr(cfg, "smarthome_mqtt_hidden", []) or [])
    connectors: Dict[str, Any] = {}
    url = (getattr(cfg, "smarthome_ha_url", "") or "").strip()
    if url:
        token = credentials.get_secret(HA_TOKEN_KEY)
        try:
            network.validate_endpoint(url, default_port=8123)
        except network.SmartHomeAddressError:
            debug_log("Home Assistant address refused: not on the local network", _TAG)
            warn("  ⚠️ Smart home: the Home Assistant address was refused. It must be on your home network.")
        else:
            if not token:
                warn("  ⚠️ Smart home: no Home Assistant access token is saved. Add one in Settings > Smart Home.")
            else:
                from .home_assistant import HomeAssistantConnector
                connectors["ha"] = HomeAssistantConnector(
                    registry, url, token, verify_tls=getattr(cfg, "smarthome_ha_verify_tls", True) is not False)
    host = (getattr(cfg, "smarthome_mqtt_host", "") or "").strip()
    if host:
        port = int(getattr(cfg, "smarthome_mqtt_port", 1883) or 1883)
        try:
            network.validate_endpoint(host, default_port=port, schemes=("mqtt",))
        except network.SmartHomeAddressError:
            debug_log("MQTT broker address refused: not on the local network", _TAG)
            warn("  ⚠️ Smart home: the MQTT broker address was refused. It must be on your home network.")
        else:
            from .mqtt import MqttConnector
            connectors["mqtt"] = MqttConnector(
                registry, host, port, username=getattr(cfg, "smarthome_mqtt_username", "") or "",
                password=credentials.get_secret(MQTT_PASSWORD_KEY), tls=getattr(cfg, "smarthome_mqtt_tls", False),
                discovery_prefix=getattr(cfg, "smarthome_mqtt_discovery_prefix", "homeassistant"))
    debug_log(f"smart home configured: {len(connectors)} connectors", _TAG)
    return SmartHome(registry, connectors)


def configure(cfg: Any, *, start: bool = True, warn=print) -> Optional[SmartHome]:
    """Build the smart home from the settings and, with ``start``, start its connectors."""
    home = build(cfg, warn=warn)
    set_home(home)
    if home is not None and start:
        home.start()
    return home

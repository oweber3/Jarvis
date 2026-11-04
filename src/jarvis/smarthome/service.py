"""The running smart home: one registry and the connectors that fill it, started from the settings.

Nothing starts while ``smarthome_enabled`` is false. A refused address leaves its connector off with a
console warning. Secrets are read from the credential store here and handed only to the connectors.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from ..debug import debug_log
from .model import Command, Device, Outcome, Registry

_LABELS = {"ha": "Home Assistant", "mqtt": "MQTT"}
_LIBRARIES = {"ha": "websocket-client", "mqtt": "paho-mqtt"}
HA_TOKEN_KEY = "smarthome.ha_token"
MQTT_PASSWORD_KEY = "smarthome.mqtt_password"
_TAG = "smarthome"


class SmartHomeError(Exception):
    """A command did not happen. ``summary`` is Jarvis's own words, without addresses or secrets; ``detail``
    is the hub's reason when it gave one (untrusted text)."""

    def __init__(self, summary: str, detail: str = ""):
        super().__init__(f"{summary} {detail}".strip() if detail else summary)
        self.summary = summary
        self.detail = detail


class SmartHome:
    def __init__(self, registry: Optional[Registry] = None, connectors: Optional[Dict[str, Any]] = None):
        self.registry = registry or Registry()
        self.connectors: Dict[str, Any] = dict(connectors or {})

    def start(self, *, warn=print) -> None:
        """Start every connector; one that cannot start is reported and the others start anyway."""
        for source, connector in self.connectors.items():
            label = _LABELS.get(source, source)
            try:
                connector.start()
            except ImportError:
                debug_log(f"smart home {source} connector not started: library missing", _TAG)
                warn(f"  ⚠️ Smart home: {label} needs the {_LIBRARIES.get(source, 'missing')} package. "
                     f"Install it with: pip install {_LIBRARIES.get(source, '')}".rstrip())
            except Exception as exc:  # noqa: BLE001 - a bad hub never stops Jarvis starting
                debug_log(f"smart home {source} connector not started ({type(exc).__name__})", _TAG)
                warn(f"  ⚠️ Smart home: {label} could not start: {exc}")

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
        from .home_assistant import HomeAssistantCommandRejected, HomeAssistantError
        from .mqtt import MqttError
        try:
            return connector.send(device, command)
        except HomeAssistantCommandRejected as exc:
            raise SmartHomeError(exc.summary, exc.detail) from None
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
            # Syntax and literal addresses only: names are resolved by the connector, which keeps trying
            # while the network or the hub is still coming up.
            network.validate_endpoint(url, default_port=8123, resolve=False)
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
            network.validate_endpoint(host, default_port=port, schemes=("mqtt",), resolve=False)
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
        home.start(warn=warn)
    return home

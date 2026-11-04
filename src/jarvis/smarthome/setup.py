"""What the Settings > Smart Home page does, without Qt: test a connection, store its secret only after
the test passes, refuse to save an untested address, and read the device list.

Secrets go straight to the credential store and are never returned, shown or logged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import credentials
from ..debug import debug_log
from . import home_assistant, mqtt
from .service import HA_TOKEN_KEY, MQTT_PASSWORD_KEY, SmartHome

GUIDE = "docs/SMART_HOME_SETUP.md"
DEVICE_WAIT_SEC = 10.0
_TAG = "smarthome"


@dataclass(frozen=True)
class TestResult:
    ok: bool
    message: str
    count: int = 0


def ha_key(url: str, verify_tls: bool) -> Tuple[Any, ...]:
    return ("ha", (url or "").strip(), bool(verify_tls))


def mqtt_key(host: str, port: int, username: str, tls: bool, prefix: str) -> Tuple[Any, ...]:
    return ("mqtt", (host or "").strip(), int(port or 0), (username or "").strip(), bool(tls),
            (prefix or "").strip().strip("/"))


def test_home_assistant(url: str, typed_token: str, verify_tls: bool = True) -> TestResult:
    """Test with the pasted token (or the saved one when nothing is pasted); a pasted token is saved only
    when the test passes."""
    token = (typed_token or "").strip() or credentials.get_secret(HA_TOKEN_KEY)
    if not (url or "").strip():
        return TestResult(False, "Enter the Home Assistant address first, such as http://192.168.1.50:8123.")
    if not token:
        return TestResult(False, "Paste a Home Assistant long-lived access token first.")
    try:
        count = home_assistant.test_connection(url, token, verify_tls=verify_tls)
    except home_assistant.HomeAssistantError as exc:
        debug_log(f"Home Assistant settings test failed ({type(exc).__name__})", _TAG)
        return TestResult(False, str(exc))
    if (typed_token or "").strip() and not credentials.set_secret(HA_TOKEN_KEY, token):
        return TestResult(False, "The connection works, but the token could not be saved to Windows Credential "
                                 "Manager, so it was not saved.")
    return TestResult(True, f"Connected. Jarvis can see {count} exposed devices.", count)


def test_mqtt(host: str, port: int, username: str, typed_password: str, tls: bool = False,
              prefix: str = "homeassistant") -> TestResult:
    """Test with the typed password (or the saved one); a typed password is saved only when the test passes."""
    if not (host or "").strip():
        return TestResult(False, "Enter the MQTT broker's address first.")
    password = typed_password or credentials.get_secret(MQTT_PASSWORD_KEY)
    try:
        count = mqtt.test_connection(host, int(port or 1883), username=username or "", password=password or "",
                                     tls=tls, discovery_prefix=prefix or "homeassistant")
    except mqtt.MqttError as exc:
        debug_log(f"MQTT settings test failed ({type(exc).__name__})", _TAG)
        return TestResult(False, str(exc))
    if typed_password and not credentials.set_secret(MQTT_PASSWORD_KEY, typed_password):
        return TestResult(False, "The connection works, but the password could not be saved to Windows Credential "
                                 "Manager, so it was not saved.")
    return TestResult(True, f"Connected. Jarvis found {count} devices.", count)


@dataclass
class SaveGate:
    """Connection settings are saved only once a test with exactly those values has passed."""
    saved: Dict[str, Tuple[Any, ...]] = field(default_factory=dict)    # as the file holds them now
    passed: set = field(default_factory=set)

    def passed_with(self, key: Tuple[Any, ...]) -> None:
        self.passed.add(key)

    def problem(self, ha: Tuple[Any, ...], broker: Tuple[Any, ...]) -> Optional[str]:
        """Why Save must refuse, or None. An empty address needs no test (it turns that hub off)."""
        if ha != self.saved.get("ha") and ha[1] and ha not in self.passed:
            return "Test the Home Assistant connection with these settings before saving."
        if broker != self.saved.get("mqtt") and broker[1] and broker not in self.passed:
            return "Test the MQTT connection with these settings before saving."
        return None


@dataclass(frozen=True)
class DeviceRow:
    id: str
    name: str
    area: str
    kind: str
    source: str
    state: str


def read_devices(home: Optional[SmartHome], *, wait: float = DEVICE_WAIT_SEC,
                 sleep: Callable[[float], None] = time.sleep) -> Tuple[List[DeviceRow], List[str]]:
    """Start ``home``, wait for its connectors, and return the device rows and any connection problems.
    Hidden MQTT devices are listed too (so they can be shown again)."""
    if home is None or not home.connectors:
        return [], ["No Home Assistant or MQTT connection is set up."]
    hidden = set(home.registry.hidden)
    home.registry.set_hidden(())
    home.start()
    try:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not all(
                getattr(c, "connected", False) or getattr(c, "status", "") in ("auth", "refused")
                for c in home.connectors.values()):
            sleep(0.1)
        sleep(1.0)
        problems = [f"{'Home Assistant' if source == 'ha' else 'MQTT'} is not connected ({status})."
                    for source, status in home.status().items() if status != "connected"]
        rows = [DeviceRow(d.id, d.name, d.area, d.kind, d.source, d.state) for d in home.registry.devices()]
    finally:
        home.stop()
        home.registry.set_hidden(hidden)
    return sorted(rows, key=lambda r: (r.area.casefold(), r.name.casefold())), problems


def apply_device_choices(config: Dict[str, Any], hidden: List[str], confirm: List[str]) -> None:
    """Write the device list's choices into a config dict about to be saved (only non-empty lists)."""
    for key, values in (("smarthome_mqtt_hidden", hidden), ("smarthome_confirm_devices", confirm)):
        cleaned = sorted({v for v in values if isinstance(v, str) and v.strip()})
        if cleaned:
            config[key] = cleaned
        else:
            config.pop(key, None)

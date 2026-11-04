"""From settings to a confirmed command through the real connectors and loopback fakes; secrets stay put."""
import json
import time
from types import SimpleNamespace

import pytest

from fake_home_assistant import FakeHomeAssistant, entity
from fake_mqtt import FakeBroker
from jarvis import credentials
from jarvis.smarthome import service
from jarvis.tools.base import ToolContext

TOKEN = "very-secret-token-123"


def wait_for(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def cfg(**values):
    base = dict(smarthome_enabled=True, smarthome_ha_url="", smarthome_ha_verify_tls=True, smarthome_mqtt_host="",
                smarthome_mqtt_port=1883, smarthome_mqtt_username="", smarthome_mqtt_tls=False,
                smarthome_mqtt_discovery_prefix="homeassistant", smarthome_mqtt_hidden=[],
                smarthome_confirm_devices=[], smarthome_share_with_cloud=False)
    base.update(values)
    return SimpleNamespace(**base)


def run(tool, args, config):
    printed = []
    context = ToolContext(db=None, cfg=config, system_prompt="", original_prompt="", redacted_text="",
                          max_retries=1, user_print=printed.append)
    return tool.run(args, context), printed


@pytest.fixture
def hubs():
    ha = FakeHomeAssistant([
        entity("light.lounge", "Lounge Lamp", "off", area="Lounge"),
        entity("switch.desk_plug", "Desk Plug", "off", unique_id="plug_9", platform="mqtt"),
    ], token=TOKEN)
    broker = FakeBroker(username="jarvis", password="broker-pw")
    broker.publish("homeassistant/switch/plug9/config", {"name": "Desk Plug", "unique_id": "plug_9",
                                                         "command_topic": "p9/set", "state_topic": "p9/state"})
    broker.publish("homeassistant/switch/fan/config", {"name": "Shed Fan", "unique_id": "shed_fan",
                                                       "command_topic": "sf/set", "state_topic": "sf/state"})
    broker.publish("sf/state", "OFF")
    yield ha, broker
    service.set_home(None)
    ha.close()
    broker.close()


@pytest.mark.unit
def test_settings_to_confirmed_commands_on_both_hubs(hubs, monkeypatch, capsys):
    from jarvis.tools.builtin.home_control import HomeControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_home_tool
    ha, broker = hubs
    credentials.set_secret("smarthome.ha_token", TOKEN)
    credentials.set_secret("smarthome.mqtt_password", "broker-pw")
    config = cfg(smarthome_ha_url=ha.url, smarthome_mqtt_host="127.0.0.1", smarthome_mqtt_port=broker.port,
                 smarthome_mqtt_username="jarvis")
    logged = []
    for module in ("jarvis.smarthome.home_assistant", "jarvis.smarthome.mqtt", "jarvis.smarthome.service",
                   "jarvis.smarthome.model", "jarvis.tools.builtin.home_control"):
        monkeypatch.setattr(f"{module}.debug_log", lambda message, *a, **k: logged.append(message))
    try:
        configure_home_tool(config)
        home = service.get_home()
        assert wait_for(lambda: home.registry.get("ha:light.lounge") is not None
                        and home.registry.get("mqtt:shed_fan") is not None)
        # The plug is in both hubs: only the Home Assistant copy is visible.
        assert [d.id for d in home.registry.resolve("desk plug").devices] == ["ha:switch.desk_plug"]

        tool = BUILTIN_TOOLS["homeControl"]
        result, _printed = run(tool, {"action": "turn_on", "target": "lounge lamp"}, config)
        assert result.success and ha.calls[-1]["service"] == "turn_on"

        broker.on_command = lambda topic, payload: broker.publish("sf/state", payload.decode())
        result, _printed = run(tool, {"action": "turn_on", "target": "shed fan"}, config)
        assert result.success and "confirmed" in result.reply_text
        assert ("sf/set", b"ON", False) in broker.published
    finally:
        configure_home_tool(cfg(smarthome_enabled=False))
    everything = " ".join(logged) + capsys.readouterr().out + result.reply_text
    for secret in (TOKEN, "broker-pw"):
        assert secret not in everything
    for private in ("Lounge", "127.0.0.1", "p9/set", "light.lounge"):
        assert private not in " ".join(logged)
    assert "homeControl" not in BUILTIN_TOOLS


@pytest.mark.unit
def test_a_refused_address_leaves_the_connector_off_with_a_warning(monkeypatch):
    warnings = []
    home = service.build(cfg(smarthome_ha_url="http://8.8.8.8:8123", smarthome_mqtt_host="8.8.4.4"),
                         warn=warnings.append)
    assert home.connectors == {} and len(warnings) == 2
    assert all("8.8" not in w for w in warnings)


@pytest.mark.unit
def test_without_a_token_home_assistant_stays_off():
    warnings = []
    home = service.build(cfg(smarthome_ha_url="http://127.0.0.1:8123"), warn=warnings.append)
    assert "ha" not in home.connectors and "token" in warnings[0]


@pytest.mark.unit
def test_the_command_line_never_prints_the_token(hubs, monkeypatch, capsys):
    from jarvis.smarthome.__main__ import main
    ha, broker = hubs
    credentials.set_secret("smarthome.ha_token", TOKEN)
    credentials.set_secret("smarthome.mqtt_password", "broker-pw")
    config = cfg(smarthome_ha_url=ha.url, smarthome_mqtt_host="127.0.0.1", smarthome_mqtt_port=broker.port,
                 smarthome_mqtt_username="jarvis")
    monkeypatch.setattr("jarvis.config.load_settings", lambda: config)
    assert main(["test"]) == 0
    assert main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "✅ Home Assistant: 2 exposed devices" in out and "✅ MQTT: 2 discovered devices" in out
    assert "Lounge Lamp" in out and "Shed Fan" in out
    assert TOKEN not in out and "broker-pw" not in out
    json.dumps(out)

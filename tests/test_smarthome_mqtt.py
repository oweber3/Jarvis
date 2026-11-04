"""The MQTT discovery connector against an in-process fake broker on loopback (smarthome.spec.md)."""
import json
import time

import pytest

from fake_mqtt import FakeBroker
from jarvis.smarthome import mqtt
from jarvis.smarthome.model import Capability, Command, Device, Registry

PREFIX = "homeassistant"


def wait_for(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def announce(broker):
    broker.publish(f"{PREFIX}/light/desk/light/config", {
        "name": "Desk Light", "uniq_id": "desk_light_1", "~": "home/desk", "cmd_t": "~/set", "stat_t": "~/state",
        "schema": "json", "brightness": True, "sup_clrm": ["rgb"],
        "dev": {"name": "Desk", "ids": ["desk01"], "sa": "Office"}})
    broker.publish("home/desk/state", {"state": "OFF", "brightness": 0})
    broker.publish(f"{PREFIX}/switch/plug/config", {
        "name": "Heater Plug", "unique_id": "plug_7", "command_topic": "plug/7/set", "state_topic": "plug/7/state"})
    broker.publish("plug/7/state", "OFF")
    broker.publish(f"{PREFIX}/cover/garage/config", {
        "name": "Garage Door", "unique_id": "garage_1", "command_topic": "garage/set", "state_topic": "garage/state",
        "device_class": "garage"})
    broker.publish(f"{PREFIX}/sensor/node/temp/config", {
        "name": "Shed Temperature", "unique_id": "shed_temp", "state_topic": "shed/sensors",
        "value_template": "{{ value_json.temperature }}", "unit_of_measurement": "°C"})
    broker.publish("shed/sensors", {"temperature": 7.5, "humidity": 80})
    broker.publish(f"{PREFIX}/button/doorbell/config", {
        "name": "Chime", "unique_id": "chime_1", "command_topic": "chime/press"})


@pytest.fixture(autouse=True)
def _short_test_settle(monkeypatch):
    """A loopback broker delivers retained messages at once, so a connection test settles in far less than a second."""
    monkeypatch.setattr(mqtt, "TEST_SETTLE_SEC", 0.3)


@pytest.fixture
def broker():
    server = FakeBroker()
    announce(server)
    yield server
    server.close()


@pytest.fixture
def connector(broker):
    registry = Registry()
    conn = mqtt.MqttConnector(registry, "127.0.0.1", broker.port)
    conn.start()
    def settled():
        states = {d.id: d.state for d in registry.devices()}
        return conn.connected and len(states) >= 5 and all(states.get(f"mqtt:{key}") for key in
                                                           ("desk_light_1", "plug_7", "shed_temp"))
    assert wait_for(settled), conn.status
    yield conn, registry
    conn.stop()


def plug_echo(broker):
    def on_command(topic, payload):
        if topic == "plug/7/set":
            broker.publish("plug/7/state", payload.decode())
    broker.on_command = on_command


@pytest.mark.unit
class TestDiscovery:
    def test_announced_devices_appear_with_their_names_areas_and_states(self, connector):
        _conn, registry = connector
        light = registry.get("mqtt:desk_light_1")
        assert (light.name, light.area, light.kind, light.state) == ("Desk Desk Light", "Office", "light", "off")
        assert Capability.COLOUR in light.capabilities and Capability.BRIGHTNESS in light.capabilities
        assert registry.get("mqtt:plug_7").state == "off"
        assert registry.get("mqtt:shed_temp").state == "7.5"
        assert registry.get("mqtt:garage_1").device_class == "garage"
        assert registry.get("mqtt:chime_1").kind == "button"

    def test_the_discovery_topics_follow_the_configured_prefix(self, broker):
        registry = Registry()
        broker.publish("custom/switch/x/config", {"name": "Other", "unique_id": "o1", "command_topic": "o/set"})
        conn = mqtt.MqttConnector(registry, "127.0.0.1", broker.port, discovery_prefix="custom")
        conn.start()
        try:
            assert wait_for(lambda: registry.get("mqtt:o1") is not None)
            assert registry.get("mqtt:plug_7") is None
            assert set(broker.subscriptions_seen) >= {"custom/+/+/config", "custom/+/+/+/config"}
        finally:
            conn.stop()

    def test_state_updates_change_the_registry(self, connector, broker):
        _conn, registry = connector
        broker.publish("shed/sensors", {"temperature": 9.0})
        assert wait_for(lambda: registry.get("mqtt:shed_temp").state == "9.0")

    def test_an_empty_config_message_removes_the_device(self, connector, broker):
        _conn, registry = connector
        broker.publish(f"{PREFIX}/button/doorbell/config", "")
        assert wait_for(lambda: registry.get("mqtt:chime_1") is None)

    @pytest.mark.parametrize("payload", [
        {"name": "Wild", "unique_id": "w1", "command_topic": "a/+/set"},
        {"name": "Hash", "unique_id": "w2", "command_topic": "a/#"},
        {"name": "Huge", "unique_id": "w3", "command_topic": "a/set", "junk": "x" * 20000},
        "not json at all",
        ["a", "list"],
    ])
    def test_malformed_or_unsafe_discovery_payloads_are_ignored(self, connector, broker, payload):
        conn, registry = connector
        broker.publish(f"{PREFIX}/switch/bad/config", payload)
        broker.publish(f"{PREFIX}/switch/good/config", {"name": "Good", "unique_id": "g1", "command_topic": "g/set"})
        assert wait_for(lambda: registry.get("mqtt:g1") is not None)
        assert not {d.id for d in registry.devices()} & {"mqtt:w1", "mqtt:w2", "mqtt:w3"}

    def test_unknown_components_are_ignored(self, connector, broker):
        _conn, registry = connector
        broker.publish(f"{PREFIX}/vacuum/v/config", {"name": "Robot", "unique_id": "v1", "command_topic": "v/set"})
        broker.publish(f"{PREFIX}/switch/after/config", {"name": "After", "unique_id": "a1", "command_topic": "x"})
        assert wait_for(lambda: registry.get("mqtt:a1") is not None)
        assert registry.get("mqtt:v1") is None

    def test_an_offline_device_is_unavailable(self, connector, broker):
        _conn, registry = connector
        broker.publish(f"{PREFIX}/switch/avail/config", {"name": "Porch", "unique_id": "p1", "command_topic": "p/set",
                                                         "state_topic": "p/state", "availability_topic": "p/status"})
        broker.publish("p/state", "ON")
        broker.publish("p/status", "offline")
        assert wait_for(lambda: (registry.get("mqtt:p1") or Device("", "", "", "")).state == "unavailable")


@pytest.mark.unit
class TestCommands:
    def test_a_command_goes_to_the_command_topic_unretained_and_is_confirmed_by_state(self, connector, broker):
        conn, registry = connector
        plug_echo(broker)
        outcome = conn.send(registry.get("mqtt:plug_7"), Command("turn_on"))
        assert outcome.confirmed
        assert ("plug/7/set", b"ON", False) in broker.published
        assert registry.get("mqtt:plug_7").state == "on"

    def test_json_schema_lights_get_json_commands(self, connector, broker, monkeypatch):
        conn, registry = connector
        monkeypatch.setattr(mqtt, "CONFIRM_TIMEOUT_SEC", 0.2)   # this light never reports back
        conn.send(registry.get("mqtt:desk_light_1"), Command("set", "colour", "red"))
        topic, payload, retain = broker.published[-1]
        assert topic == "home/desk/set" and not retain
        assert json.loads(payload) == {"state": "ON", "color": {"r": 255, "g": 0, "b": 0}}
        conn.send(registry.get("mqtt:desk_light_1"), Command("set", "brightness", 50))
        assert json.loads(broker.published[-1][1]) == {"state": "ON", "brightness": 128}

    def test_a_device_that_never_reports_back_is_sent_but_not_confirmed(self, connector, broker, monkeypatch):
        conn, registry = connector
        monkeypatch.setattr(mqtt, "CONFIRM_TIMEOUT_SEC", 0.2)
        outcome = conn.send(registry.get("mqtt:plug_7"), Command("turn_on"))
        assert not outcome.confirmed and "not confirmed" in outcome.note

    def test_a_device_without_a_state_topic_is_sent_but_not_confirmed(self, connector, broker):
        conn, registry = connector
        outcome = conn.send(registry.get("mqtt:chime_1"), Command("activate"))
        assert not outcome.confirmed and "not confirmed" in outcome.note
        assert wait_for(lambda: ("chime/press", b"PRESS", False) in broker.published)

    def test_nothing_but_commands_is_ever_published(self, connector, broker):
        conn, registry = connector
        plug_echo(broker)
        conn.send(registry.get("mqtt:plug_7"), Command("turn_off"))
        assert {topic for topic, _p, _r in broker.published} == {"plug/7/set"}
        assert not any(retain for _t, _p, retain in broker.published)


@pytest.mark.unit
class TestConnection:
    def test_test_connection_counts_discovered_devices(self, broker):
        assert mqtt.test_connection("127.0.0.1", broker.port) == 5

    def test_a_wrong_login_is_reported(self):
        server = FakeBroker(username="jarvis", password="right")
        try:
            with pytest.raises(mqtt.MqttAuthError):
                mqtt.test_connection("127.0.0.1", server.port, username="jarvis", password="wrong")
            assert mqtt.test_connection("127.0.0.1", server.port, username="jarvis", password="right") == 0
        finally:
            server.close()

    def test_a_public_broker_is_refused_before_connecting(self):
        with pytest.raises(mqtt.MqttError):
            mqtt.test_connection("8.8.8.8", 1883)

    def test_an_unreachable_broker(self, broker):
        port = broker.port
        broker.close()
        with pytest.raises(mqtt.MqttUnreachable):
            mqtt.test_connection("127.0.0.1", port)

    def test_a_disconnect_marks_devices_unavailable(self, connector, broker):
        conn, registry = connector
        broker.drop_connections()
        assert wait_for(lambda: registry.get("mqtt:plug_7").state in ("unavailable", "off"))
        assert wait_for(lambda: conn.connected, timeout=8)

    def test_the_password_never_reaches_the_debug_log(self, monkeypatch):
        logged = []
        monkeypatch.setattr(mqtt, "debug_log", lambda message, *a, **k: logged.append(message))
        server = FakeBroker(username="jarvis", password="hunter2")
        try:
            mqtt.test_connection("127.0.0.1", server.port, username="jarvis", password="hunter2")
        finally:
            server.close()
        assert "hunter2" not in " ".join(logged) and "127.0.0.1" not in " ".join(logged)


@pytest.mark.unit
class TestRobustness:
    def test_the_connection_goes_to_the_resolved_address_not_the_name(self, broker, monkeypatch):
        from jarvis.smarthome import network
        monkeypatch.setattr(network, "_resolve", lambda host: ["127.0.0.1"])
        # A name no resolver knows: only the pinned socket override can reach the broker.
        assert mqtt.test_connection("jarvis-broker.invalid", broker.port) == 5
        registry = Registry()
        conn = mqtt.MqttConnector(registry, "jarvis-broker.invalid", broker.port)
        conn.start()
        try:
            assert wait_for(lambda: conn.connected and registry.count() >= 5)
        finally:
            conn.stop()

    def test_a_name_not_found_yet_is_retried_without_blocking_start(self, broker, monkeypatch):
        from jarvis.smarthome import network
        answers = iter([[], []])
        monkeypatch.setattr(network, "_resolve", lambda host: next(answers, ["127.0.0.1"]))
        monkeypatch.setattr(mqtt, "BACKOFF_START_SEC", 0.05)
        registry = Registry()
        conn = mqtt.MqttConnector(registry, "broker.local", broker.port)
        conn.start()
        try:
            assert wait_for(lambda: conn.connected)
        finally:
            conn.stop()

    def test_a_missing_paho_library_has_its_own_status(self, monkeypatch):
        def missing():
            raise ImportError("No module named 'paho'")
        monkeypatch.setattr(mqtt, "_paho", missing)
        conn = mqtt.MqttConnector(Registry(), "127.0.0.1", 1883)
        with pytest.raises(ImportError):
            conn.start()
        assert conn.status == "missing_library"

    def test_a_test_with_a_chatty_device_still_ends(self, broker, monkeypatch):
        import threading
        monkeypatch.setattr(mqtt, "TEST_MAX_SEC", 1.5)
        stop = threading.Event()

        def chatter():
            while not stop.is_set():
                broker.publish(f"{PREFIX}/sensor/node/temp/config", {
                    "name": "Shed Temperature", "unique_id": "shed_temp", "state_topic": "shed/sensors"})
                time.sleep(0.05)

        thread = threading.Thread(target=chatter, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            assert mqtt.test_connection("127.0.0.1", broker.port) == 5
            assert time.monotonic() - started < 4
        finally:
            stop.set()
        assert not {"plug/7/state", "shed/sensors", "home/desk/state"} & set(broker.subscriptions_seen)

    def test_the_same_object_id_under_two_nodes_is_two_devices(self, connector, broker):
        _conn, registry = connector
        for node in ("porch", "shed"):
            broker.publish(f"{PREFIX}/switch/{node}/relay/config", {"name": f"{node} relay",
                                                                    "command_topic": f"{node}/set"})
        assert wait_for(lambda: {"mqtt:switch_porch.relay", "mqtt:switch_shed.relay"}
                        <= {d.id for d in registry.devices()})

    def test_node_and_object_ids_that_join_alike_stay_two_devices(self, connector, broker):
        _conn, registry = connector
        for node, obj in (("a_b", "c"), ("a", "b_c")):
            broker.publish(f"{PREFIX}/switch/{node}/{obj}/config", {"name": f"{node} {obj}",
                                                                    "command_topic": f"{node}/{obj}/set"})
        assert wait_for(lambda: len([d for d in registry.devices() if d.name in ("a_b c", "a b_c")]) == 2)

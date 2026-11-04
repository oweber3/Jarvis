"""Settings > Smart Home without Qt: test before save, secrets saved only after a passing test."""
import pytest

from fake_home_assistant import FakeHomeAssistant, entity
from fake_mqtt import FakeBroker
from jarvis import credentials
from jarvis.smarthome import setup
from jarvis.smarthome.service import HA_TOKEN_KEY, MQTT_PASSWORD_KEY, SmartHome


@pytest.fixture
def ha():
    server = FakeHomeAssistant([entity("light.a", "Lamp", area="Den"), entity("switch.b", "Plug")], token="tok")
    yield server
    server.close()


@pytest.mark.unit
class TestHomeAssistantTest:
    def test_a_passing_test_saves_the_pasted_token(self, ha):
        result = setup.test_home_assistant(ha.url, "tok")
        assert result.ok and result.count == 2 and "2 exposed devices" in result.message
        assert credentials.get_secret(HA_TOKEN_KEY) == "tok"

    def test_a_failing_test_saves_nothing(self, ha):
        result = setup.test_home_assistant(ha.url, "wrong")
        assert not result.ok and "create a new one" in result.message
        assert credentials.get_secret(HA_TOKEN_KEY) == ""

    def test_the_saved_token_is_used_when_nothing_is_pasted(self, ha):
        credentials.set_secret(HA_TOKEN_KEY, "tok")
        assert setup.test_home_assistant(ha.url, "").ok

    def test_messages_never_hold_the_token(self, ha):
        assert "wrong-secret" not in setup.test_home_assistant(ha.url, "wrong-secret").message


@pytest.mark.unit
class TestMqttTest:
    def test_a_passing_test_saves_the_password_and_a_failing_one_does_not(self):
        broker = FakeBroker(username="u", password="pw")
        try:
            assert not setup.test_mqtt("127.0.0.1", broker.port, "u", "bad").ok
            assert credentials.get_secret(MQTT_PASSWORD_KEY) == ""
            assert setup.test_mqtt("127.0.0.1", broker.port, "u", "pw").ok
            assert credentials.get_secret(MQTT_PASSWORD_KEY) == "pw"
        finally:
            broker.close()


@pytest.mark.unit
class TestSaveGate:
    def test_changed_connection_settings_need_a_passing_test(self):
        saved = {"ha": setup.ha_key("", True), "mqtt": setup.mqtt_key("", 1883, "", False, "homeassistant")}
        gate = setup.SaveGate(saved=dict(saved))
        new_ha = setup.ha_key("http://127.0.0.1:8123", True)
        assert gate.problem(new_ha, saved["mqtt"])
        gate.passed_with(new_ha)
        assert gate.problem(new_ha, saved["mqtt"]) is None
        assert gate.problem(setup.ha_key("http://127.0.0.1:8124", True), saved["mqtt"])

    def test_unchanged_or_cleared_settings_need_no_test(self):
        saved = {"ha": setup.ha_key("http://127.0.0.1:8123", True),
                 "mqtt": setup.mqtt_key("", 1883, "", False, "homeassistant")}
        gate = setup.SaveGate(saved=dict(saved))
        assert gate.problem(saved["ha"], saved["mqtt"]) is None
        assert gate.problem(setup.ha_key("", True), saved["mqtt"]) is None


@pytest.mark.unit
def test_device_rows_include_hidden_mqtt_devices_so_they_can_be_shown_again(ha):
    from types import SimpleNamespace
    from jarvis.smarthome import service
    credentials.set_secret(HA_TOKEN_KEY, "tok")
    cfg = SimpleNamespace(smarthome_enabled=True, smarthome_ha_url=ha.url, smarthome_ha_verify_tls=True,
                          smarthome_mqtt_host="", smarthome_mqtt_hidden=["ha:switch.b"])
    rows, problems = setup.read_devices(service.build(cfg), wait=5)
    assert problems == [] and {r.id for r in rows} == {"ha:light.a", "ha:switch.b"}


@pytest.mark.unit
def test_device_choices_are_written_only_when_set():
    config = {"smarthome_mqtt_hidden": ["mqtt:old"]}
    setup.apply_device_choices(config, [], ["ha:lock.front", "ha:lock.front"])
    assert config == {"smarthome_confirm_devices": ["ha:lock.front"]}
    assert setup.read_devices(SmartHome())[0] == []


@pytest.mark.unit
def test_a_connector_that_fails_to_start_is_still_stopped_and_reported():
    class Hub:
        status, stopped = "starting", False

        def start(self):
            raise RuntimeError("boom")

        def stop(self):
            self.stopped = True

    hub = Hub()
    rows, problems = setup.read_devices(SmartHome(connectors={"ha": hub}), wait=0.1, sleep=lambda s: None)
    assert rows == [] and problems and hub.stopped

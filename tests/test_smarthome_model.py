"""The shared smart home device model: devices, the registry and name resolution (smarthome.spec.md)."""
import pytest

from jarvis.smarthome.model import (MAX_DEVICES, MAX_NAME_CHARS, Capability, Device, Registry, capabilities_for,
                                    supports)


def device(id_, name, kind="light", area="", aliases=(), state="off", source=None, unique_id="", **extra):
    source = source or id_.split(":", 1)[0]
    return Device(id=id_, name=name, kind=kind, source=source, area=area, aliases=tuple(aliases), state=state,
                  capabilities=capabilities_for(kind, extra.pop("features", None)), unique_id=unique_id, **extra)


@pytest.fixture
def registry():
    reg = Registry()
    reg.replace_source("ha", [
        device("ha:light.kitchen_ceiling", "Kitchen Ceiling", area="Kitchen"),
        device("ha:light.kitchen_strip", "Kitchen Strip", area="Kitchen"),
        device("ha:light.bedroom_lamp", "Bedroom Lamp", area="Bedroom", aliases=("Reading light",)),
        device("ha:switch.kettle", "Kettle", kind="switch", area="Kitchen"),
        device("ha:sensor.bedroom_temp", "Bedroom Temperature", kind="sensor", area="Bedroom", state="21.5"),
        device("ha:light.lampe_salon", "Lampe Écran", area="Salon"),
    ])
    return reg


@pytest.mark.unit
class TestResolution:
    def test_an_exact_name_wins_in_any_case(self, registry):
        found = registry.resolve("kitchen ceiling")
        assert [d.id for d in found.devices] == ["ha:light.kitchen_ceiling"] and not found.candidates

    def test_an_alias_or_an_id_is_exact_too(self, registry):
        assert [d.id for d in registry.resolve("READING LIGHT").devices] == ["ha:light.bedroom_lamp"]
        assert [d.id for d in registry.resolve("ha:switch.kettle").devices] == ["ha:switch.kettle"]

    def test_a_unique_device_whose_name_holds_every_spoken_word_is_chosen(self, registry):
        assert [d.id for d in registry.resolve("strip").devices] == ["ha:light.kitchen_strip"]

    def test_several_partial_matches_without_an_area_change_nothing(self, registry):
        found = registry.resolve("kitchen")
        assert found.devices == ()
        assert {d.id for d in found.candidates} == {"ha:light.kitchen_ceiling", "ha:light.kitchen_strip"}

    def test_an_area_with_a_kind_means_every_device_of_that_kind_there(self, registry):
        found = registry.resolve("", area="kitchen", kind="light")
        assert {d.id for d in found.devices} == {"ha:light.kitchen_ceiling", "ha:light.kitchen_strip"}

    def test_a_kind_without_an_area_or_name_is_only_candidates(self, registry):
        found = registry.resolve("", kind="light")
        assert found.devices == () and len(found.candidates) == 4

    def test_an_area_narrows_a_name(self, registry):
        assert [d.id for d in registry.resolve("lamp", area="Bedroom").devices] == ["ha:light.bedroom_lamp"]

    def test_nothing_is_guessed(self, registry):
        found = registry.resolve("garage door")
        assert found.devices == () and found.candidates == ()

    def test_words_are_compared_after_unicode_case_folding(self, registry):
        assert [d.id for d in registry.resolve("LAMPE ÉCRAN").devices] == ["ha:light.lampe_salon"]
        assert [d.id for d in registry.resolve("écran").devices] == ["ha:light.lampe_salon"]

    def test_a_spoken_area_name_with_a_kind_reaches_the_area(self, registry):
        found = registry.resolve("kitchen", kind="light")
        assert {d.id for d in found.devices} == {"ha:light.kitchen_ceiling", "ha:light.kitchen_strip"}


@pytest.mark.unit
class TestRegistry:
    def test_states_update_in_memory(self, registry):
        registry.update_state("ha:light.bedroom_lamp", "on", {"brightness": 40})
        lamp = registry.get("ha:light.bedroom_lamp")
        assert lamp.state == "on" and lamp.attributes["brightness"] == 40

    def test_a_disconnected_hub_reports_its_devices_unavailable(self, registry):
        registry.mark_unavailable("ha")
        assert {d.state for d in registry.devices()} == {"unavailable"}

    def test_the_home_assistant_copy_of_a_device_hides_the_mqtt_copy(self):
        reg = Registry()
        reg.replace_source("mqtt", [device("mqtt:abc123", "Desk Plug", kind="switch", unique_id="abc123"),
                                    device("mqtt:zz9", "Hall Sensor", kind="sensor", unique_id="zz9")])
        reg.replace_source("ha", [device("ha:switch.desk_plug", "Desk Plug", kind="switch", unique_id="abc123")])
        assert {d.id for d in reg.devices()} == {"ha:switch.desk_plug", "mqtt:zz9"}
        assert reg.get("mqtt:abc123") is None
        assert [d.id for d in reg.resolve("desk plug").devices] == ["ha:switch.desk_plug"]

    def test_hidden_devices_are_never_listed_or_resolved(self):
        reg = Registry(hidden=("mqtt:secret",))
        reg.replace_source("mqtt", [device("mqtt:secret", "Secret Plug", kind="switch")])
        assert reg.devices() == () and reg.resolve("secret plug").devices == ()

    def test_names_are_trimmed_and_the_device_count_is_bounded(self):
        reg = Registry()
        reg.replace_source("mqtt", [device(f"mqtt:{i}", "x" * 500 + str(i), kind="switch")
                                    for i in range(MAX_DEVICES + 50)])
        kept = reg.devices()
        assert len(kept) == MAX_DEVICES
        assert max(len(d.name) for d in kept) <= MAX_NAME_CHARS

    def test_areas_are_listed_once(self, registry):
        assert sorted(registry.areas()) == ["Bedroom", "Kitchen", "Salon"]


@pytest.mark.unit
class TestCapabilities:
    def test_each_kind_has_the_capabilities_of_the_spec_table(self):
        assert Capability.ON_OFF in capabilities_for("light")
        assert Capability.BRIGHTNESS in capabilities_for("light")
        assert capabilities_for("sensor") == frozenset({Capability.READ})
        assert Capability.LOCK in capabilities_for("lock")
        assert Capability.POSITION in capabilities_for("cover")

    def test_a_hub_can_narrow_what_a_device_supports(self):
        plain = capabilities_for("light", {Capability.ON_OFF})
        assert plain == frozenset({Capability.ON_OFF})

    def test_actions_are_checked_against_capabilities(self):
        lamp = device("ha:light.a", "A")
        lock = device("ha:lock.front", "Front", kind="lock")
        assert supports(lamp, "turn_on") and not supports(lock, "turn_on")
        assert supports(lock, "unlock") and not supports(lamp, "unlock")
        assert supports(lamp, "set", "brightness") and not supports(lamp, "set", "temperature")

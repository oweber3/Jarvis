"""Smart home referents reach Codex or Claude only when smart home is shared (smarthome.spec.md)."""
from types import SimpleNamespace

import pytest

from jarvis.bridge.adapter import build_desktop_context
from jarvis.bridge.settings import BridgeSettings
from jarvis.memory.desktop_referents import get_desktop_referents


@pytest.fixture
def referents():
    store = get_desktop_referents()
    store.clear()
    store.record_device("ha:lock.front_door", "homeControl", "lock")
    store.record_device("tv", "tvControl", "key PowerOff")
    yield store
    store.clear()


def shared_devices(cfg):
    desktop = build_desktop_context(cfg, BridgeSettings(share_desktop_referents=True), "voice")
    return {entry.get("device") for entry in desktop.get("other_referents", [])}


@pytest.mark.unit
def test_home_devices_stay_local_while_smart_home_is_not_shared(referents):
    cfg = SimpleNamespace(dialogue_memory_timeout=300, smarthome_enabled=True, smarthome_share_with_cloud=False)
    assert shared_devices(cfg) == {"tv"}


@pytest.mark.unit
def test_home_devices_are_shared_with_the_switch_on(referents):
    cfg = SimpleNamespace(dialogue_memory_timeout=300, smarthome_enabled=True, smarthome_share_with_cloud=True)
    assert shared_devices(cfg) == {"tv", "ha:lock.front_door"}

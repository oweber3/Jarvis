"""Pairing a phone: one-time codes, hashed device tokens, revocation shared between processes."""
import json

import pytest

from jarvis.remote.pairing import MAX_CODE_ATTEMPTS, NAME_MAX_CHARS, DeviceStore


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return DeviceStore(tmp_path, clock=clock)


def pair(store, name="Alex's iPhone"):
    code = store.start_pairing()
    return store.complete_pairing(code, name)


@pytest.mark.unit
class TestPairingCodes:
    def test_a_code_is_six_digits(self, store):
        code = store.start_pairing()
        assert len(code) == 6 and code.isdigit()

    def test_the_right_code_issues_a_token_that_authenticates(self, store):
        result = pair(store)
        assert result is not None
        device = store.authenticate(result.token)
        assert device is not None and device.id == result.device_id

    def test_a_code_works_only_once(self, store):
        code = store.start_pairing()
        assert store.complete_pairing(code, "phone") is not None
        assert store.complete_pairing(code, "phone") is None

    def test_a_wrong_code_issues_nothing(self, store):
        code = store.start_pairing()
        wrong = "000000" if code != "000000" else "111111"
        assert store.complete_pairing(wrong, "phone") is None
        assert store.devices() == []

    def test_too_many_wrong_attempts_void_the_code(self, store):
        code = store.start_pairing()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(MAX_CODE_ATTEMPTS):
            assert store.complete_pairing(wrong, "phone") is None
        assert store.complete_pairing(code, "phone") is None
        assert not store.pairing_active()

    def test_an_expired_code_issues_nothing(self, store, clock):
        code = store.start_pairing(ttl_sec=60)
        clock.now += 61
        assert store.complete_pairing(code, "phone") is None
        assert not store.pairing_active()

    def test_a_new_code_replaces_the_old_one(self, store):
        first = store.start_pairing()
        second = store.start_pairing()
        if first != second:
            assert store.complete_pairing(first, "phone") is None
        assert store.complete_pairing(second, "phone") is not None

    def test_the_remaining_time_counts_down(self, store, clock):
        store.start_pairing(ttl_sec=300)
        clock.now += 100
        assert store.pairing_seconds_left() == pytest.approx(200, abs=1)

    @pytest.mark.parametrize("bad", [None, 123456, "", "12345a", "1234567"])
    def test_malformed_codes_are_refused(self, store, bad):
        store.start_pairing()
        assert store.complete_pairing(bad, "phone") is None


@pytest.mark.unit
class TestDevices:
    def test_tokens_are_never_stored_in_clear(self, store, tmp_path):
        result = pair(store)
        stored = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
        assert result.token not in stored

    def test_codes_are_never_stored_in_clear(self, store, tmp_path):
        code = store.start_pairing()
        stored = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
        assert f'"{code}"' not in stored

    def test_unknown_and_empty_tokens_do_not_authenticate(self, store):
        pair(store)
        for token in ["", None, "nope", "x" * 43]:
            assert store.authenticate(token) is None

    def test_a_revoked_device_stops_working(self, store):
        result = pair(store)
        assert store.revoke(result.device_id)
        assert store.authenticate(result.token) is None
        assert store.devices() == []

    def test_a_revoke_from_another_process_reaches_a_running_store(self, store, tmp_path, clock):
        result = pair(store)
        other = DeviceStore(tmp_path, clock=clock)
        assert other.revoke(result.device_id)
        assert store.authenticate(result.token) is None

    def test_a_pairing_code_from_another_process_works(self, store, tmp_path, clock):
        code = DeviceStore(tmp_path, clock=clock).start_pairing()
        assert store.complete_pairing(code, "phone") is not None

    def test_device_names_are_trimmed(self, store):
        pair(store, "  " + "n" * (NAME_MAX_CHARS + 20) + "  ")
        (device,) = store.devices()
        assert device.name == "n" * NAME_MAX_CHARS

    def test_a_blank_name_gets_a_default(self, store):
        pair(store, "   ")
        (device,) = store.devices()
        assert device.name

    def test_last_seen_follows_use(self, store, clock):
        result = pair(store)
        clock.now += 30
        store.authenticate(result.token)
        (device,) = store.devices()
        assert device.last_seen == pytest.approx(clock.now)

    def test_a_corrupt_device_file_means_no_devices(self, tmp_path, clock):
        (tmp_path / "remote_devices.json").write_text("{not json", encoding="utf-8")
        store = DeviceStore(tmp_path, clock=clock)
        assert store.devices() == []
        assert pair(store) is not None

    def test_several_devices_are_independent(self, store):
        first, second = pair(store, "phone"), pair(store, "tablet")
        store.revoke(first.device_id)
        assert store.authenticate(second.token) is not None
        assert [d.name for d in store.devices()] == ["tablet"]

    def test_the_device_file_is_valid_json(self, store, tmp_path):
        pair(store)
        data = json.loads((tmp_path / "remote_devices.json").read_text(encoding="utf-8"))
        assert len(data["devices"]) == 1

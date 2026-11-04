"""Smart home hubs are reached on the local network only (smarthome.spec.md, Boundaries)."""
import pytest

from jarvis.smarthome import network
from jarvis.smarthome.network import SmartHomeAddressError, validate_endpoint


def resolver(*addresses):
    return lambda host: list(addresses)


@pytest.mark.unit
class TestAddressValidation:
    @pytest.mark.parametrize("url,ip,port", [
        ("http://192.168.1.50:8123", "192.168.1.50", 8123),
        ("http://10.1.2.3:8123/", "10.1.2.3", 8123),
        ("https://172.20.0.4", "172.20.0.4", 443),
        ("http://127.0.0.1:8123", "127.0.0.1", 8123),
        ("http://169.254.10.10:8123", "169.254.10.10", 8123),
        ("http://[fe80::1]:8123", "fe80::1", 8123),
        ("http://[::1]:8123", "::1", 8123),
    ])
    def test_private_loopback_and_link_local_addresses_are_accepted(self, url, ip, port):
        endpoint = validate_endpoint(url, default_port=8123)
        assert (endpoint.ip, endpoint.port) == (ip, port)

    @pytest.mark.parametrize("url", [
        "http://8.8.8.8:8123", "https://example.nabu.casa", "http://100.64.1.1:8123", "ftp://192.168.1.50",
        "http://user:pw@192.168.1.50:8123", "", "not a url", "http://192.168.1.50:8123/path?q=1",
        "http://0.0.0.0:8123", "http://[fe80::1%25eth0]:8123",
    ])
    def test_public_remote_and_malformed_addresses_are_refused(self, url, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver("93.184.216.34"))
        with pytest.raises(SmartHomeAddressError):
            validate_endpoint(url, default_port=8123)

    def test_a_hostname_is_accepted_when_every_address_it_resolves_to_is_allowed(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver("192.168.1.50", "fe80::5"))
        endpoint = validate_endpoint("http://homeassistant.local:8123", default_port=8123)
        assert endpoint.ip == "192.168.1.50" and endpoint.host == "homeassistant.local"

    def test_a_hostname_with_any_public_address_is_refused(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver("192.168.1.50", "93.184.216.34"))
        with pytest.raises(SmartHomeAddressError):
            validate_endpoint("http://homeassistant.local:8123", default_port=8123)

    def test_a_hostname_that_does_not_resolve_is_refused(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver())
        with pytest.raises(SmartHomeAddressError):
            validate_endpoint("http://homeassistant.local:8123", default_port=8123)

    def test_a_bare_host_is_accepted_for_a_broker(self):
        endpoint = validate_endpoint("192.168.1.50", default_port=1883, schemes=("mqtt", "mqtts"))
        assert (endpoint.ip, endpoint.port) == ("192.168.1.50", 1883)

    def test_the_refusal_never_repeats_the_address(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver("93.184.216.34"))
        with pytest.raises(SmartHomeAddressError) as error:
            validate_endpoint("http://secret-house.example.com:8123", default_port=8123)
        assert "secret-house" not in str(error.value)


@pytest.mark.unit
class TestResolution:
    def test_a_slow_lookup_is_bounded_and_never_changes_the_process_default_timeout(self, monkeypatch):
        import socket
        import time
        default = socket.getdefaulttimeout()
        monkeypatch.setattr(network, "RESOLVE_TIMEOUT_SEC", 0.2)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: time.sleep(3) or [])
        started = time.monotonic()
        with pytest.raises(network.SmartHomeNotFound):
            validate_endpoint("http://homeassistant.local:8123", default_port=8123)
        assert time.monotonic() - started < 1.5
        assert socket.getdefaulttimeout() == default

    def test_an_unknown_hostname_is_not_found_yet_rather_than_refused(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver())
        with pytest.raises(network.SmartHomeNotFound) as error:
            validate_endpoint("http://homeassistant.local:8123", default_port=8123)
        assert "could not be found" in str(error.value)

    def test_without_resolving_only_syntax_and_literal_addresses_are_checked(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", lambda host: pytest.fail("resolved"))
        assert validate_endpoint("http://homeassistant.local:8123", default_port=8123, resolve=False).ip == ""
        with pytest.raises(SmartHomeAddressError):
            validate_endpoint("http://8.8.8.8:8123", default_port=8123, resolve=False)
        with pytest.raises(SmartHomeAddressError):
            validate_endpoint("ftp://homeassistant.local", default_port=8123, resolve=False)

    def test_an_address_copied_from_the_browser_uses_the_web_port(self):
        # A browser hides ":80", so "http://192.168.1.36" is Home Assistant serving on port 80.
        assert validate_endpoint("http://192.168.1.36", default_port=8123, resolve=False).port == 80
        assert validate_endpoint("http://192.168.1.36/", default_port=8123, resolve=False).port == 80
        assert validate_endpoint("https://192.168.1.36", default_port=8123, resolve=False).port == 443

    def test_a_bare_address_uses_the_service_default_port(self):
        assert validate_endpoint("192.168.1.36", default_port=8123, resolve=False).port == 8123
        assert validate_endpoint("192.168.1.36", default_port=1883, schemes=("mqtt", "mqtts"),
                                 resolve=False).port == 1883

    def test_a_link_local_hostname_keeps_its_scope_id(self, monkeypatch):
        monkeypatch.setattr(network, "_resolve", resolver("fe80::5%eth0"))
        assert validate_endpoint("http://ha.local:8123", default_port=8123).ip == "fe80::5%eth0"

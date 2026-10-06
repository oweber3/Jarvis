"""Phone access answers only clients on private networks: home LAN, VPN shared space and loopback."""
import pytest

from jarvis.remote.networks import is_allowed_client


@pytest.mark.unit
class TestAllowedClients:
    @pytest.mark.parametrize("address", [
        "127.0.0.1", "::1",                       # this PC
        "192.168.1.20", "10.0.0.5", "172.20.3.4",  # home networks
        "169.254.10.10", "fe80::1",                # link-local
        "100.101.102.103",                         # Tailscale / CGNAT shared space
        "fd7a:115c:a1e0::1",                       # Tailscale IPv6 (unique local)
        "::ffff:192.168.1.20",                     # IPv4-mapped home address
    ])
    def test_private_addresses_are_allowed(self, address):
        assert is_allowed_client(address)

    @pytest.mark.parametrize("address", [
        "8.8.8.8", "1.1.1.1", "172.32.0.1", "100.128.0.1", "2001:4860:4860::8888",
        "::ffff:8.8.8.8", "", "not an address", None,
    ])
    def test_public_and_malformed_addresses_are_refused(self, address):
        assert not is_allowed_client(address)

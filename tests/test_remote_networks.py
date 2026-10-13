"""Phone access answers only clients on private networks: home LAN, VPN shared space and loopback."""
import pytest

from jarvis.remote.networks import is_allowed_client, is_own_host


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


@pytest.mark.unit
class TestOwnHostNames:
    """The names a phone opens the app by. Any other name may be a DNS name rebound to this PC."""

    @pytest.mark.parametrize("name", [
        "localhost", "127.0.0.1", "192.168.1.5", "100.101.102.103", "fd7a:115c:a1e0::1",
        "desktop-abc", "desktop-abc.local", "desktop-abc.lan", "desktop-abc.home.arpa",
        "desktop-abc.tail1234.ts.net",
    ])
    def test_this_pcs_names_and_private_addresses_are_its_own(self, name):
        assert is_own_host(name, "desktop-abc")

    @pytest.mark.parametrize("name", [
        "", "evil.example", "8.8.8.8", "0.0.0.0", "desktop-abc.evil.example", "desktop-abcx.tail1234.ts.net",
        "desktop-abc.a.b.ts.net", "phone.tail1234.ts.net", "desktop-ab",
    ])
    def test_other_names_are_not(self, name):
        assert not is_own_host(name, "desktop-abc")

    def test_without_a_pc_name_only_addresses_count(self):
        assert is_own_host("192.168.1.5", "")
        assert not is_own_host("anything.local", "")

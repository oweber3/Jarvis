"""The PC's addresses and where it is are personal: the location debug log never names them."""
from unittest.mock import MagicMock, patch

import pytest

from jarvis.utils import location

pytestmark = pytest.mark.unit

CGNAT = "100.64.7.9"
PUBLIC = "93.184.216.34"


@pytest.fixture
def logged(monkeypatch):
    messages = []
    monkeypatch.setattr(location, "debug_log", lambda message, *a, **k: messages.append(message))
    monkeypatch.setattr(location, "_persist_disk_caches", lambda *a, **k: None)
    monkeypatch.setattr(location, "_location_cache", {})
    monkeypatch.setattr(location, "_cgnat_resolution_cache", {})
    return messages


def _assert_not_named(messages, *values):
    text = "\n".join(messages)
    assert messages
    for value in values:
        assert value not in text, value


def test_an_address_found_through_opendns(logged):
    with patch.object(location, "_get_external_ip_via_upnp", return_value=None), \
         patch.object(location, "_get_external_ip_via_socket", return_value=None), \
         patch.object(location, "_resolve_public_ip_via_opendns", return_value=PUBLIC):
        assert location._get_external_ip_automatically() == PUBLIC
    _assert_not_named(logged, PUBLIC)


def _reader(city=None, error=None):
    reader = MagicMock()
    reader.__enter__.return_value = reader
    if error is not None:
        reader.city.side_effect = error
    else:
        reader.city.return_value = city
    return reader


@pytest.mark.skipif(not location.GEOIP2_AVAILABLE, reason="geoip2 not installed")
def test_a_cgnat_address_resolved_and_located(logged, tmp_path):
    city = MagicMock()
    city.country.name, city.country.iso_code = "Wakanda", "WK"
    city.subdivisions.most_specific.name, city.subdivisions.most_specific.iso_code = "Golden City Region", "GC"
    city.city.name = "Birnin Zana"
    city.location.latitude = city.location.longitude = None
    city.location.time_zone, city.location.accuracy_radius = "UTC", 10
    database = tmp_path / "GeoLite2-City.mmdb"
    database.write_bytes(b"x")
    with patch.object(location, "_resolve_public_ip_via_opendns", return_value=PUBLIC), \
         patch.object(location, "_get_database_path", return_value=database), \
         patch.object(location.geoip2.database, "Reader", return_value=_reader(city)):
        info = location.get_location_info(CGNAT, resolve_cgnat_public_ip=True)
        assert info["city"] == "Birnin Zana"
        location.get_location_info(CGNAT, resolve_cgnat_public_ip=True)  # the cached resolution
    _assert_not_named(logged, CGNAT, PUBLIC, "Birnin Zana", "Wakanda")


@pytest.mark.skipif(not location.GEOIP2_AVAILABLE, reason="geoip2 not installed")
@pytest.mark.parametrize("error", [
    lambda: location.geoip2.errors.AddressNotFoundError(f"The address {PUBLIC} is not in the database."),
    lambda: ValueError(f"'{PUBLIC}' does not appear to be an IPv4 or IPv6 address"),
])
def test_an_address_that_cannot_be_located(logged, tmp_path, error):
    database = tmp_path / "GeoLite2-City.mmdb"
    database.write_bytes(b"x")
    with patch.object(location, "_get_database_path", return_value=database), \
         patch.object(location.geoip2.database, "Reader", return_value=_reader(error=error())):
        assert "error" in location.get_location_info(PUBLIC, auto_detect=False)
    _assert_not_named(logged, PUBLIC)


def test_a_missing_database_does_not_name_its_folder(logged, tmp_path, monkeypatch):
    database = tmp_path / "someone" / "GeoLite2-City.mmdb"
    monkeypatch.setattr(location, "_print_location_setup_instructions", lambda *a, **k: None)
    with patch.object(location, "_get_database_path", return_value=database):
        assert location._download_geolite2_database() is False
    _assert_not_named(logged, str(tmp_path))

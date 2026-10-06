"""The Roku ECP client: behaviour against a fake device, address policy, app resolution, discovery."""
import socket
import threading

import pytest

from fake_roku import FakeRoku


@pytest.fixture
def roku(fake_tv):
    return fake_tv


def client():
    from jarvis.devices.roku import RokuClient
    return RokuClient("127.0.0.1")


# --- address policy -------------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "10.0.0.5", "172.16.4.9", "172.31.255.1", "192.168.1.50", "169.254.10.20", "fe80::1", "[fe80::abcd]",
])
def test_private_and_link_local_addresses_are_accepted(host):
    from jarvis.devices.roku import validate_host
    assert validate_host(host)


@pytest.mark.parametrize("host", [
    "", "   ", "8.8.8.8", "1.1.1.1", "172.32.0.1", "192.169.1.1", "11.0.0.1", "127.0.0.1", "0.0.0.0",
    "224.0.0.251", "255.255.255.255", "2001:4860:4860::8888", "::ffff:8.8.8.8", "::1",
    "example.com", "tv.local", "192.168.1.50:8060", "http://192.168.1.50", "192.168.1.50/path",
    "192.168.1.50 8.8.8.8", "192.168.1", "999.1.1.1", None, 42,
])
def test_anything_else_is_rejected(host):
    from jarvis.devices.roku import RokuAddressError, validate_host
    with pytest.raises(RokuAddressError):
        validate_host(host)


def test_client_refuses_a_public_address_before_any_network_use():
    from jarvis.devices.roku import RokuAddressError, RokuClient
    with pytest.raises(RokuAddressError):
        RokuClient("8.8.8.8")


# --- queries --------------------------------------------------------------------------------------

def test_device_info_reads_name_model_power_and_text_support(roku):
    info = client().device_info()
    assert (info.name, info.model, info.power_mode, info.supports_text) == (
        "Living Room TV", "Fake Roku TV", "PowerOn", True)
    assert info.serial == "FAKESERIAL1"


def test_apps_come_from_the_device_not_a_built_in_table(roku):
    roku.apps = [("9", "appl", "Only App")]
    assert [(a.id, a.name) for a in client().apps()] == [("9", "Only App")]


def test_app_names_are_tidied_of_non_breaking_spaces_and_xml_escapes(roku):
    roku.apps = [("1", "tvin", "HDMI 1"), ("2", "appl", "Tubi - Free Movies & TV")]
    assert [a.name for a in client().apps()] == ["HDMI 1", "Tubi - Free Movies & TV"]


def test_hostile_app_ids_are_dropped(roku):
    roku.apps = [("12/../x", "appl", "Bad"), ("12", "appl", "Good")]
    assert [a.name for a in client().apps()] == ["Good"]


def test_active_app_names_the_running_app_or_none_at_home(roku):
    roku.active = "Roku"
    assert client().active_app().name == "Roku"
    assert client().active_app().id is None


def test_malformed_or_hostile_xml_is_a_clean_failure(roku, monkeypatch):
    from jarvis.devices import roku as module
    monkeypatch.setattr(module.RokuClient, "_get", lambda self, path: b"<!DOCTYPE x [<!ENTITY a 'b'>]><apps/>")
    with pytest.raises(module.RokuRequestFailed):
        client().apps()
    monkeypatch.setattr(module.RokuClient, "_get", lambda self, path: b"not xml")
    with pytest.raises(module.RokuRequestFailed):
        client().apps()


def test_oversized_responses_are_cut_off(roku, monkeypatch):
    from jarvis.devices import roku as module
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 50)
    with pytest.raises(module.RokuRequestFailed):
        client().apps()


# --- keys -----------------------------------------------------------------------------------------

def test_a_key_is_one_post(roku):
    client().key("Home")
    assert roku.keys() == ["Home"]


def test_key_names_are_validated_case_insensitively(roku):
    client().key("volumeup")
    assert roku.keys() == ["VolumeUp"]


@pytest.mark.parametrize("bad", ["", "Format", "../../launch/12", "Home/../Back", "Lit_a", None])
def test_unknown_keys_send_nothing(roku, bad):
    from jarvis.devices.roku import RokuRequestFailed
    with pytest.raises(RokuRequestFailed):
        client().key(bad)
    assert roku.posts() == []


def test_repeat_sends_that_many_presses_and_is_capped(roku):
    from jarvis.devices.roku import MAX_REPEAT
    client().key("VolumeDown", repeat=3)
    assert roku.keys() == ["VolumeDown"] * 3
    roku.requests.clear()
    client().key("VolumeUp", repeat=10_000)
    assert roku.keys() == ["VolumeUp"] * MAX_REPEAT
    roku.requests.clear()
    client().key("Home", repeat=0)
    assert roku.keys() == ["Home"]


def test_every_documented_key_is_in_the_validated_list():
    from jarvis.devices.roku import KEYS
    for name in ("Home", "Back", "Select", "Up", "Down", "Left", "Right", "Play", "Rev", "Fwd", "InstantReplay",
                 "Info", "VolumeUp", "VolumeDown", "VolumeMute", "PowerOn", "PowerOff", "InputHDMI1",
                 "InputHDMI2", "InputHDMI3", "InputHDMI4", "InputTuner"):
        assert name in KEYS


# --- launch and text ------------------------------------------------------------------------------

def test_launch_posts_the_app_id(roku):
    client().launch("12")
    assert roku.posts() == ["/launch/12"]


@pytest.mark.parametrize("bad", ["", "12/../../keypress/Home", "1 2", "12?x=y", None])
def test_launch_rejects_ids_that_could_change_the_path(roku, bad):
    from jarvis.devices.roku import RokuRequestFailed
    with pytest.raises(RokuRequestFailed):
        client().launch(bad)
    assert roku.posts() == []


def test_text_is_sent_as_literal_keypresses_one_character_at_a_time(roku):
    client().type_text("a b/é")
    assert roku.keys() == ["Lit_a", "Lit_ ", "Lit_b", "Lit_/", "Lit_é"]


def test_text_is_percent_encoded_on_the_wire(roku):
    client().type_text("a b/?")
    assert [p for m, p in roku.requests if m == "POST"] == [
        "/keypress/Lit_a", "/keypress/Lit_%20", "/keypress/Lit_b", "/keypress/Lit_%2F", "/keypress/Lit_%3F"]


def test_text_length_is_bounded_and_empty_text_is_refused(roku):
    from jarvis.devices.roku import MAX_TEXT_CHARS, RokuRequestFailed
    with pytest.raises(RokuRequestFailed):
        client().type_text("x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(RokuRequestFailed):
        client().type_text("")
    assert roku.posts() == []


# --- failure modes --------------------------------------------------------------------------------

def test_control_by_mobile_apps_disabled_is_reported_as_forbidden(roku):
    from jarvis.devices.roku import RokuForbidden
    roku.mode = "limited"
    assert client().device_info().name  # reading still works in Limited mode
    with pytest.raises(RokuForbidden):
        client().key("Home")
    with pytest.raises(RokuForbidden):
        client().launch("12")


def test_a_closed_port_is_unreachable_and_fast(monkeypatch):
    import time
    from jarvis.devices import roku as module
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    import ipaddress
    monkeypatch.setattr(module, "ECP_PORT", port)
    monkeypatch.setattr(module, "ALLOWED_NETWORKS", module.ALLOWED_NETWORKS + (ipaddress.ip_network("127.0.0.0/8"),))
    started = time.monotonic()
    with pytest.raises(module.RokuUnreachable):
        client().device_info()
    assert time.monotonic() - started < module.TIMEOUT_SEC + 1


def test_a_silent_device_times_out_instead_of_hanging(monkeypatch):
    import ipaddress
    import time
    from jarvis.devices import roku as module
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)  # accepts the connection, never answers
    monkeypatch.setattr(module, "ECP_PORT", server.getsockname()[1])
    monkeypatch.setattr(module, "TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(module, "ALLOWED_NETWORKS", module.ALLOWED_NETWORKS + (ipaddress.ip_network("127.0.0.0/8"),))
    started = time.monotonic()
    try:
        with pytest.raises(module.RokuUnreachable):
            client().device_info()
    finally:
        server.close()
    assert time.monotonic() - started < 3


def test_redirects_are_never_followed(roku, monkeypatch):
    """A device answering with a redirect must not move the request to another host."""
    from jarvis.devices import roku as module

    class Redirecting:
        status_code = 302
        headers = {"Location": "http://8.8.8.8/"}
        is_redirect = True

        def iter_content(self, _size):
            return iter(())

        def close(self):
            pass

    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(kwargs)
        return Redirecting()

    monkeypatch.setattr(module.requests.Session, "request", fake_request)
    with pytest.raises(module.RokuRequestFailed):
        client().device_info()
    assert seen["allow_redirects"] is False


def test_system_proxy_settings_are_ignored(roku, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert client().device_info().name == "Living Room TV"


# --- app name resolution --------------------------------------------------------------------------

def _apps(*names):
    from jarvis.devices.roku import App
    return [App(id=str(i), name=n, kind="appl") for i, n in enumerate(names, 1)]


def test_exact_name_beats_a_looser_token_match():
    from jarvis.devices.roku import resolve_app
    app, candidates = resolve_app("plex", _apps("Plex", "Plex Media Server"))
    assert app.name == "Plex" and candidates == []


def test_a_unique_token_match_resolves():
    from jarvis.devices.roku import resolve_app
    app, _ = resolve_app("prime", _apps("Netflix", "Prime Video"))
    assert app.name == "Prime Video"


def test_case_and_punctuation_do_not_matter():
    from jarvis.devices.roku import resolve_app
    app, _ = resolve_app("  DISNEY  plus ", _apps("Disney Plus", "Hulu"))
    assert app.name == "Disney Plus"


def test_ambiguity_returns_candidates_and_never_guesses():
    from jarvis.devices.roku import resolve_app
    app, candidates = resolve_app("plex", _apps("Plex Media Server", "Plex Player"))
    assert app is None
    assert sorted(a.name for a in candidates) == ["Plex Media Server", "Plex Player"]


def test_duplicate_exact_names_are_ambiguous():
    from jarvis.devices.roku import resolve_app
    app, candidates = resolve_app("netflix", _apps("Netflix", "Netflix"))
    assert app is None and len(candidates) == 2


def test_nothing_suitable_returns_nothing():
    from jarvis.devices.roku import resolve_app
    assert resolve_app("crunchyroll", _apps("Netflix")) == (None, [])
    assert resolve_app("", _apps("Netflix")) == (None, [])


def test_an_exact_installed_id_resolves():
    from jarvis.devices.roku import resolve_app
    app, _ = resolve_app("2", _apps("Netflix", "Hulu"))
    assert app.name == "Hulu"


def test_spelling_variations_never_match_a_different_app():
    from jarvis.devices.roku import resolve_app
    assert resolve_app("netflixx", _apps("Netflix", "Hulu"))[0] is None


# --- discovery ------------------------------------------------------------------------------------

def _ssdp_responder(replies):
    """A UDP socket on loopback that answers any datagram with each reply; returns (port, request log, stop)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    log, stop = [], threading.Event()

    def serve():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            log.append(data)
            for reply in replies:
                sock.sendto(reply.encode(), addr)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def finish():
        stop.set()
        thread.join()
        sock.close()

    return sock.getsockname()[1], log, finish


def _reply(location, usn="uuid:roku:ecp:FAKESERIAL1"):
    return ("HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nST: roku:ecp\r\n"
            f"USN: {usn}\r\nLocation: {location}\r\n\r\n")


def test_discovery_sends_an_ecp_m_search_and_reads_the_replies():
    from jarvis.devices.roku import discover
    port, log, finish = _ssdp_responder([_reply("http://192.168.1.50:8060/")])
    try:
        found = discover(timeout=0.5, target=("127.0.0.1", port))
    finally:
        finish()
    assert [(d.host, d.serial) for d in found] == [("192.168.1.50", "FAKESERIAL1")]
    request = log[0].decode()
    assert request.startswith("M-SEARCH * HTTP/1.1") and "roku:ecp" in request and 'MAN: "ssdp:discover"' in request


def test_discovery_ignores_public_and_malformed_replies_and_duplicates():
    from jarvis.devices.roku import discover
    port, _, finish = _ssdp_responder([
        _reply("http://8.8.8.8:8060/"), _reply("http://not-an-ip:8060/"), "garbage",
        _reply("http://192.168.1.50:8060/"), _reply("http://192.168.1.50:8060/"),
        _reply("http://10.1.2.3:8060/", usn="uuid:roku:ecp:OTHER"),
    ])
    try:
        found = discover(timeout=0.5, target=("127.0.0.1", port))
    finally:
        finish()
    assert sorted(d.host for d in found) == ["10.1.2.3", "192.168.1.50"]


def test_discovery_with_no_answers_returns_nothing_within_its_timeout():
    import time
    from jarvis.devices.roku import discover
    port, _, finish = _ssdp_responder([])
    started = time.monotonic()
    try:
        assert discover(timeout=0.3, target=("127.0.0.1", port)) == []
    finally:
        finish()
    assert time.monotonic() - started < 2

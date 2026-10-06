"""The configured TV as a long-lived object: cached apps, remembered identity, recovery when its address moves."""
import pytest


@pytest.fixture(autouse=True)
def _stale_address_fails_at_once(monkeypatch):
    """127.0.0.2 stands for an address the TV has left. Windows takes two seconds to refuse a closed
    loopback port, so refuse it immediately instead."""
    from jarvis.devices import roku
    real = roku.RokuClient._send

    def send(self, method, path, **kwargs):
        if self.host == "127.0.0.2":
            raise roku.RokuUnreachable("The TV did not answer.")
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(roku.RokuClient, "_send", send)


def device(host="127.0.0.1"):
    from jarvis.devices.roku import get_device
    return get_device(host)


def test_one_device_object_per_host(fake_tv):
    assert device() is device()
    assert device() is not device("127.0.0.2")


def test_app_cache_is_empty_until_the_device_has_been_read(fake_tv):
    tv = device()
    assert tv.cached_apps() == ()
    assert fake_tv.requests == []  # reading the cache never touches the network
    tv.apps()
    assert [a.name for a in tv.cached_apps()][:3] == ["HDMI 1", "Live TV", "Netflix"]


def test_warm_up_reads_identity_and_apps_with_gets_only(fake_tv):
    tv = device()
    tv.warm_up()
    assert tv.serial == "FAKESERIAL1"
    assert tv.cached_apps()
    assert fake_tv.posts() == []


def test_warm_up_swallows_an_unreachable_tv(fake_tv):
    fake_tv.close()
    tv = device()
    tv.warm_up()  # must not raise
    assert tv.cached_apps() == ()


def test_unreachable_without_a_known_identity_does_not_search_or_guess(fake_tv, monkeypatch):
    from jarvis.devices import roku
    searched = []
    monkeypatch.setattr(roku, "discover", lambda *a, **k: searched.append(1) or [])
    fake_tv.close()
    with pytest.raises(roku.RokuUnreachable):
        device().call(lambda client: client.device_info())
    assert searched == []


def test_moved_tv_is_found_again_by_its_serial_and_the_command_completes(fake_tv, monkeypatch):
    from jarvis.devices import roku
    tv = device("127.0.0.2")  # nothing listens here: the configured address went stale
    tv.serial = "FAKESERIAL1"
    monkeypatch.setattr(roku, "discover", lambda *a, **k: [roku.Discovered("127.0.0.1", "FAKESERIAL1")])
    tv.call(lambda client: client.key("Home"))
    assert fake_tv.keys() == ["Home"]
    assert tv.take_move_notice() is True
    assert tv.take_move_notice() is False  # reported once


def test_a_different_roku_is_never_adopted(fake_tv, monkeypatch):
    from jarvis.devices import roku
    tv = device("127.0.0.2")
    tv.serial = "FAKESERIAL1"
    monkeypatch.setattr(roku, "discover", lambda *a, **k: [roku.Discovered("127.0.0.1", "SOMEONE-ELSES")])
    with pytest.raises(roku.RokuUnreachable):
        tv.call(lambda client: client.key("Home"))
    assert fake_tv.posts() == []


def test_searching_is_rate_limited(fake_tv, monkeypatch):
    from jarvis.devices import roku
    tv = device("127.0.0.2")
    tv.serial = "FAKESERIAL1"
    searched = []
    monkeypatch.setattr(roku, "discover", lambda *a, **k: searched.append(1) or [])
    for _ in range(3):
        with pytest.raises(roku.RokuUnreachable):
            tv.call(lambda client: client.device_info())
    assert len(searched) == 1


def test_forbidden_is_not_treated_as_a_moved_tv(fake_tv, monkeypatch):
    from jarvis.devices import roku
    searched = []
    monkeypatch.setattr(roku, "discover", lambda *a, **k: searched.append(1) or [])
    fake_tv.mode = "limited"
    tv = device()
    tv.serial = "FAKESERIAL1"
    with pytest.raises(roku.RokuForbidden):
        tv.call(lambda client: client.key("Home"))
    assert searched == []

"""Behaviour of the Windows media layer, driven through a fake backend."""

import pytest

from jarvis.platform.windows import media


class FakeBackend:
    def __init__(self, session=None, accept=True):
        self.session = session
        self.accept = accept
        self.calls = []

    def get_session(self):
        return self.session

    def _cmd(self, name):
        self.calls.append(name)
        return self.accept

    def play(self):
        return self._cmd("play")

    def pause(self):
        return self._cmd("pause")

    def toggle(self):
        return self._cmd("toggle")

    def next(self):
        return self._cmd("next")

    def previous(self):
        return self._cmd("previous")

    def press_media_key(self, key):
        self.calls.append(f"key:{key}")


def _session(status="playing"):
    return media.NowPlaying(title="Song", artist="Artist", status=status, app="Spotify.exe")


@pytest.fixture
def install(monkeypatch):
    def _install(backend):
        monkeypatch.setattr(media, "create_backend", lambda: backend)
        return backend

    return _install


@pytest.mark.unit
def test_now_playing_returns_title_and_artist(install):
    install(FakeBackend(_session()))
    info = media.now_playing()
    assert (info.title, info.artist, info.status) == ("Song", "Artist", "playing")


@pytest.mark.unit
def test_now_playing_is_none_without_a_session(install):
    install(FakeBackend(None))
    assert media.now_playing() is None


@pytest.mark.unit
def test_pause_on_paused_media_does_not_send_a_toggle(install):
    backend = install(FakeBackend(_session("paused")))
    assert media.control("pause").status == "already"
    assert backend.calls == []


@pytest.mark.unit
def test_pause_on_playing_media_pauses_explicitly(install):
    backend = install(FakeBackend(_session("playing")))
    assert media.control("pause").status == "done"
    assert backend.calls == ["pause"]


@pytest.mark.unit
def test_play_on_playing_media_is_a_noop(install):
    backend = install(FakeBackend(_session("playing")))
    assert media.control("play").status == "already"
    assert backend.calls == []


@pytest.mark.unit
def test_play_on_paused_media_plays(install):
    backend = install(FakeBackend(_session("paused")))
    assert media.control("play").status == "done"
    assert backend.calls == ["play"]


@pytest.mark.unit
def test_play_pause_toggles(install):
    backend = install(FakeBackend(_session("paused")))
    assert media.control("play_pause").status == "done"
    assert backend.calls == ["toggle"]


@pytest.mark.unit
@pytest.mark.parametrize("action", ["play", "pause", "play_pause", "next", "previous"])
def test_no_session_reports_no_session_and_sends_nothing(install, action):
    backend = install(FakeBackend(None))
    assert media.control(action).status == "no_session"
    assert backend.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["next", "previous"])
def test_skip_uses_session_command(install, action):
    backend = install(FakeBackend(_session()))
    assert media.control(action).status == "done"
    assert backend.calls == [action]


@pytest.mark.unit
@pytest.mark.parametrize("action", ["next", "previous"])
def test_skip_falls_back_to_media_key_when_session_rejects(install, action):
    backend = install(FakeBackend(_session(), accept=False))
    assert media.control(action).status == "done"
    assert backend.calls == [action, f"key:{action}"]


@pytest.mark.unit
def test_pause_rejected_by_session_is_reported_not_faked(install):
    backend = install(FakeBackend(_session("playing"), accept=False))
    assert media.control("pause").status == "unsupported"
    assert not any(c.startswith("key:") for c in backend.calls)


@pytest.mark.unit
def test_unknown_action_raises(install):
    install(FakeBackend(_session()))
    with pytest.raises(ValueError):
        media.control("rewind")

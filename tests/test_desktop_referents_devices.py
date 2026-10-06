"""Device, media and clipboard actions leave a desktop referent, through the central tool path.

Only names of applications and devices, never content: no track titles, clipboard text or TV text.
See ``src/jarvis/memory/desktop_referents.spec.md``."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "windows_control"))

from jarvis.memory.desktop_referents import get_desktop_referents  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_referents():
    get_desktop_referents().clear()
    yield
    get_desktop_referents().clear()


def others():
    return get_desktop_referents().others(max_age_sec=300)


def shown():
    from jarvis.memory.desktop_referents import format_for_model, others_for_request
    return format_for_model([], others=others()) + json.dumps(others_for_request(others()))


def run(cfg, tool, **args):
    from jarvis.tools.registry import run_tool_with_retries
    return run_tool_with_retries(db=None, cfg=cfg, tool_name=tool, tool_args=args, system_prompt="",
                                 original_prompt="", redacted_text="", max_retries=1, quiet=True)


# --- TV --------------------------------------------------------------------------------------------

@pytest.fixture
def tv(fake_tv, mock_config):
    from jarvis.tools.builtin.tv_control import TvControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    mock_config.roku_host = "127.0.0.1"
    BUILTIN_TOOLS["tvControl"] = TvControlTool()
    yield fake_tv
    BUILTIN_TOOLS.pop("tvControl", None)


@pytest.mark.unit
class TestTv:
    def test_a_key_press_records_the_tv_and_the_key(self, tv, mock_config):
        assert run(mock_config, "tvControl", action="key", key="PowerOn").success
        (entry,) = others()
        assert (entry.kind, entry.device, entry.tool, entry.last_action) == ("device", "tv", "tvControl",
                                                                              "key PowerOn")

    def test_a_status_read_records_the_tv(self, tv, mock_config):
        assert run(mock_config, "tvControl", action="status").success
        assert [(e.device, e.last_action) for e in others()] == [("tv", "status")]

    def test_typed_text_and_app_names_are_never_kept(self, tv, mock_config):
        run(mock_config, "tvControl", action="type", text="secret search")
        run(mock_config, "tvControl", action="launch", app="Netflix")
        assert "secret" not in shown() and "Netflix" not in shown()

    def test_a_failure_records_nothing(self, tv, mock_config):
        assert not run(mock_config, "tvControl", action="key", key="Format").success
        assert others() == []


# --- media -----------------------------------------------------------------------------------------

@pytest.fixture
def player(monkeypatch, mock_config):
    from test_media import FakeBackend
    from jarvis.platform.windows import media
    fake = FakeBackend(media.NowPlaying(title="Secret Song", artist="Hidden Artist", status="playing",
                                        app="AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"))
    monkeypatch.setattr(media, "create_backend", lambda: fake)
    monkeypatch.setattr(media, "app_display_name",
                        lambda aumid: "Apple Music" if aumid.startswith("AppleInc.") else aumid)
    mock_config.windows_tools_enabled = True
    return fake


@pytest.mark.unit
class TestMedia:
    def test_pausing_records_the_player_as_paused(self, player, mock_config):
        assert run(mock_config, "mediaControl", action="pause").success
        (entry,) = others()
        assert (entry.kind, entry.application, entry.status, entry.tool) == ("media", "Apple Music", "paused",
                                                                              "mediaControl")

    def test_now_playing_records_the_player_but_never_the_track(self, player, mock_config):
        assert run(mock_config, "mediaControl", action="now_playing").success
        assert [(e.application, e.status) for e in others()] == [("Apple Music", "playing")]
        assert "Secret" not in shown() and "Hidden" not in shown()

    def test_no_session_records_nothing(self, player, mock_config):
        player.session = None
        run(mock_config, "mediaControl", action="pause")
        run(mock_config, "mediaControl", action="now_playing")
        assert others() == []


@pytest.mark.unit
def test_an_unknown_player_identifier_reads_as_its_application():
    from jarvis.platform.windows.media import readable_app_id
    assert readable_app_id("AppleInc.AppleMusicWin_nzyj5cx40ttqa!App") == "AppleMusicWin"
    assert readable_app_id("Spotify.exe") == "Spotify"
    assert readable_app_id("Chrome") == "Chrome"
    assert readable_app_id("") == ""


# --- clipboard -------------------------------------------------------------------------------------

class FakeClipboard:
    def __init__(self, text="private words"):
        self.text, self.kind, self.counter = text, "text", 1

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text, self.kind = text, "text"
        self.counter += 1

    def sequence(self):
        return self.counter

    def content_type(self):
        return self.kind


@pytest.fixture
def clipboard(monkeypatch, mock_config):
    from jarvis.platform.windows import input_control
    fake = FakeClipboard()
    input_control.set_clipboard_backend(fake)
    monkeypatch.setattr(input_control, "send_chord", lambda chord, sender=None: {
        "action": "hotkey_sent", "keys": "+".join(chord)})
    mock_config.windows_tools_enabled = True
    yield fake
    input_control.set_clipboard_backend(None)


@pytest.mark.unit
class TestClipboard:
    def test_writing_records_text_never_the_text_itself(self, clipboard, mock_config):
        assert run(mock_config, "inputControl", action="clipboard_write", text="my private note").success
        assert [(e.kind, e.content_type, e.tool) for e in others()] == [("clipboard", "text", "inputControl")]
        assert "private" not in shown()

    def test_reading_records_the_content_type(self, clipboard, mock_config):
        clipboard.kind = "image"
        assert run(mock_config, "inputControl", action="clipboard_read").success
        assert [e.content_type for e in others()] == ["image"]

    def test_a_hotkey_that_copies_shows_what_the_clipboard_now_holds(self, clipboard, mock_config):
        assert run(mock_config, "inputControl", action="hotkey", keys="ctrl+c").success
        assert others() == []  # nothing changed yet: the application copies after the chord
        clipboard.kind, clipboard.counter = "files", clipboard.counter + 1
        assert [e.content_type for e in others()] == ["files"]

    def test_a_hotkey_that_does_not_touch_the_clipboard_shows_nothing(self, clipboard, mock_config):
        assert run(mock_config, "inputControl", action="hotkey", keys="win+d").success
        assert others() == []

"""playMusic tool: opt-in registration, catalogue and library requests, honest failures, privacy."""
import json

import pytest

from jarvis.apple_music import catalogue
from jarvis.platform.windows import apple_music as am
from jarvis.platform.windows.media import NowPlaying

SONG = {"trackId": 1440806326, "collectionId": 1440806041, "artistId": 3296287, "artistName": "Queen",
        "collectionName": "A Night at the Opera", "trackName": "Bohemian Rhapsody"}
ARTIST = {"artistId": 159260351, "artistName": "Taylor Swift"}


class Player:
    """Records what the tool asked the player layer to do and answers with a set outcome."""

    def __init__(self, outcome=None, installed=True):
        self.outcome = outcome or am.PlayOutcome("playing", NowPlaying("Bohemian Rhapsody", "Queen — A Night "
                                                                       "at the Opera", "playing", am.APP_ID))
        self.is_installed = installed
        self.targets = []
        self.playlists = []

    def installed(self):
        return self.is_installed

    def play_target(self, target):
        self.targets.append(target)
        return self.outcome

    def play_playlist(self, name):
        self.playlists.append(name)
        return self.outcome


@pytest.fixture
def player(monkeypatch):
    fake = Player()
    monkeypatch.setattr(am, "installed", fake.installed)
    monkeypatch.setattr(am, "play_target", fake.play_target)
    monkeypatch.setattr(am, "play_playlist", fake.play_playlist)
    monkeypatch.setattr(am, "windows_region", lambda: "US")
    return fake


@pytest.fixture
def http(monkeypatch):
    calls = []
    state = {"results": [SONG], "error": None}

    def get(url, params, timeout):
        calls.append(dict(params))
        if state["error"]:
            raise state["error"]
        return 200, json.dumps({"results": state["results"]}).encode()

    monkeypatch.setattr(catalogue, "_http_get", get)
    state["calls"] = calls
    return state


@pytest.fixture
def cfg(mock_config):
    mock_config.apple_music_enabled = True
    return mock_config


def run(cfg, **args):
    from jarvis.tools.builtin.apple_music import AppleMusicTool
    from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
    BUILTIN_TOOLS.setdefault("playMusic", AppleMusicTool())
    return run_tool_with_retries(db=None, cfg=cfg, tool_name="playMusic", tool_args=args, system_prompt="",
                                 original_prompt="", redacted_text="", max_retries=1, quiet=True)


@pytest.fixture(autouse=True)
def _clean_registry():
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS.pop("playMusic", None)
    yield
    BUILTIN_TOOLS.pop("playMusic", None)


# --- requests ----------------------------------------------------------------------------------------

def test_a_song_is_looked_up_then_played_and_the_reply_names_it(cfg, player, http):
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert result.success
    assert "Bohemian Rhapsody" in result.reply_text and "Queen" in result.reply_text
    target = player.targets[0]
    assert (target.kind, target.track, target.page_title) == ("song", "Bohemian Rhapsody", "A Night at the Opera")
    assert target.uri == "musics://music.apple.com/us/album/1440806041?i=1440806326"


def test_an_artist_request_opens_the_artist_page(cfg, player, http):
    http["results"] = [ARTIST]
    result = run(cfg, query="taylor swift", kind="artist")
    assert result.success and http["calls"][0]["entity"] == "musicArtist"
    assert player.targets[0].uri.endswith("/artist/159260351")


def test_a_missing_kind_is_treated_as_a_song(cfg, player, http):
    run(cfg, query="bohemian rhapsody")
    assert http["calls"][0]["entity"] == "song"


def test_the_store_country_setting_reaches_the_lookup_and_the_link(cfg, player, http):
    cfg.apple_music_country = "GB"
    run(cfg, query="bohemian rhapsody", kind="song")
    assert http["calls"][0]["country"] == "GB"
    assert "/gb/album/" in player.targets[0].uri


def test_a_library_playlist_never_calls_the_catalogue(cfg, player, http):
    result = run(cfg, query="road trip", kind="playlist")
    assert result.success and player.playlists == ["road trip"] and http["calls"] == []


def test_a_successful_play_is_remembered_for_follow_ups(cfg, player, http):
    from jarvis.memory.desktop_referents import get_desktop_referents
    store = get_desktop_referents()
    store.clear()
    run(cfg, query="bohemian rhapsody", kind="song")
    media = [o for o in store.others(max_age_sec=300) if o.kind == "media"]
    assert media and media[0].application == am.APP_NAME and media[0].status == "playing"


# --- honest failures ---------------------------------------------------------------------------------

def test_the_app_not_installed_fails_before_anything_is_sent(cfg, player, http):
    player.is_installed = False
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert not result.success and "Microsoft Store" in result.reply_text
    assert http["calls"] == [] and player.targets == []


def test_being_offline_says_so_and_plays_nothing(cfg, player, http):
    http["error"] = OSError("no network")
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert not result.success and "internet" in result.reply_text.lower()
    assert player.targets == []


def test_nothing_in_the_catalogue_plays_nothing(cfg, player, http):
    http["results"] = []
    result = run(cfg, query="zzzz", kind="song")
    assert not result.success and player.targets == []


def test_an_empty_query_is_refused(cfg, player, http):
    result = run(cfg, query="  ", kind="song")
    assert not result.success and http["calls"] == []


def test_several_playlists_are_offered_back_to_choose_from(cfg, player, http):
    player.outcome = am.PlayOutcome("ambiguous", candidates=("Road Trip", "Road Trip Classics"))
    result = run(cfg, query="trip", kind="playlist")
    assert not result.success
    assert "Road Trip" in result.reply_text and "Road Trip Classics" in result.reply_text


@pytest.mark.parametrize("status", ["not_found", "no_control", "not_started", "not_installed", "timed_out",
                                    "busy"])
def test_player_failures_are_reported_as_failures(cfg, player, http, status):
    player.outcome = am.PlayOutcome(status)
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert not result.success and result.reply_text


def test_something_else_playing_is_reported_truthfully(cfg, player, http):
    player.outcome = am.PlayOutcome("other_playing", NowPlaying("Other Song", "Someone", "playing", am.APP_ID))
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert "Other Song" in result.reply_text


def test_a_player_timeout_is_a_failure_not_a_crash(cfg, player, http, monkeypatch):
    def stuck(target):
        raise TimeoutError("took too long")
    monkeypatch.setattr(am, "play_target", stuck)
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert not result.success and "in time" in result.reply_text


def test_a_control_failure_is_not_reported_as_a_timeout(cfg, player, http, monkeypatch):
    def broken(target):
        raise OSError("control went away")
    monkeypatch.setattr(am, "play_target", broken)
    result = run(cfg, query="bohemian rhapsody", kind="song")
    assert not result.success and "in time" not in result.reply_text


# --- privacy and safety ------------------------------------------------------------------------------

def test_the_search_text_and_titles_never_reach_the_debug_log(cfg, player, http, monkeypatch):
    logged = []
    import jarvis.tools.builtin.apple_music as module
    monkeypatch.setattr(module, "debug_log", lambda message, *a, **k: logged.append(message))
    monkeypatch.setattr(catalogue, "debug_log", lambda message, *a, **k: logged.append(message))
    run(cfg, query="bohemian rhapsody", kind="song")
    text = " ".join(logged).casefold()
    assert logged and "bohemian" not in text and "queen" not in text


def test_playing_music_is_routine_and_results_are_outside_content(cfg):
    from jarvis.tools.builtin.apple_music import AppleMusicTool
    from jarvis.tools.confirmation import SafetyTier
    tool = AppleMusicTool()
    assert tool.classify_safety({"query": "x", "kind": "song"}, cfg).tier == SafetyTier.SAFE
    assert tool.returns_outside_content_for({"query": "x", "kind": "song"}) is True


def test_the_router_reads_what_the_tool_is_for_in_the_first_120_characters():
    from jarvis.tools.builtin.apple_music import AppleMusicTool
    head = AppleMusicTool().description[:120]
    assert "Apple Music" in head and "mediaControl" in AppleMusicTool().description


# --- registration ------------------------------------------------------------------------------------

def test_the_tool_is_off_by_default(mock_config):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_apple_music_tool
    configure_apple_music_tool(mock_config, platform="win32")
    assert "playMusic" not in BUILTIN_TOOLS


def test_the_tool_is_offered_when_enabled_on_windows_and_removed_when_disabled(cfg):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_apple_music_tool
    configure_apple_music_tool(cfg, platform="win32")
    assert "playMusic" in BUILTIN_TOOLS
    cfg.apple_music_enabled = False
    configure_apple_music_tool(cfg, platform="win32")
    assert "playMusic" not in BUILTIN_TOOLS


@pytest.mark.parametrize("platform,windows_tools", [("linux", True), ("darwin", True), ("win32", False)])
def test_the_tool_needs_windows_and_the_windows_tools(cfg, platform, windows_tools):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_apple_music_tool
    cfg.windows_tools_enabled = windows_tools
    configure_apple_music_tool(cfg, platform=platform)
    assert "playMusic" not in BUILTIN_TOOLS

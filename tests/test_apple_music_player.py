"""Apple Music player layer: opening catalogue items and library playlists and starting them, against a
scripted app window (``fake_apple_music``). Never touches the real app."""
import pytest

from fake_apple_music import APP, FakeDesktop, album_page, artist_page
from jarvis.platform.windows import apple_music as am
from jarvis.platform.windows.media import NowPlaying

ALBUM_URI = "musics://music.apple.com/us/album/111"
SONG_URI = ALBUM_URI + "?i=222"
ARTIST_URI = "musics://music.apple.com/us/artist/333"
ALBUM = "A Night at the Opera"
TRACKS = ["Death on Two Legs", "Bohemian Rhapsody", "Bohemian Rhapsody (Live)", "God Save the Queen"]


def song(title="Bohemian Rhapsody", uri=SONG_URI):
    return am.Target(kind="song", uri=uri, page_title=ALBUM, track=title, artist="Queen")


def album(title=ALBUM, uri=ALBUM_URI):
    return am.Target(kind="album", uri=uri, page_title=title, artist="Queen")


def artist(name="Queen"):
    return am.Target(kind="artist", uri=ARTIST_URI, page_title=name, artist=name)


def desktop(**kwargs):
    page = album_page(ALBUM, "Queen", TRACKS)
    pages = {SONG_URI: page, ALBUM_URI: page, ARTIST_URI: artist_page("Queen", "Under Pressure")}
    pages.update(kwargs.pop("pages", {}))
    return FakeDesktop(pages, **kwargs)


# --- catalogue items ---------------------------------------------------------------------------------

def test_a_song_is_opened_and_that_exact_track_starts():
    d = desktop()
    outcome = am.play_target_with(d, song())
    assert d.opened == [SONG_URI]
    assert outcome.status == "playing"
    assert outcome.now_playing.title == "Bohemian Rhapsody"


def test_a_song_title_that_differs_only_in_case_and_spacing_still_matches():
    outcome = am.play_target_with(desktop(), song("  bohemian   RHAPSODY "))
    assert outcome.status == "playing" and outcome.now_playing.title == "Bohemian Rhapsody"


def test_an_album_starts_from_its_header_play_button():
    outcome = am.play_target_with(desktop(), album())
    assert outcome.status == "playing"
    assert outcome.now_playing.title == TRACKS[0]


def test_an_artist_starts_from_the_artist_page_play_button():
    outcome = am.play_target_with(desktop(), artist())
    assert outcome.status == "playing" and outcome.now_playing.title == "Under Pressure"


def test_a_page_that_is_still_showing_another_album_is_never_played():
    other = album_page("News of the World", "Queen", ["We Will Rock You"])
    d = desktop(pages={ALBUM_URI: other})
    outcome = am.play_target_with(d, album(), page_timeout=1.0)
    assert outcome.status == "not_found"
    assert d.now is None


def test_a_song_missing_from_the_album_page_is_reported_not_guessed():
    d = desktop()
    outcome = am.play_target_with(d, song("Love of My Life"), page_timeout=1.0)
    assert outcome.status == "not_found" and d.now is None


def test_a_track_further_down_a_long_album_is_scrolled_into_view():
    page = album_page(ALBUM, "Queen", TRACKS)
    page["hidden_tracks"] = {(5, 4)}  # the last row is not realised until the list scrolls
    d = desktop(pages={SONG_URI: page})
    outcome = am.play_target_with(d, song("God Save the Queen"))
    assert outcome.status == "playing" and outcome.now_playing.title == "God Save the Queen"
    assert any(action[0] == "scroll" for action in d.actions)


def test_a_menu_that_does_not_open_fails_honestly():
    d = desktop(menu_opens=False)
    outcome = am.play_target_with(d, song())
    assert outcome.status == "no_control" and d.now is None


def test_playback_that_loads_paused_is_resumed_once():
    d = desktop(starts_paused=True)
    outcome = am.play_target_with(d, album())
    assert outcome.status == "playing" and d.resumes == 1


def test_playback_that_never_starts_is_reported():
    d = desktop(plays=False)
    outcome = am.play_target_with(d, album(), confirm_timeout=1.0)
    assert outcome.status == "not_started"


def test_another_track_playing_afterwards_is_reported_as_such():
    d = desktop(plays=False, now=NowPlaying("Something Else", "Someone — Elsewhere", "playing", APP))
    outcome = am.play_target_with(d, song(), confirm_timeout=1.0)
    assert outcome.status == "other_playing"
    assert outcome.now_playing.title == "Something Else"


def test_the_app_not_being_installed_opens_nothing():
    d = desktop(installed=False)
    outcome = am.play_target_with(d, song())
    assert outcome.status == "not_installed" and d.opened == []


@pytest.mark.parametrize("uri", ["https://evil.example/x", "musics://music.apple.com/us/album/1 & calc",
                                 "music://music.apple.com/us/album/1"])
def test_only_well_formed_apple_music_links_are_opened(uri):
    d = desktop()
    with pytest.raises(ValueError):
        am.play_target_with(d, song(uri=uri))
    assert d.opened == []


# --- library playlists -------------------------------------------------------------------------------

def playlists_desktop(**kwargs):
    from fake_apple_music import header, track_row
    names = ["Favourite Songs", "Road Trip", "Road Trip Classics", "Focus"]
    pages = {name: {"controls": header((5, 0), name, play_id="PlayButton") + track_row((5, 1), 1, f"{name} opener"),
                    "artist": "Various", "album": name, "tracks": [f"{name} opener"]} for name in names}
    return FakeDesktop(playlists=names, playlist_pages=pages, **kwargs)


def test_a_paused_track_from_elsewhere_is_never_resumed_in_place_of_the_playlist():
    d = playlists_desktop(plays=False, now=NowPlaying("Old Song", "Someone — Old Album", "paused", APP))
    outcome = am.play_playlist_with(d, "Focus", confirm_timeout=2.0)
    assert outcome.status == "not_started" and d.resumes == 0


def test_a_library_playlist_is_found_in_the_sidebar_and_played():
    d = playlists_desktop()
    outcome = am.play_playlist_with(d, "focus")
    assert outcome.status == "playing" and outcome.now_playing.title == "Focus opener"
    assert d.opened == []  # a library playlist needs no link and no network lookup


def test_the_sidebar_is_left_as_it_was_found():
    d = playlists_desktop()
    am.play_playlist_with(d, "Focus")
    assert d.expanded is False
    d = playlists_desktop()
    d.expanded = True
    am.play_playlist_with(d, "Focus")
    assert d.expanded is True and not any(a[0] == "collapse" for a in d.actions)


def test_an_exact_playlist_name_wins_over_longer_names():
    outcome = am.play_playlist_with(playlists_desktop(), "Road Trip")
    assert outcome.status == "playing" and outcome.now_playing.title == "Road Trip opener"


def test_several_matching_playlists_return_the_candidates_and_play_nothing():
    d = playlists_desktop()
    outcome = am.play_playlist_with(d, "trip")
    assert outcome.status == "ambiguous" and d.now is None
    assert set(outcome.candidates) == {"Road Trip", "Road Trip Classics"}


def test_an_unknown_playlist_plays_nothing():
    d = playlists_desktop()
    outcome = am.play_playlist_with(d, "Jazz Nights")
    assert outcome.status == "not_found" and d.now is None


def test_a_closed_app_is_started_before_looking_for_playlists():
    d = playlists_desktop(running=False)
    outcome = am.play_playlist_with(d, "Focus")
    assert d.opened == [am.LAUNCH_URI]
    assert outcome.status == "playing"


# --- store country -----------------------------------------------------------------------------------

@pytest.mark.parametrize("configured,region,expected", [
    ("gb", "US", "gb"), ("", "DE", "de"), ("", "", "us"), ("not a country", "FR", "fr"), ("", "001", "us")])
def test_the_store_country_comes_from_settings_then_the_windows_region(configured, region, expected):
    assert am.store_country(configured, region=lambda: region) == expected

"""Apple Music catalogue lookup through the public iTunes Search API, against a fake HTTP layer."""
import json

import pytest

from jarvis.apple_music import catalogue


class FakeHttp:
    def __init__(self, body=None, status=200, error=None):
        self.body = body if body is not None else {"resultCount": 0, "results": []}
        self.status = status
        self.error = error
        self.calls = []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params), timeout))
        if self.error:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, raw


SONG = {"wrapperType": "track", "kind": "song", "artistId": 3296287, "collectionId": 1440806041,
        "trackId": 1440806326, "artistName": "Queen", "collectionName": "A Night at the Opera",
        "trackName": "Bohemian Rhapsody"}
ALBUM = {"wrapperType": "collection", "collectionType": "Album", "artistId": 3296287,
         "collectionId": 1440806041, "artistName": "Queen", "collectionName": "A Night at the Opera"}
ARTIST = {"wrapperType": "artist", "artistType": "Artist", "artistName": "Taylor Swift", "artistId": 159260351}


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(catalogue, "_http_get", fake)
    return fake


@pytest.mark.parametrize("kind,entity", [("song", "song"), ("album", "album"), ("artist", "musicArtist")])
def test_each_kind_searches_the_matching_catalogue_entity(http, kind, entity):
    catalogue.search("anything", kind, "gb")
    url, params, timeout = http.calls[0]
    assert url == catalogue.SEARCH_URL and url.startswith("https://")
    assert params["entity"] == entity and params["media"] == "music"
    assert params["country"] == "GB" and params["term"] == "anything"
    assert timeout <= catalogue.TIMEOUT_SEC


def test_a_song_becomes_a_link_to_its_track_on_the_album(http):
    http.body = {"results": [SONG]}
    item = catalogue.search("bohemian rhapsody", "song", "us")
    assert (item.kind, item.title, item.artist, item.album) == ("song", "Bohemian Rhapsody", "Queen",
                                                                 "A Night at the Opera")
    assert item.app_uri("us") == "musics://music.apple.com/us/album/1440806041?i=1440806326"


def test_an_album_and_an_artist_link_to_their_pages(http):
    http.body = {"results": [ALBUM]}
    assert catalogue.search("a night at the opera", "album", "us").app_uri("gb") == \
        "musics://music.apple.com/gb/album/1440806041"
    http.body = {"results": [ARTIST]}
    item = catalogue.search("taylor swift", "artist", "us")
    assert item.title == "Taylor Swift"
    assert item.app_uri("us") == "musics://music.apple.com/us/artist/159260351"


def test_results_without_usable_ids_are_skipped(http):
    http.body = {"results": [dict(SONG, trackId="1; rm"), dict(SONG, collectionId=None), SONG]}
    item = catalogue.search("bohemian rhapsody", "song", "us")
    assert item.app_uri("us").endswith("?i=1440806326")


def test_no_results_returns_nothing(http):
    assert catalogue.search("zzzz", "song", "us") is None


def test_a_long_query_is_trimmed_before_it_is_sent(http):
    catalogue.search("x" * 1000, "song", "us")
    assert len(http.calls[0][1]["term"]) == catalogue.MAX_QUERY_CHARS


@pytest.mark.parametrize("kind", ["playlist", "", "video"])
def test_kinds_the_catalogue_cannot_search_send_nothing(http, kind):
    with pytest.raises(ValueError):
        catalogue.search("x", kind, "us")
    assert http.calls == []


def test_an_empty_query_sends_nothing(http):
    with pytest.raises(ValueError):
        catalogue.search("   ", "song", "us")
    assert http.calls == []


@pytest.mark.parametrize("fake", [FakeHttp(error=OSError("offline")), FakeHttp(status=503),
                                  FakeHttp(body=b"<html>not json</html>"), FakeHttp(body={"results": "x"})])
def test_network_and_format_failures_raise_unavailable(monkeypatch, fake):
    monkeypatch.setattr(catalogue, "_http_get", fake)
    with pytest.raises(catalogue.CatalogueUnavailable):
        catalogue.search("bohemian rhapsody", "song", "us")


def test_long_names_are_clipped(http):
    http.body = {"results": [dict(SONG, trackName="y" * 5000)]}
    assert len(catalogue.search("y", "song", "us").title) <= catalogue.MAX_NAME_CHARS

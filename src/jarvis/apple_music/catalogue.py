"""Find a song, album or artist in the Apple Music catalogue through the public iTunes Search API.

No account, key or developer token: one HTTPS GET to ``itunes.apple.com`` with the search text, the kind
and the store country. Only the opt-in ``playMusic`` tool calls this. Knows nothing about tools, the
reply engine or the OS. The search text and the names that come back are never logged.
See ``apple_music.spec.md``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ..debug import debug_log

SEARCH_URL = "https://itunes.apple.com/search"
TIMEOUT_SEC = 5.0
MAX_RESPONSE_BYTES = 512 * 1024
MAX_QUERY_CHARS = 200
MAX_NAME_CHARS = 200
RESULT_LIMIT = 5
# The catalogue entity each kind is searched as. Playlists are not in the public search: library playlists
# are found in the app itself.
ENTITIES = {"song": "song", "album": "album", "artist": "musicArtist"}
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_LINK_BASE = "musics://music.apple.com"


class CatalogueUnavailable(RuntimeError):
    """The catalogue could not be reached or answered with something unusable."""


@dataclass(frozen=True)
class CatalogueItem:
    kind: str
    title: str
    artist: str
    album: str
    track_id: Optional[int]
    collection_id: Optional[int]
    artist_id: Optional[int]

    def app_uri(self, country: str) -> str:
        """The ``musics://`` link that opens this item in the Apple Music app, built only from numeric ids."""
        store = country.lower() if _COUNTRY_RE.match(country or "") else "us"
        if self.kind == "song":
            return f"{_LINK_BASE}/{store}/album/{self.collection_id}?i={self.track_id}"
        if self.kind == "album":
            return f"{_LINK_BASE}/{store}/album/{self.collection_id}"
        return f"{_LINK_BASE}/{store}/artist/{self.artist_id}"


def _requests_get(url: str, params: Dict[str, Any], timeout: float) -> Tuple[int, bytes]:
    import requests

    with requests.get(url, params=params, timeout=(min(3.0, timeout), timeout), allow_redirects=False,
                      stream=True, headers={"Accept": "application/json"}) as response:
        body = b""
        for chunk in response.iter_content(65536):
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise CatalogueUnavailable("The catalogue answer was too large.")
        return response.status_code, body


# Replaced in tests; the signature is (url, params, timeout) -> (status code, body bytes).
_http_get: Callable[[str, Dict[str, Any], float], Tuple[int, bytes]] = _requests_get


def _id(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _name(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_NAME_CHARS]


def _item(kind: str, row: Any) -> Optional[CatalogueItem]:
    if not isinstance(row, dict):
        return None
    item = CatalogueItem(
        kind=kind,
        title=_name(row.get({"song": "trackName", "album": "collectionName", "artist": "artistName"}[kind])),
        artist=_name(row.get("artistName")), album=_name(row.get("collectionName")),
        track_id=_id(row.get("trackId")), collection_id=_id(row.get("collectionId")),
        artist_id=_id(row.get("artistId")))
    needed = {"song": (item.track_id, item.collection_id), "album": (item.collection_id,),
              "artist": (item.artist_id,)}[kind]
    if not item.title or any(value is None for value in needed):
        return None
    return item


def search(query: str, kind: str, country: str) -> Optional[CatalogueItem]:
    """The best catalogue match for ``query`` as ``kind`` (song, album or artist), or ``None``.

    Raises ``ValueError`` for an empty query or a kind the catalogue cannot search (nothing is sent), and
    ``CatalogueUnavailable`` when the service cannot be reached or answers with something unusable."""
    if kind not in ENTITIES:
        raise ValueError(f"The catalogue cannot search for {kind or 'that'}.")
    term = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    if not term:
        raise ValueError("Say what to play.")
    store = country.upper() if _COUNTRY_RE.match(country or "") else "US"
    params = {"term": term, "media": "music", "entity": ENTITIES[kind], "country": store,
              "limit": RESULT_LIMIT}
    debug_log(f"apple music catalogue search kind={kind} store={store}", "music")
    try:
        status, body = _http_get(SEARCH_URL, params, TIMEOUT_SEC)
    except CatalogueUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - every transport failure means the catalogue is unreachable
        debug_log(f"apple music catalogue unreachable ({type(exc).__name__})", "music")
        raise CatalogueUnavailable("The Apple Music catalogue could not be reached.") from None
    if status != 200:
        debug_log(f"apple music catalogue answered HTTP {status}", "music")
        raise CatalogueUnavailable("The Apple Music catalogue did not answer the search.")
    try:
        results = json.loads(body.decode("utf-8")).get("results")
    except (ValueError, UnicodeDecodeError, AttributeError):
        results = None
    if not isinstance(results, list):
        raise CatalogueUnavailable("The Apple Music catalogue answered in an unexpected format.")
    for row in results:
        item = _item(kind, row)
        if item is not None:
            return item
    debug_log(f"apple music catalogue had no usable {kind} ({len(results)} results)", "music")
    return None

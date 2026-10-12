"""Play a song, album, artist or library playlist in the Apple Music app on Windows.

Registered only on Windows with the Windows tools on and ``apple_music_enabled`` true
(``registry.configure_apple_music_tool``). Catalogue lookup is in ``jarvis.apple_music.catalogue`` and driving
the app in ``jarvis.platform.windows.apple_music``; this tool turns them into the strings the model sees.
Starting music is routine and needs no confirmation. See ``apple_music/apple_music.spec.md``.
"""
from typing import Any, Dict, Optional

from ...apple_music import catalogue
from ...debug import debug_log
from ...platform.windows import apple_music as player
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

_KINDS = ["song", "album", "artist", "playlist"]
_NOT_INSTALLED = "The Apple Music app is not installed. Install Apple Music from the Microsoft Store."
_OFFLINE = ("Could not reach Apple Music to look that up. Check the internet connection. Your own playlists can "
            "still be started by name.")
_FAILURES = {
    "not_installed": _NOT_INSTALLED,
    "no_window": "The Apple Music app did not open.",
    "not_found": "Apple Music opened, but the {what} did not appear in the app.",
    "no_control": "Apple Music opened the {what}, but its Play control could not be found.",
    "not_started": "Apple Music opened the {what}, but playback did not start. Check that you are signed in "
                   "to Apple Music.",
    "timed_out": "Apple Music did not respond in time, so playback could not be started.",
    "busy": "Apple Music is still busy with the previous request. Try again in a moment.",
}
_TIMED_OUT = _FAILURES["timed_out"]


def _fail(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)


def _remember_player() -> None:
    """Apple Music is what "pause it" is about (``memory/desktop_referents.spec.md``); never the track."""
    try:
        from ...memory.desktop_referents import get_desktop_referents
        get_desktop_referents().record_media(player.APP_NAME, "playing")
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f"apple music referent not recorded ({type(exc).__name__})", "music")


def _track(now) -> str:
    artist = now.artist.split(" — ")[0].strip() if now.artist else ""
    return f"{now.title} by {artist}" if artist else now.title


class AppleMusicTool(Tool):
    @property
    def name(self) -> str:
        return "playMusic"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters, so "play a named thing in Apple Music" comes first.
        return (
            "Play or put on a song, artist, album or playlist by name in Apple Music ('play Hello', 'put on Adele'). "
            "NOT to open apps. Finds the item and starts it in the Apple Music app. "
            "NOT for pause, resume, skip or what is playing: use mediaControl. NOT for opening the Apple Music "
            "app: use appControl. NOT for loudness: use systemVolume."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to play as the user named it: the song, album, artist or playlist "
                                         "name, with the artist too when the user said it (e.g. 'bohemian "
                                         "rhapsody queen')."},
                "kind": {"type": "string", "enum": _KINDS,
                         "description": "song for one track; album; artist for music by a singer or band in "
                                        "general; playlist for one of the user's own playlists."},
            },
            "required": ["query", "kind"],
        }

    def returns_outside_content_for(self, args: Optional[Dict[str, Any]]) -> bool:
        # Titles and artist names come from Apple's catalogue and the app.
        return True

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args if isinstance(args, dict) else {}
        query = " ".join(str(args.get("query") or "").split())
        kind = str(args.get("kind") or "").strip().lower()
        kind = kind if kind in _KINDS else "song"
        if not query:
            return _fail("Say what to play, such as a song, album, artist or playlist name.")
        debug_log(f"playMusic kind={kind}", "music")
        try:
            if not player.installed():
                context.user_print("⚠️ Apple Music is not installed")
                return _fail(_NOT_INSTALLED)
            if kind == "playlist":
                context.user_print("🎵 Looking for your playlist in Apple Music")
                outcome = player.play_playlist(query)
                what = "playlist"
            else:
                target = self._lookup(query, kind, context)
                if isinstance(target, ToolExecutionResult):
                    return target
                context.user_print(f"🎵 Opening {target.track or target.page_title} in Apple Music")
                outcome = player.play_target(target)
                what = kind
        except TimeoutError:
            debug_log("playMusic gave up waiting for the app", "music")
            context.user_print("⚠️ Apple Music did not respond in time")
            return _fail(_TIMED_OUT)
        except OSError as exc:
            debug_log(f"playMusic could not use the app's controls ({type(exc).__name__})", "music")
            context.user_print("⚠️ Could not control Apple Music")
            return _fail("Apple Music's controls could not be used, so playback could not be started.")
        return self._result(outcome, what, context)

    def _lookup(self, query: str, kind: str, context: ToolContext):
        country = player.store_country(getattr(context.cfg, "apple_music_country", ""))
        try:
            item = catalogue.search(query, kind, country)
        except catalogue.CatalogueUnavailable:
            context.user_print("⚠️ Apple Music catalogue unreachable")
            return _fail(_OFFLINE)
        except ValueError as exc:
            return _fail(str(exc))
        if item is None:
            context.user_print("🎵 Nothing matching in Apple Music")
            return _fail(f"Apple Music has no {kind} matching that request.")
        page = item.album if kind == "song" else item.title
        return player.Target(kind=kind, uri=item.app_uri(country), page_title=page,
                             track=item.title if kind == "song" else "", artist=item.artist)

    def _result(self, outcome, what: str, context: ToolContext) -> ToolExecutionResult:
        if outcome.status == "playing" and outcome.now_playing is not None:
            _remember_player()
            context.user_print(f"🎵 Playing {_track(outcome.now_playing)}")
            return ToolExecutionResult(True, f"Playing {_track(outcome.now_playing)} in Apple Music.")
        if outcome.status == "other_playing" and outcome.now_playing is not None:
            _remember_player()
            context.user_print(f"🎵 Apple Music is playing {_track(outcome.now_playing)}")
            return ToolExecutionResult(
                True, f"Apple Music is playing {_track(outcome.now_playing)}, which could not be confirmed as the "
                      f"requested {what}.")
        if outcome.status == "ambiguous":
            names = ", ".join(outcome.candidates)
            return _fail(f"Several of your playlists match: {names}. Say which one.")
        if outcome.status == "not_found" and what == "playlist":
            return _fail("None of your Apple Music playlists matches that name.")
        context.user_print("⚠️ Apple Music did not start playing")
        return _fail(_FAILURES.get(outcome.status, _FAILURES["not_started"]).format(what=what))

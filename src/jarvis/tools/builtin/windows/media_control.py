"""Control media playback and report what is playing on Windows."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ....debug import debug_log
from ....platform.windows import media
from ...base import Tool, ToolContext
from ...types import ToolExecutionResult
from ._common import disabled_result, failure

_ACTIONS = ["play", "pause", "play_pause", "next", "previous", "now_playing"]

_DONE = {
    "play": "Resumed playback.",
    "pause": "Paused playback.",
    "play_pause": "Toggled play/pause.",
    "next": "Skipped to the next track.",
    "previous": "Went back to the previous track.",
}
_ALREADY = {"play": "Media is already playing.", "pause": "Media is already paused."}


_AFTER = {"pause": "paused", "play": "playing"}


def _remember(info: Optional[media.NowPlaying], status: str) -> None:
    """The media player is what "pause it" is about (``memory/desktop_referents.spec.md``).

    Only the application and playback state are kept, never the track."""
    if info is None:
        return
    try:
        from ....memory.desktop_referents import get_desktop_referents
        get_desktop_referents().record_media(media.app_display_name(info.app), status)
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f"media referent not recorded ({type(exc).__name__})", "windows")


def _status_after(action: str, outcome: media.MediaOutcome) -> str:
    before = outcome.now_playing.status if outcome.now_playing else ""
    if action == "play_pause" and outcome.status == "done":
        return {"playing": "paused", "paused": "playing"}.get(before, "")
    return _AFTER.get(action, before) if outcome.status == "done" else before


def _track(info: media.NowPlaying) -> str:
    title = info.title or "Unknown title"
    return f"{title} by {info.artist}" if info.artist else title


def _action(args) -> str:
    return str((args if isinstance(args, dict) else {}).get("action") or "").strip().lower()


class MediaControlTool(Tool):
    """Transport controls for whatever app is playing media."""

    @property
    def name(self) -> str:
        return "mediaControl"

    @property
    def description(self) -> str:
        return (
            "Control the media player that is currently active (Spotify, YouTube "
            "in a browser, etc.): play, pause, toggle, next or previous track, "
            "or say what song is playing (e.g. 'pause the music', 'next song', "
            "'what's playing?'). NOT for loudness; that is systemVolume."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": _ACTIONS,
                    "description": (
                        "play, pause, play_pause (toggle), next, previous, or "
                        "now_playing to read the current title and artist."
                    ),
                },
            },
            "required": ["action"],
        }

    def returns_outside_content_for(self, args: Optional[Dict[str, Any]]) -> bool:
        # A track title and artist are chosen by whoever published the media (routines.spec.md).
        return _action(args) == "now_playing"

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        blocked = disabled_result(context)
        if blocked:
            return blocked

        action = _action(args)
        debug_log(f"mediaControl action={action}", "windows")
        if action not in _ACTIONS:
            return failure(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")

        try:
            if action == "now_playing":
                info = media.now_playing()
                if info is None:
                    context.user_print("🎵 Nothing is playing")
                    return ToolExecutionResult(True, "No media is currently playing.")
                context.user_print(f"🎵 {_track(info)}")
                _remember(info, info.status)
                return ToolExecutionResult(
                    True, f"Now playing ({info.status}): {_track(info)}"
                )

            outcome = media.control(action)
        except media.MediaError as exc:
            context.user_print("⚠️ Could not reach media controls.")
            return failure(str(exc))

        if outcome.status == "no_session":
            context.user_print("🎵 No media player is active")
            return failure("No media player is active, so there is nothing to control.")
        if outcome.status == "unsupported":
            context.user_print("⚠️ The player did not accept that command")
            return failure("The active media player does not support that command.")

        text = _DONE[action] if outcome.status == "done" else _ALREADY[action]
        _remember(outcome.now_playing, _status_after(action, outcome))
        context.user_print(f"🎵 {text}")
        return ToolExecutionResult(True, text)

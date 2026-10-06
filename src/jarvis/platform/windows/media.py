"""Media transport control and "now playing" for Windows.

Uses the System Media Transport Controls (SMTC) session manager, which is what
the Windows volume flyout and lock screen read. SMTC exposes explicit play and
pause commands and the playback state, so "pause" can never start playback the
way the ``play/pause`` media key (a toggle) can. Next and previous fall back to
the media virtual keys only when a session exists but rejects the command.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Protocol

from ...debug import debug_log
from ._bounded import run_bounded

CALL_TIMEOUT_SEC = 5.0

ACTIONS = ("play", "pause", "play_pause", "next", "previous")

# Virtual-key codes for the keyboard media keys.
_MEDIA_KEYS = {"next": 0xB0, "previous": 0xB1, "play_pause": 0xB3}
_KEYEVENTF_KEYUP = 0x0002


class MediaError(RuntimeError):
    """The media session manager could not be reached."""


@dataclass(frozen=True)
class NowPlaying:
    title: str
    artist: str
    status: str  # "playing", "paused", "stopped", ...
    app: str


@dataclass(frozen=True)
class MediaOutcome:
    # done: command sent; already: media was already in the requested state;
    # no_session: nothing is exposing media controls; unsupported: the session
    # rejected the command and no safe fallback exists.
    status: str
    now_playing: Optional[NowPlaying] = None


class MediaBackend(Protocol):
    def get_session(self) -> Optional[NowPlaying]: ...
    def play(self) -> bool: ...
    def pause(self) -> bool: ...
    def toggle(self) -> bool: ...
    def next(self) -> bool: ...
    def previous(self) -> bool: ...
    def press_media_key(self, key: str) -> None: ...


class WinRTMediaBackend:
    """SMTC through the ``winrt-Windows.Media.Control`` projection."""

    @staticmethod
    async def _manager():
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        return await Manager.request_async()

    def _run(self, coro_fn):
        try:
            return run_bounded(lambda: asyncio.run(coro_fn()), CALL_TIMEOUT_SEC)
        except TimeoutError as exc:
            raise MediaError("The media session did not respond in time.") from exc
        except Exception as exc:
            raise MediaError(f"Could not reach Windows media controls: {exc}") from exc

    def get_session(self) -> Optional[NowPlaying]:
        async def _read():
            session = (await self._manager()).get_current_session()
            if session is None:
                return None
            props = await session.try_get_media_properties_async()
            status = session.get_playback_info().playback_status
            return NowPlaying(
                title=props.title or "",
                artist=props.artist or "",
                status=status.name.lower(),
                app=session.source_app_user_model_id or "",
            )

        return self._run(_read)

    def _command(self, method: str) -> bool:
        async def _send():
            session = (await self._manager()).get_current_session()
            if session is None:
                return False
            return bool(await getattr(session, method)())

        return self._run(_send)

    def play(self) -> bool:
        return self._command("try_play_async")

    def pause(self) -> bool:
        return self._command("try_pause_async")

    def toggle(self) -> bool:
        return self._command("try_toggle_play_pause_async")

    def next(self) -> bool:
        return self._command("try_skip_next_async")

    def previous(self) -> bool:
        return self._command("try_skip_previous_async")

    def press_media_key(self, key: str) -> None:
        if sys.platform != "win32":
            raise MediaError("Media keys are only available on Windows.")
        import ctypes

        code = _MEDIA_KEYS[key]
        user32 = ctypes.windll.user32
        user32.keybd_event(code, 0, 0, 0)
        user32.keybd_event(code, 0, _KEYEVENTF_KEYUP, 0)


def readable_app_id(app_id: str) -> str:
    """A readable form of a media session's application identifier, when Windows has no name for it.

    ``AppleInc.AppleMusicWin_nzyj5cx40ttqa!App`` -> ``AppleMusicWin``, ``Spotify.exe`` -> ``Spotify``."""
    text = str(app_id or "").split("!", 1)[0]
    if text.lower().endswith(".exe"):
        text = text[:-4]
    text = text.split("_", 1)[0]
    return text.rsplit(".", 1)[-1]


@lru_cache(maxsize=32)
def _shell_display_name(app_id: str) -> str:
    """The name Windows shows for an application user model ID (Start menu), or empty."""
    import pythoncom
    from win32comext.shell import shell, shellcon

    pythoncom.CoInitialize()
    try:
        item = shell.SHCreateItemFromParsingName("shell:AppsFolder\\" + app_id, None, shell.IID_IShellItem)
        return str(item.GetDisplayName(shellcon.SIGDN_NORMALDISPLAY) or "").strip()
    finally:
        item = None
        pythoncom.CoUninitialize()


def app_display_name(app_id: str) -> str:
    """The media session's application as Windows names it (for example "Apple Music"), else a
    readable form of its identifier. Bounded; never the track."""
    if not app_id:
        return ""
    try:
        name = run_bounded(lambda: _shell_display_name(app_id), CALL_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 - an unnamed application keeps its identifier's name
        debug_log(f"media application name unavailable ({type(exc).__name__})", "windows")
        name = ""
    return name or readable_app_id(app_id)


def create_backend() -> MediaBackend:
    return WinRTMediaBackend()


def now_playing() -> Optional[NowPlaying]:
    """The current media session, or ``None`` when nothing exposes controls."""
    return create_backend().get_session()


def control(action: str) -> MediaOutcome:
    """Perform a transport action against the current media session."""
    if action not in ACTIONS:
        raise ValueError(f"unknown media action: {action!r}")

    backend = create_backend()
    session = backend.get_session()
    if session is None:
        debug_log(f"media {action}: no session", "windows")
        return MediaOutcome("no_session")

    playing = session.status == "playing"
    if action == "pause" and not playing:
        return MediaOutcome("already", session)
    if action == "play" and playing:
        return MediaOutcome("already", session)

    send = {
        "play": backend.play,
        "pause": backend.pause,
        "play_pause": backend.toggle,
        "next": backend.next,
        "previous": backend.previous,
    }[action]
    if send():
        debug_log(f"media {action}: sent via session", "windows")
        return MediaOutcome("done", session)

    if action in ("next", "previous"):
        debug_log(f"media {action}: session rejected, using media key", "windows")
        backend.press_media_key(action)
        return MediaOutcome("done", session)

    debug_log(f"media {action}: session rejected the command", "windows")
    return MediaOutcome("unsupported", session)

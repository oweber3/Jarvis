"""Start a song, album, artist or library playlist in the Apple Music app for Windows.

There is no public Windows API for Apple Music, so this drives the app the way a person would, through
documented surfaces only: a ``musics://music.apple.com/...`` link opens the item in the app (the app
registers that protocol), UI Automation control patterns press its Play controls (never mouse or keyboard
input), and the Windows media session (SMTC) confirms what is playing. Controls are recognised by the
app's automation ids and by the catalogue names on the page, never by localised button text.

Pure OS layer: knows nothing about tools, replies or models. The decision logic works on a ``Desktop``
so it can be tested without the app; ``play_target`` and ``play_playlist`` run it against the real app on
a bounded worker. Names, titles and links are never logged. See ``apple_music/apple_music.spec.md``.
"""
from __future__ import annotations

import ctypes
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol, Sequence, Tuple, TypeVar

from ...debug import debug_log
from ._bounded import run_bounded
from .media import MediaError, NowPlaying

PACKAGE_FAMILY = "AppleInc.AppleMusicWin_nzyj5cx40ttqa"
APP_ID = PACKAGE_FAMILY + "!App"
APP_NAME = "Apple Music"
PROCESS_NAME = "AppleMusic"
LAUNCH_URI = "shell:AppsFolder\\" + APP_ID

POLL_SEC = 0.4
PAGE_TIMEOUT_SEC = 20.0  # includes starting the app from cold
MENU_TIMEOUT_SEC = 3.0
CONFIRM_TIMEOUT_SEC = 8.0
TOTAL_TIMEOUT_SEC = 45.0
MAX_SCROLLS = 8
MAX_CANDIDATES = 10
# Paused this many polls after loading the right item: send one explicit play.
RESUME_AFTER_POLLS = 2

# Automation ids of the app's own controls (identical in every language).
HEADER_PLAY_IDS = frozenset({"PlayButtonElement", "PlayButton"})
TITLE_ID = "TitleTextBlock"
PLAYLISTS_HEADER_ID = "Sidebar_Header_Playlists"
PLAYLIST_ID_MARK = "IKIND:ePlaylist"

_LINK_RE = re.compile(r"^musics://music\.apple\.com/[a-z]{2}/(?:album/\d{1,20}(?:\?i=\d{1,20})?|artist/\d{1,20})$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_REPOSITORY_KEY = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
_PACKAGE_NAME, _PUBLISHER_ID = PACKAGE_FAMILY.split("_", 1)

T = TypeVar("T")


@dataclass(frozen=True)
class Control:
    kind: str
    name: str
    automation_id: str
    path: Tuple[int, ...]
    patterns: frozenset
    expand: Optional[int] = None  # 0 collapsed, 1 expanded, for expand/collapse controls
    handle: Any = field(default=None, compare=False, repr=False)


class AppWindow(Protocol):
    def controls(self) -> List[Control]: ...
    def invoke(self, control: Control) -> None: ...
    def select(self, control: Control) -> None: ...
    def expand(self, control: Control) -> None: ...
    def collapse(self, control: Control) -> None: ...
    def scroll_down(self, control: Control) -> None: ...


class Desktop(Protocol):
    def installed(self) -> bool: ...
    def open_uri(self, uri: str) -> None: ...
    def window(self) -> Optional[AppWindow]: ...
    def now_playing(self) -> Optional[NowPlaying]: ...
    def resume(self) -> bool: ...
    def wait(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class Target:
    """A catalogue item to open and start: ``uri`` opens its page, ``page_title`` is the page's heading
    (the album for a song or album, the artist for an artist) and ``track`` the song on that page."""
    kind: str
    uri: str
    page_title: str
    track: str = ""
    artist: str = ""


@dataclass(frozen=True)
class PlayOutcome:
    # playing: the asked-for item is playing; other_playing: Apple Music plays something that could not be
    # confirmed as the item; not_installed; no_window: the app did not open; not_found: the page, track or
    # playlist never appeared; ambiguous: several playlists match (``candidates``); no_control: the Play
    # control or menu item was missing; not_started: nothing started playing.
    status: str
    now_playing: Optional[NowPlaying] = None
    candidates: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    from .ui_automation import normalise_name
    return normalise_name(text)


def _children(controls: Sequence[Control], parent: Tuple[int, ...]) -> List[Control]:
    return [c for c in controls if c.path[:-1] == parent]


def _is_button(control: Control) -> bool:
    return control.kind == "button" and "invoke" in control.patterns


def header_play(controls: Sequence[Control], title: str) -> Optional[Control]:
    """The page header's Play button when the header's title is ``title``.

    Album and playlist headers mark Play with an automation id; the artist header has an unnamed button next
    to ``TitleTextBlock``. The title is the header's first text, so an artist name elsewhere in the header
    never stands in for an album title."""
    wanted = _norm(title)
    if not wanted:
        return None
    for control in controls:
        if not _is_button(control):
            continue
        siblings = _children(controls, control.path[:-1])
        marked = control.automation_id in HEADER_PLAY_IDS
        beside_title = not control.automation_id and any(s.automation_id == TITLE_ID for s in siblings)
        if not (marked or beside_title):
            continue
        texts = sorted((s for s in siblings if s.kind == "text"), key=lambda s: s.path)
        if texts and _norm(texts[0].name) == wanted:
            return control
    return None


def track_more_button(controls: Sequence[Control], title: str) -> Optional[Control]:
    """The "More" button of the track row whose title text is ``title`` (an unnamed-id button in the row)."""
    wanted = _norm(title)
    for row in controls:
        if row.kind != "listitem":
            continue
        cells = _children(controls, row.path)
        if any(c.kind == "text" and _norm(c.name) == wanted for c in cells):
            buttons = [c for c in cells if _is_button(c) and not c.automation_id]
            if buttons:
                return buttons[-1]
    return None


def play_menu_item(controls: Sequence[Control], title: str) -> Optional[Control]:
    """The open context menu's item that plays ``title`` (its text quotes the title in every language)."""
    wanted = _norm(title)
    for control in controls:
        if control.kind == "menuitem" and "invoke" in control.patterns and wanted and wanted in _norm(control.name):
            return control
    return None


def _scroll_list(controls: Sequence[Control], anchor: Control) -> Optional[Control]:
    lists = [c for c in controls if c.kind == "list" and "scroll" in c.patterns and anchor.path[:len(c.path)] == c.path]
    return max(lists, key=lambda c: len(c.path)) if lists else None


def _poll(desktop: Desktop, timeout: float, probe: Callable[[], Optional[T]]) -> Optional[T]:
    for attempt in range(max(1, math.ceil(timeout / POLL_SEC))):
        if attempt:
            desktop.wait(POLL_SEC)
        found = probe()
        if found is not None:
            return found
    return None


def _matches(target: Target) -> Callable[[NowPlaying], bool]:
    """A song by its title; an album or artist by its name in the session's "artist — album" line."""
    if target.kind == "song":
        return lambda now: _norm(now.title) == _norm(target.track)
    return lambda now: _norm(target.page_title) in _norm(now.artist)


def _safe_now(desktop: Desktop) -> Optional[NowPlaying]:
    try:
        return desktop.now_playing()
    except MediaError:
        return None


def _confirm(desktop: Desktop, matches: Callable[[NowPlaying], bool], timeout: float) -> PlayOutcome:
    """Wait for Apple Music to report the item playing; one explicit play if it loaded but stayed paused."""
    resumed = False
    last = None
    for attempt in range(max(1, math.ceil(timeout / POLL_SEC))):
        if attempt:
            desktop.wait(POLL_SEC)
        now = _safe_now(desktop)
        last = now
        if now is None or now.app != APP_ID or not matches(now):
            continue
        if now.status == "playing":
            debug_log(f"apple music confirmed playing after {attempt} polls", "music")
            return PlayOutcome("playing", now)
        if not resumed and attempt >= RESUME_AFTER_POLLS:
            debug_log("apple music loaded the item paused; sending play", "music")
            try:
                desktop.resume()
            except MediaError:
                pass
            resumed = True
    if last is not None and last.app == APP_ID and last.status == "playing":
        debug_log("apple music is playing something unconfirmed", "music")
        return PlayOutcome("other_playing", last)
    debug_log("apple music did not start playing", "music")
    return PlayOutcome("not_started", last)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

def check_link(uri: str) -> None:
    """Raise ``ValueError`` unless ``uri`` is a catalogue link of the shape this module builds."""
    if not isinstance(uri, str) or not _LINK_RE.match(uri):
        raise ValueError("Not an Apple Music catalogue link.")


def play_target_with(desktop: Desktop, target: Target, *, page_timeout: float = PAGE_TIMEOUT_SEC,
                     confirm_timeout: float = CONFIRM_TIMEOUT_SEC) -> PlayOutcome:
    """Open ``target`` in the app and start it; see ``PlayOutcome`` for the results."""
    check_link(target.uri)
    if not desktop.installed():
        return PlayOutcome("not_installed")
    desktop.open_uri(target.uri)

    def page():
        window = desktop.window()
        if window is None:
            return None
        controls = window.controls()
        play = header_play(controls, target.page_title)
        return (window, controls, play) if play is not None else None

    ready = _poll(desktop, page_timeout, page)
    if ready is None:
        debug_log(f"apple music page for the {target.kind} did not appear", "music")
        return PlayOutcome("not_found" if desktop.window() is not None else "no_window")
    window, controls, play = ready
    if target.kind != "song":
        window.invoke(play)
        return _confirm(desktop, _matches(target), confirm_timeout)

    more = track_more_button(controls, target.track)
    scrolls = 0
    while more is None and scrolls < MAX_SCROLLS:
        container = _scroll_list(controls, play)
        if container is None:
            break
        window.scroll_down(container)
        scrolls += 1
        desktop.wait(POLL_SEC)
        controls = window.controls()
        more = track_more_button(controls, target.track)
    if more is None:
        debug_log(f"apple music track row not found after {scrolls} scrolls", "music")
        return PlayOutcome("not_found")
    window.invoke(more)
    item = _poll(desktop, MENU_TIMEOUT_SEC, lambda: play_menu_item(window.controls(), target.track))
    if item is None:
        debug_log("apple music track menu had no play item", "music")
        return PlayOutcome("no_control")
    window.invoke(item)
    return _confirm(desktop, _matches(target), confirm_timeout)


def _distinct(names: Sequence[str]) -> List[str]:
    seen, out = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def play_playlist_with(desktop: Desktop, name: str, *, page_timeout: float = PAGE_TIMEOUT_SEC,
                       confirm_timeout: float = CONFIRM_TIMEOUT_SEC) -> PlayOutcome:
    """Find the user's own playlist ``name`` in the app's sidebar and start it. Nothing leaves the PC."""
    from .ui_automation import match_names
    if not desktop.installed():
        return PlayOutcome("not_installed")
    window = desktop.window()
    if window is None:
        desktop.open_uri(LAUNCH_URI)
        window = _poll(desktop, page_timeout, desktop.window)
        if window is None:
            return PlayOutcome("no_window")

    def find_header():
        return next((c for c in window.controls() if c.automation_id == PLAYLISTS_HEADER_ID), None)

    header = _poll(desktop, page_timeout, find_header)
    if header is None:
        return PlayOutcome("no_control")
    expanded_here = header.expand == 0
    if expanded_here:
        window.expand(header)

    def entries_now():
        listed = [c for c in window.controls() if c.kind == "listitem" and PLAYLIST_ID_MARK in c.automation_id]
        return listed or None

    try:
        entries = (_poll(desktop, MENU_TIMEOUT_SEC, entries_now) if expanded_here else entries_now()) or []
        names = [entry.name for entry in entries]
        hits = match_names(name, names)
        chosen = _distinct([names[index] for index in hits])
        debug_log(f"apple music playlists: {len(entries)} listed, {len(chosen)} matching", "music")
        if not chosen:
            return PlayOutcome("not_found")
        if len(chosen) > 1:
            return PlayOutcome("ambiguous", candidates=tuple(chosen[:MAX_CANDIDATES]))
        window.select(entries[hits[0]])

        def page():
            controls = window.controls()
            play = header_play(controls, chosen[0])
            return (controls, play) if play is not None else None

        ready = _poll(desktop, page_timeout, page)
        if ready is None:
            return PlayOutcome("no_control")
        controls, play = ready
        titles = row_texts(controls)
        window.invoke(play)
    finally:
        if expanded_here:
            try:
                window.collapse(header)
            except OSError:
                pass
    # What plays must be one of the playlist's own tracks, so a paused track from elsewhere is never taken
    # (or resumed) for it.
    return _confirm(desktop, lambda now: _norm(now.title) in titles, confirm_timeout)


def row_texts(controls: Sequence[Control]) -> frozenset:
    """Normalised texts of the page's track rows: titles, artists and durations."""
    rows = {c.path for c in controls if c.kind == "listitem"}
    return frozenset(_norm(c.name) for c in controls if c.kind == "text" and c.path[:-1] in rows and c.name)


# ---------------------------------------------------------------------------
# The real app
# ---------------------------------------------------------------------------

def windows_region() -> str:
    """The two-letter country of Windows' region setting, or ``''``."""
    if sys.platform != "win32":
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(16)
        if ctypes.windll.kernel32.GetUserDefaultGeoName(buffer, len(buffer)) > 0:
            return buffer.value
    except (AttributeError, OSError):
        pass
    return ""


def store_country(configured: str, *, region: Callable[[], str] = windows_region) -> str:
    """The lower-case store country: the setting when it is two letters, else Windows' region, else ``us``."""
    for value in (configured, region()):
        if isinstance(value, str) and _COUNTRY_RE.match(value.strip()):
            return value.strip().lower()
    return "us"


def installed() -> bool:
    """Whether the Apple Music app (Microsoft Store package) is installed for this user."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REPOSITORY_KEY) as key:
            index = 0
            while True:
                try:
                    package = winreg.EnumKey(key, index)
                except OSError:
                    return False
                if package.startswith(_PACKAGE_NAME + "_") and package.endswith("_" + _PUBLISHER_ID):
                    return True
                index += 1
    except OSError:
        return False


class _UiaWindow:
    def __init__(self, session, hwnd: int):
        self._session = session
        self._hwnd = hwnd

    def controls(self) -> List[Control]:
        try:
            nodes = self._session.nodes(self._hwnd)
        except Exception as exc:  # noqa: BLE001 - a page that is changing reads as still loading
            debug_log(f"apple music window unreadable for now ({type(exc).__name__})", "music")
            return []
        return [Control(kind=n.kind, name=n.name, automation_id=n.automation_id, path=n.path,
                        patterns=n.patterns, expand=n.expand, handle=n) for n in nodes]

    def _act(self, method: str, control: Control) -> None:
        try:
            getattr(self._session, method)(control.handle)
        except OSError:
            raise
        except Exception as exc:  # noqa: BLE001 - COM errors from a control that went away
            raise OSError(f"Apple Music control action failed ({type(exc).__name__}).") from None

    def invoke(self, control: Control) -> None:
        self._act("invoke", control)

    def select(self, control: Control) -> None:
        self._act("select", control)

    def expand(self, control: Control) -> None:
        self._act("expand", control)

    def collapse(self, control: Control) -> None:
        self._act("collapse", control)

    def scroll_down(self, control: Control) -> None:
        self._act("scroll_down", control)


class _WindowsDesktop:
    def __init__(self, session):
        self._session = session

    def installed(self) -> bool:
        return installed()

    def open_uri(self, uri: str) -> None:
        if uri != LAUNCH_URI:
            check_link(uri)
        os.startfile(uri)  # the shell hands the link to the app that registered the protocol

    def window(self) -> Optional[AppWindow]:
        from .windows_mgmt import list_windows
        for window in list_windows():
            if window.process.casefold() == PROCESS_NAME.casefold():
                return _UiaWindow(self._session, window.hwnd)
        return None

    def now_playing(self) -> Optional[NowPlaying]:
        from . import media
        return media.now_playing()

    def resume(self) -> bool:
        from . import media
        return media.control("play").status in ("done", "already")

    def wait(self, seconds: float) -> None:
        import time
        time.sleep(seconds)


def _run(flow: Callable[[Desktop], PlayOutcome]) -> PlayOutcome:
    from .ui_automation import with_session
    return run_bounded(lambda: with_session(lambda session: flow(_WindowsDesktop(session))), TOTAL_TIMEOUT_SEC)


def play_target(target: Target) -> PlayOutcome:
    """Open and start ``target`` in the real app. Raises ``TimeoutError`` or ``OSError`` on OS failures."""
    return _run(lambda desktop: play_target_with(desktop, target))


def play_playlist(name: str) -> PlayOutcome:
    """Start the user's playlist ``name`` in the real app. Raises ``TimeoutError`` or ``OSError``."""
    return _run(lambda desktop: play_playlist_with(desktop, name))

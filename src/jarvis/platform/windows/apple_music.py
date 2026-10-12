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
import os
import re
import sys
import threading
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
PAGE_TIMEOUT_SEC = 20.0  # opening the item's page, including starting the app from cold
PLAYLIST_PAGE_TIMEOUT_SEC = 8.0
MENU_TIMEOUT_SEC = 3.0
CONFIRM_TIMEOUT_SEC = 8.0
# Every flow returns within this, acting on nothing once it has passed.
TOTAL_TIMEOUT_SEC = 45.0
# One tree walk or media call can overrun the flow's own deadline by this much before the worker is abandoned.
WORKER_GRACE_SEC = 12.0
MAX_SCROLLS = 8
MAX_CANDIDATES = 10
# The right item seen paused this many polls in a row: send one explicit play.
RESUME_AFTER_POLLS = 2

# Automation ids of the app's own controls (identical in every language).
ALBUM_PLAY_ID = "PlayButtonElement"
HEADER_PLAY_IDS = frozenset({ALBUM_PLAY_ID, "PlayButton"})
TITLE_ID = "TitleTextBlock"
PLAYLISTS_HEADER_ID = "Sidebar_Header_Playlists"
PLAYLIST_ID_MARK = "IKIND:ePlaylist"
# The media session's artist line as Apple Music fills it: "<artist> — <album>".
_ARTIST_ALBUM_SEPARATOR = " — "

_LINK_RE = re.compile(r"musics://music\.apple\.com/[a-z]{2}/(?:album/\d{1,20}(?:\?i=\d{1,20})?|artist/\d{1,20})")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_REPOSITORY_KEY = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
_PACKAGE_NAME, _PUBLISHER_ID = PACKAGE_FAMILY.split("_", 1)
# One flow at a time; held by the worker until it has really finished, even after the caller gave up on it.
_FLOW_LOCK = threading.Lock()

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
    def clock(self) -> float: ...


@dataclass(frozen=True)
class Target:
    """A catalogue item to open and start: ``uri`` opens its page, ``page_title`` is the page's heading
    (the album for a song or album, the artist for an artist), ``track`` the song on that page and ``artist``
    the item's artist."""
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
    # control or menu item was missing; not_started: nothing started playing; timed_out: the flow ran out of
    # time and stopped acting; busy: another request was still driving the app.
    status: str
    now_playing: Optional[NowPlaying] = None
    candidates: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    from .ui_automation import normalise_name
    return normalise_name(text)


def _words(text: str) -> frozenset:
    from .ui_automation import name_words
    return frozenset(name_words(text))


def _children(controls: Sequence[Control], parent: Tuple[int, ...]) -> List[Control]:
    return [c for c in controls if c.path[:-1] == parent]


def _is_button(control: Control) -> bool:
    return control.kind == "button" and "invoke" in control.patterns


def header_play(controls: Sequence[Control], title: str, kind: str, artist: str = "") -> Optional[Control]:
    """The page header's Play button when the header shows ``title`` (and ``artist``, when given).

    Each kind has its own header: an album (or a song's album) marks Play ``PlayButtonElement`` and shows the
    artist beside the title; a playlist marks Play with one of ``HEADER_PLAY_IDS``; an artist page has an
    unnamed Play button beside ``TitleTextBlock``. The title is the header's first text, so an artist name
    elsewhere in the header never stands in for an album title."""
    wanted = _norm(title)
    if not wanted:
        return None
    for control in controls:
        if not _is_button(control):
            continue
        siblings = _children(controls, control.path[:-1])
        if kind == "artist":
            shaped = not control.automation_id and any(s.automation_id == TITLE_ID for s in siblings)
        elif kind == "playlist":
            shaped = control.automation_id in HEADER_PLAY_IDS
        else:
            shaped = control.automation_id == ALBUM_PLAY_ID
        if not shaped:
            continue
        texts = sorted((s for s in siblings if s.kind == "text"), key=lambda s: s.path)
        if not texts or _norm(texts[0].name) != wanted:
            continue
        if artist and kind in ("album", "song") and not any(_norm(t.name) == _norm(artist) for t in texts[1:]):
            continue
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


def row_texts(controls: Sequence[Control], anchor: Control) -> frozenset:
    """Normalised texts of the track rows in the list that holds ``anchor`` (the page header's Play button):
    titles, artists and durations. Sidebar entries are never included."""
    container = _scroll_list(controls, anchor)
    if container is None:
        return frozenset()
    inside = len(container.path)
    rows = {c.path for c in controls if c.kind == "listitem" and c.path[:inside] == container.path}
    return frozenset(_norm(c.name) for c in controls if c.kind == "text" and c.path[:-1] in rows and c.name)


def _split_artist_line(now: NowPlaying) -> Tuple[str, str]:
    artist, _, album = (now.artist or "").partition(_ARTIST_ALBUM_SEPARATOR)
    return _norm(artist), _norm(album)


def _same_artist(wanted: str, shown: str) -> bool:
    """Equal names, or one name's words all within the other (a duet credit); empty ``wanted`` accepts any."""
    if not wanted:
        return True
    a, b = _words(wanted), _words(shown)
    return _norm(wanted) == shown or (bool(a) and bool(b) and (a <= b or b <= a))


def _matches(target: Target) -> Callable[[NowPlaying], bool]:
    """Whether the media session shows ``target``: a song by title and artist, an album by its name in the
    album part of the session's artist line, an artist by the artist part."""
    def song(now):
        artist, _ = _split_artist_line(now)
        return _norm(now.title) == _norm(target.track) and _same_artist(target.artist, artist)

    def album(now):
        artist, album_name = _split_artist_line(now)
        return album_name == _norm(target.page_title) and _same_artist(target.artist, artist)

    def artist(now):
        return _split_artist_line(now)[0] == _norm(target.page_title)

    return {"song": song, "album": album, "artist": artist}[target.kind]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def _poll(desktop: Desktop, timeout: float, probe: Callable[[], Optional[T]], deadline: float) -> Optional[T]:
    """``probe()`` until it returns something, for at most ``timeout`` seconds and never past ``deadline``."""
    end = min(desktop.clock() + timeout, deadline)
    while True:
        found = probe()
        if found is not None:
            return found
        left = end - desktop.clock()
        if left <= 0:
            return None
        desktop.wait(min(POLL_SEC, left))


def _expired(desktop: Desktop, deadline: float) -> bool:
    if desktop.clock() < deadline:
        return False
    debug_log("apple music flow ran out of time; acting on nothing more", "music")
    return True


def _safe_now(desktop: Desktop) -> Optional[NowPlaying]:
    try:
        return desktop.now_playing()
    except MediaError:
        return None


def _confirm(desktop: Desktop, matches: Callable[[NowPlaying], bool], timeout: float,
             deadline: float) -> PlayOutcome:
    """Wait for Apple Music to report the item playing; one explicit play if the item stays paused."""
    end = min(desktop.clock() + timeout, deadline)
    resumed = False
    paused_polls = 0
    polls = 0
    last = None
    while True:
        now = _safe_now(desktop)
        last = now
        polls += 1
        if now is not None and now.app == APP_ID and matches(now):
            if now.status == "playing":
                debug_log(f"apple music confirmed playing after {polls} polls", "music")
                return PlayOutcome("playing", now)
            paused_polls += 1
            if not resumed and paused_polls >= RESUME_AFTER_POLLS and desktop.clock() < deadline:
                debug_log("apple music loaded the item paused; sending play", "music")
                try:
                    desktop.resume()
                except MediaError:
                    pass
                resumed = True
        else:
            paused_polls = 0
        left = end - desktop.clock()
        if left <= 0:
            break
        desktop.wait(min(POLL_SEC, left))
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
    if not isinstance(uri, str) or not _LINK_RE.fullmatch(uri):
        raise ValueError("Not an Apple Music catalogue link.")


def play_target_with(desktop: Desktop, target: Target, *, page_timeout: float = PAGE_TIMEOUT_SEC,
                     confirm_timeout: float = CONFIRM_TIMEOUT_SEC,
                     total_timeout: float = TOTAL_TIMEOUT_SEC) -> PlayOutcome:
    """Open ``target`` in the app and start it; see ``PlayOutcome`` for the results."""
    check_link(target.uri)
    if target.kind not in ("song", "album", "artist"):
        raise ValueError("Only songs, albums and artists open from the catalogue.")
    deadline = desktop.clock() + total_timeout
    if not desktop.installed():
        return PlayOutcome("not_installed")
    desktop.open_uri(target.uri)

    def page():
        window = desktop.window()
        if window is None:
            return None
        controls = window.controls()
        play = header_play(controls, target.page_title, target.kind, target.artist)
        return (window, controls, play) if play is not None else None

    ready = _poll(desktop, page_timeout, page, deadline)
    if ready is None:
        debug_log(f"apple music page for the {target.kind} did not appear", "music")
        if _expired(desktop, deadline):
            return PlayOutcome("timed_out")
        return PlayOutcome("not_found" if desktop.window() is not None else "no_window")
    window, controls, play = ready
    if target.kind != "song":
        if _expired(desktop, deadline):
            return PlayOutcome("timed_out")
        window.invoke(play)
        return _confirm(desktop, _matches(target), confirm_timeout, deadline)

    more = track_more_button(controls, target.track)
    scrolls = 0
    while more is None and scrolls < MAX_SCROLLS and not _expired(desktop, deadline):
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
        return PlayOutcome("timed_out" if _expired(desktop, deadline) else "not_found")
    if _expired(desktop, deadline):
        return PlayOutcome("timed_out")
    window.invoke(more)
    item = _poll(desktop, MENU_TIMEOUT_SEC, lambda: play_menu_item(window.controls(), target.track), deadline)
    if item is None:
        debug_log("apple music track menu had no play item", "music")
        return PlayOutcome("timed_out" if _expired(desktop, deadline) else "no_control")
    if _expired(desktop, deadline):
        return PlayOutcome("timed_out")
    window.invoke(item)
    return _confirm(desktop, _matches(target), confirm_timeout, deadline)


def _distinct(names: Sequence[str]) -> List[str]:
    seen, out = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def play_playlist_with(desktop: Desktop, name: str, *, page_timeout: float = PAGE_TIMEOUT_SEC,
                       confirm_timeout: float = CONFIRM_TIMEOUT_SEC,
                       total_timeout: float = TOTAL_TIMEOUT_SEC) -> PlayOutcome:
    """Find the user's own playlist ``name`` in the app's sidebar and start it. Nothing leaves the PC."""
    from .ui_automation import match_names
    deadline = desktop.clock() + total_timeout
    if not desktop.installed():
        return PlayOutcome("not_installed")
    # Starting the app and finding its sidebar share one page budget.
    app_ready = min(desktop.clock() + page_timeout, deadline)
    window = desktop.window()
    if window is None:
        desktop.open_uri(LAUNCH_URI)
        window = _poll(desktop, page_timeout, desktop.window, app_ready)
        if window is None:
            return PlayOutcome("no_window")

    def find_header():
        return next((c for c in window.controls() if c.automation_id == PLAYLISTS_HEADER_ID), None)

    header = _poll(desktop, page_timeout, find_header, app_ready)
    if header is None:
        return PlayOutcome("timed_out" if _expired(desktop, deadline) else "no_control")
    expanded_here = header.expand == 0
    if expanded_here:
        window.expand(header)

    def entries_now():
        listed = [c for c in window.controls() if c.kind == "listitem" and PLAYLIST_ID_MARK in c.automation_id]
        return listed or None

    try:
        entries = (_poll(desktop, MENU_TIMEOUT_SEC, entries_now, deadline) if expanded_here
                   else entries_now()) or []
        names = [entry.name for entry in entries]
        hits = match_names(name, names)
        chosen = _distinct([names[index] for index in hits])
        debug_log(f"apple music playlists: {len(entries)} listed, {len(chosen)} matching", "music")
        if not chosen:
            return PlayOutcome("not_found")
        if len(chosen) > 1:
            return PlayOutcome("ambiguous", candidates=tuple(chosen[:MAX_CANDIDATES]))
        if _expired(desktop, deadline):
            return PlayOutcome("timed_out")
        window.select(entries[hits[0]])

        def page():
            controls = window.controls()
            play = header_play(controls, chosen[0], "playlist")
            return (controls, play) if play is not None else None

        ready = _poll(desktop, PLAYLIST_PAGE_TIMEOUT_SEC, page, deadline)
        if ready is None:
            return PlayOutcome("timed_out" if _expired(desktop, deadline) else "no_control")
        if _expired(desktop, deadline):
            return PlayOutcome("timed_out")
        controls, play = ready
        titles = row_texts(controls, play)
        window.invoke(play)
    finally:
        if expanded_here:
            try:
                window.collapse(header)
            except OSError:
                pass
    # What plays must be one of the playlist's own tracks, so a paused track from elsewhere is never taken
    # (or resumed) for it.
    return _confirm(desktop, lambda now: _norm(now.title) in titles, confirm_timeout, deadline)


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

    def clock(self) -> float:
        import time
        return time.monotonic()


def _run(flow: Callable[[Desktop], PlayOutcome]) -> PlayOutcome:
    """Run ``flow`` on a bounded worker, one flow at a time. The flow stops acting at its own deadline; the
    worker's limit only adds the time one tree walk or media call may overrun it."""
    from .ui_automation import with_session

    def work() -> PlayOutcome:
        if not _FLOW_LOCK.acquire(timeout=1.0):
            debug_log("apple music is still busy with an earlier request", "music")
            return PlayOutcome("busy")
        try:
            return with_session(lambda session: flow(_WindowsDesktop(session)))
        finally:
            _FLOW_LOCK.release()

    return run_bounded(work, TOTAL_TIMEOUT_SEC + WORKER_GRACE_SEC)


def play_target(target: Target) -> PlayOutcome:
    """Open and start ``target`` in the real app. Raises ``TimeoutError`` or ``OSError`` on OS failures."""
    return _run(lambda desktop: play_target_with(desktop, target))


def play_playlist(name: str) -> PlayOutcome:
    """Start the user's playlist ``name`` in the real app. Raises ``TimeoutError`` or ``OSError``."""
    return _run(lambda desktop: play_playlist_with(desktop, name))

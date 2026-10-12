"""A scripted stand-in for the Apple Music app window and Windows media session.

Pages are lists of ``Control`` built the way the real app's UI Automation tree is shaped (measured on
Apple Music for Windows): a header group whose Play button sits next to the page title, track rows whose
"More" button opens a context menu holding a "Play <title>" item, and sidebar playlist entries that only
exist while the Playlists header is expanded. Invoking a control runs the reaction the real app has.
"""
from jarvis.platform.windows import apple_music as am
from jarvis.platform.windows.media import NowPlaying

APP = am.APP_ID


def control(kind, name="", automation_id="", path=(), patterns=(), expand=None):
    return am.Control(kind=kind, name=name, automation_id=automation_id, path=tuple(path),
                      patterns=frozenset(patterns), expand=expand)


def header(path, title, play_id="PlayButtonElement", artist=""):
    """A page header: the title, the artist (on album pages) and a Play button side by side."""
    controls = [
        control("group", title, path=path),
        control("text", title, path=path + (0,), patterns={"text"}),
        control("button", "Play", play_id, path=path + (2,), patterns={"invoke"}),
        control("button", "Shuffle", "ShuffleButtonElement", path=path + (3,), patterns={"invoke"}),
    ]
    if artist:
        controls.append(control("text", artist, path=path + (1,), patterns={"text"}))
    return controls


def artist_header(path, name):
    return [
        control("group", name, "RootGrid", path=path),
        control("button", "Play", "", path=path + (0,), patterns={"invoke"}),
        control("text", name, "TitleTextBlock", path=path + (1,), patterns={"text"}),
        control("button", "Favorite", "FavoriteButton", path=path + (2,), patterns={"invoke"}),
        control("button", "More", "MoreButton", path=path + (3,), patterns={"invoke"}),
    ]


def track_row(path, number, title):
    return [
        control("listitem", f"Track {number} {title} 3 minutes", path=path, patterns={"invoke", "selectionitem"}),
        control("text", str(number), path=path + (0,), patterns={"text"}),
        control("text", title, path=path + (1,), patterns={"text"}),
        control("button", "More", path=path + (2,), patterns={"invoke"}),
    ]


def album_page(album, artist, tracks, *, start=0):
    controls = [control("list", path=(5,), patterns={"scroll", "selection"})] + header((5, 0), album, artist=artist)
    for offset, title in enumerate(tracks):
        controls += track_row((5, 1 + offset), start + offset + 1, title)
    return {"controls": controls, "album": album, "artist": artist, "tracks": list(tracks)}


def artist_page(name, top_song):
    return {"controls": artist_header((5, 0), name), "artist": name, "top_song": top_song}


SIDEBAR_HEADER = (2, 0, 0, 4)


def sidebar(playlists, expanded):
    controls = [control("listitem", "Playlists", "Sidebar_Header_Playlists", path=SIDEBAR_HEADER,
                        patterns={"expandcollapse", "selectionitem"}, expand=1 if expanded else 0)]
    if expanded:
        for index, name in enumerate(playlists):
            controls.append(control("listitem", name, f"DBID:0x1-PID:0x{index:x}-IKIND:ePlaylist",
                                    path=(2, 0, 0, 5, index), patterns={"selectionitem"}))
    return controls


class FakeWindow:
    def __init__(self, desktop):
        self.desktop = desktop

    def controls(self):
        d = self.desktop
        d.reads += 1
        if d.loading > 0:
            d.loading -= 1
            return sidebar(d.playlists, d.expanded)
        page = d.page or {}
        rows = page.get("controls", [])
        if page.get("hidden_tracks") and not d.scrolled:
            rows = [c for c in rows if c.path[:2] not in page["hidden_tracks"]]
        return sidebar(d.playlists, d.expanded) + rows + d.menu

    def invoke(self, c):
        d = self.desktop
        d.actions.append(("invoke", c.automation_id or c.name))
        page = d.page or {}
        if c.kind == "button" and c.name == "More" and not c.automation_id:
            title = next(t.name for t in page["controls"] if t.kind == "text" and t.path[:-1] == c.path[:-1]
                         and not t.name.isdigit())
            if d.menu_opens:
                d.menu = [control("menu", path=(9,)),
                          control("menuitem", "Add to Library", path=(9, 0), patterns={"invoke"}),
                          control("menuitem", f"Play “{title}”", path=(9, 1), patterns={"invoke"}),
                          control("menuitem", "Play Next", path=(9, 2), patterns={"invoke"})]
        elif c.kind == "menuitem" and c.name.startswith("Play “"):
            d.menu = []
            d.start(c.name[len("Play “"):-1], page.get("artist", ""), page.get("album", ""))
        elif c.kind == "button" and c.name == "Play" and "top_song" in page:
            d.start(page["top_song"], page["artist"], "Greatest")
        elif c.kind == "button" and c.name == "Play":
            d.start(page["tracks"][0], page.get("artist", ""), page.get("album", ""))

    def select(self, c):
        d = self.desktop
        d.actions.append(("select", c.name))
        d.page = d.playlist_pages.get(c.name)
        d.loading = d.load_reads

    def expand(self, c):
        self.desktop.actions.append(("expand", c.automation_id))
        self.desktop.expanded = True

    def collapse(self, c):
        self.desktop.actions.append(("collapse", c.automation_id))
        self.desktop.expanded = False

    def scroll_down(self, c):
        self.desktop.actions.append(("scroll", c.path))
        self.desktop.scrolled = True


class FakeDesktop:
    def __init__(self, pages=None, *, installed=True, running=True, playlists=(), playlist_pages=None,
                 load_reads=1, starts_paused=False, plays=True, menu_opens=True, now=None):
        self.pages = pages or {}
        self.is_installed = installed
        self.running = running
        self.playlists = list(playlists)
        self.playlist_pages = playlist_pages or {}
        self.load_reads = load_reads
        self.starts_paused = starts_paused
        self.plays = plays
        self.menu_opens = menu_opens
        self.now = now
        self.page = None
        self.menu = []
        self.expanded = False
        self.scrolled = False
        self.loading = 0
        self.reads = 0
        self.opened = []
        self.actions = []
        self.resumes = 0
        self.waited = 0.0

    # Desktop protocol
    def installed(self):
        return self.is_installed

    def open_uri(self, uri):
        self.opened.append(uri)
        self.running = True
        if uri in self.pages:
            self.page = self.pages[uri]
            self.loading = self.load_reads
            self.scrolled = False

    def window(self):
        return FakeWindow(self) if self.running else None

    def now_playing(self):
        return self.now

    def resume(self):
        self.resumes += 1
        if self.now is not None and self.plays:
            self.now = NowPlaying(self.now.title, self.now.artist, "playing", self.now.app)
            return True
        return False

    def wait(self, seconds):
        self.waited += seconds

    def clock(self):
        return self.waited

    # Reactions
    def start(self, title, artist, album):
        if not self.plays:
            return
        status = "paused" if self.starts_paused else "playing"
        self.now = NowPlaying(title, f"{artist} — {album}", status, APP)

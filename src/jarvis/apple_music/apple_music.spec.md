# Apple Music

Jarvis starts a song, album, artist or one of the user's own playlists in the Apple Music app for Windows (the Microsoft Store app, package `AppleInc.AppleMusicWin_nzyj5cx40ttqa`). Three layers:

- `jarvis.apple_music.catalogue`: finds a song, album or artist in the Apple Music catalogue. Knows nothing about tools, the reply engine or the OS.
- `jarvis.platform.windows.apple_music`: opens an item in the app, presses its Play control and confirms what is playing. Knows nothing about tools, the reply engine or the LLM.
- `jarvis.tools.builtin.apple_music`: the `playMusic` tool, which turns those into the strings the model sees.

Pausing, resuming, skipping and "what's playing" stay with `mediaControl` (`platform/windows/windows.spec.md`), which already reaches Apple Music through the Windows media session.

## Boundaries

- **Opt-in, off by default.** `apple_music_enabled` (default `false`) registers the tool. Without it the router never sees the tool and nothing is ever sent to Apple. The tool also needs Windows and `windows_tools_enabled`.
- **The one external call.** A song, album or artist request sends the search text, the kind and the store country to Apple's public iTunes Search API (`https://itunes.apple.com/search`): no account, key, developer token or cookie, and nothing else about the user. Library playlists are found in the app itself and send nothing. There is no telemetry.
- **No vendor lock on the core.** The feature is an optional integration with an app the user already runs. Nothing else in Jarvis depends on it, and every other tool works offline without it.
- **Nothing personal in logs.** The search text, titles, artist and playlist names and links are never written to the debug log. Logs carry the kind, the store country and stage outcomes only.
- **Pattern-only automation.** The app is driven through a `musics://` link (a protocol the app registers), UI Automation control patterns (Invoke, SelectionItem, ExpandCollapse, Scroll) and the Windows media session. It never synthesises mouse or keyboard input and never focuses a window itself; the shell brings the app forward when it opens the link, as when any app is opened.
- **Language-independent.** Controls are recognised by the app's automation ids and by the catalogue or playlist names shown on the page, never by localised button text. The model fills `query` and `kind`; no phrase lists are involved.

## Catalogue

`search(query, kind, country)` returns the first usable result for `kind` (`song` searches entity `song`, `album` searches `album`, `artist` searches `musicArtist`), or `None`.

- The query is whitespace-tidied and cut to 200 characters. An empty query or another kind raises `ValueError` and sends nothing.
- One GET with a 5 second timeout, redirects not followed, the answer capped at 512 KB. A transport failure, a non-200 answer or an answer that is not the expected JSON raises `CatalogueUnavailable`.
- A result is usable only with positive integer ids (track and collection for a song, collection for an album, artist for an artist) and a title. Names are clipped to 200 characters.
- `CatalogueItem.app_uri(country)` builds `musics://music.apple.com/<country>/album/<collection>?i=<track>`, `.../album/<collection>` or `.../artist/<artist>` from the numeric ids only; links in the answer are never used.
- **Store country**: `apple_music_country` when it is two letters (stored in upper case), else the two-letter Windows region (`GetUserDefaultGeoName`), else `us`.

## Player

The decision logic works on a `Desktop` (installed check, open a link, the app window's controls and pattern actions, now playing, resume, wait), so it is tested against a scripted app. `play_target` and `play_playlist` run it against the real app on one bounded worker (45 seconds) inside one UI Automation session (`ui_automation.with_session`); a stuck call surfaces as `TimeoutError`.

- **Installed**: the per-user package repository in the registry lists an `AppleInc.AppleMusicWin_*` package of Apple's publisher id. When it does not, nothing is opened and nothing is sent.
- **The window** is the visible window of the `AppleMusic` process. A window whose tree cannot be read while it changes reads as still loading.
- **Links**: only links of the exact shape the catalogue builds are opened (`ValueError` otherwise); the app is started for playlists through its application id.
- **Page header**: the Play button of the header whose first text equals the expected heading (the album for a song or album, the artist for an artist, the playlist name), compared case-insensitively with whitespace tidied. Album and playlist Play buttons carry the automation ids `PlayButtonElement` or `PlayButton`; the artist Play button is the unnamed button beside `TitleTextBlock`. A page still showing another item is never played. The page is waited for up to 20 seconds, which covers starting the app.
- **Song**: on its album page, the track row whose title text equals the song title; when the row is not in the list yet, the list is scrolled down (at most 8 times). The row's More button opens the context menu, and the menu item whose text contains the song title is invoked (the app quotes the title in every language). A missing row is `not_found` and a menu without that item is `no_control`; nothing else is pressed.
- **Album and artist**: the header Play button.
- **Library playlist**: the sidebar's Playlists section (`Sidebar_Header_Playlists`) is expanded if it was collapsed and collapsed again afterwards. Entries are the sidebar items whose automation id marks a playlist. Names match as in `uiControl`: an exact name wins, else names containing every spoken word. Several different names is `ambiguous` with up to 10 candidates and nothing plays; none is `not_found`. The chosen entry is selected and its page's header Play button invoked.
- **Confirmation** reads the Windows media session for up to 8 seconds. The item counts as playing when Apple Music reports playing and: a song's title equals the song title; an album's or artist's name appears in the session's "artist — album" line; a playlist's title is one of the track rows on the playlist page. When the right item loaded but stays paused for two polls, one explicit play is sent (the app sometimes loads paused on a cold start); a paused track that is not the item is never resumed. Afterwards: `playing`; `other_playing` when Apple Music plays something that could not be confirmed as the item; `not_started` otherwise.

## playMusic

| Argument | Meaning |
|----------|---------|
| `query` | What to play as the user named it, with the artist when said |
| `kind` | `song`, `album`, `artist` or `playlist` (the user's own). A missing or unknown kind is treated as `song` |

- The tool is called `playMusic`, not after the app, so "open Apple Music" is not drawn to it by name. The description opens with playing or putting on a song, artist, album or playlist by name in Apple Music and "NOT to open apps", and points to `mediaControl` for pause, resume, skip and what is playing, to `appControl` for opening the app and to `systemVolume` for loudness (`evals/test_apple_music_routing.py`). `mediaControl` offers resume rather than play for the same reason (`platform/windows/windows.spec.md`).
- Results are raw facts: "Playing <title> by <artist> in Apple Music.", or for `other_playing` what Apple Music is playing and that it could not be confirmed as the request (both successes). Failures say what happened: not installed (install it from the Microsoft Store), the catalogue unreachable (check the internet connection; playlists still work), nothing matching, several playlists (names listed), the page or Play control missing, playback not started (check the Apple Music sign-in), or the app not responding in time.
- Starting music is routine and `SAFE`: no confirmation. Results carry titles from Apple, so the tool returns outside content (`routines.spec.md`).
- A successful start records Apple Music as the media desktop referent (`memory/desktop_referents.spec.md`), so "pause it" reaches it through `mediaControl`. The track is never recorded.
- Reply bridges: the tool is in Codex and Claude snapshots like other Windows tools; the search text it sends is what the user asked to play.
- Routines: `query` and `kind` are meaningful saved arguments, so "save that as wake-up music" replays the same request.
- There is no fast-path family: the kind of a spoken name (song, artist or album) needs the model.

## Tests

`tests/test_apple_music_catalogue.py` (fake HTTP), `tests/test_apple_music_player.py` (a scripted app in `tests/fake_apple_music.py`, shaped like the real app's UI Automation tree), `tests/test_apple_music_tool.py` and `tests/test_config_apple_music.py`. The default test run never opens the real app or contacts Apple. `evals/test_apple_music_routing.py` checks routing against `mediaControl` and `systemVolume` with the local model.

## Limits

- Songs and albums the user uploaded or that are not in the store catalogue are not found; only library playlists are searched in the app.
- Apple Music playlists from the catalogue (editorial playlists) are not in the public search.
- The automation ids and page layout are the app's own and may change in an app update; the player then reports the Play control or page as missing rather than pressing anything else.

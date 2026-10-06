"""Installed Steam games, read from Steam's own local manifests.

Nothing is downloaded and no Steam account data is read: only the install location (registry),
``libraryfolders.vdf`` and each library's ``appmanifest_*.acf``. Games launch through the
``steam://rungameid/<id>`` protocol, so Steam does its own launching and updating.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from ...debug import debug_log

# Steam installs these as "apps" but they are not games.
_TOOL_APP_IDS = {'228980'}
_TOOL_NAME_MARKERS = ('proton', 'steam linux runtime', 'steamworks common', 'steamvr')
_INSTALLED_FLAG = 4  # StateFlags: fully installed
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


@dataclass(frozen=True)
class SteamGame:
    appid: str
    name: str

    @property
    def launch_url(self) -> str:
        return f'steam://rungameid/{self.appid}'


def parse_vdf(text: str) -> dict:
    """Parse Valve's key-value text format into nested dictionaries."""
    stack: list[dict] = [{}]
    pending: str | None = None
    for string, brace in _TOKEN.findall(text):
        if brace == '{':
            child: dict = {}
            if pending is not None:
                stack[-1][pending] = child
            stack.append(child)
            pending = None
        elif brace == '}':
            if len(stack) > 1:
                stack.pop()
            pending = None
        elif pending is None:
            pending = string.replace('\\\\', '\\')
        else:
            stack[-1][pending] = string.replace('\\\\', '\\')
            pending = None
    return stack[0]


def steam_root() -> Path | None:
    """Steam's install folder from the registry, or ``None`` when Steam is not installed."""
    try:
        import winreg
    except ImportError:
        return None
    for hive, path, name in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                             (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
        try:
            with winreg.OpenKey(hive, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if isinstance(value, str) and Path(value).is_dir():
            return Path(value)
    return None


def library_folders(root: Path) -> list[str]:
    """Every library path Steam knows, the install folder first."""
    folders = [str(root)]
    try:
        parsed = parse_vdf((root / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return folders
    for entry in parsed.get('libraryfolders', {}).values():
        path = entry.get('path') if isinstance(entry, dict) else None
        if path and Path(path).resolve() not in {Path(folder).resolve() for folder in folders}:
            folders.append(path)
    return folders


def _is_tool(game: SteamGame) -> bool:
    return game.appid in _TOOL_APP_IDS or any(marker in game.name.casefold() for marker in _TOOL_NAME_MARKERS)


def installed_games(root: Path | None = None) -> list[SteamGame]:
    """Fully installed games across all libraries; tooling and unreadable manifests are skipped."""
    root = root if root is not None else steam_root()
    if root is None or not Path(root).is_dir():
        return []
    games: dict[str, SteamGame] = {}
    for folder in library_folders(Path(root)):
        for manifest in sorted((Path(folder) / 'steamapps').glob('appmanifest_*.acf')):
            try:
                state = parse_vdf(manifest.read_text(encoding='utf-8', errors='replace')).get('AppState', {})
                appid, name, flags = state['appid'], state['name'], int(state.get('StateFlags', 0))
            except (OSError, KeyError, ValueError, TypeError):
                continue
            game = SteamGame(str(appid), str(name))
            if flags & _INSTALLED_FLAG and appid.isdigit() and name and not _is_tool(game):
                games[game.appid] = game
    return list(games.values())


def steam_applications(root: Path | None = None):
    """Installed games as application-index entries that launch through Steam."""
    from .apps import Application
    games = installed_games(root)
    debug_log(f'Steam library contains {len(games)} installed game(s).', 'windows')
    return [Application(game.name, game.launch_url) for game in games]

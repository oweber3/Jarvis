"""Steam games are discovered from local manifests and launched through steam://rungameid."""
from pathlib import Path

import pytest

from jarvis.platform.windows import steam
from jarvis.platform.windows.apps import Application, resolve_application

LIBRARY = '''"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"%s"
\t\t"apps"
\t\t{
\t\t\t"228980"\t\t"0"
\t\t\t"739630"\t\t"0"
\t\t}
\t}
\t"1"
\t{
\t\t"path"\t\t"%s"
\t}
}
'''


def manifest(appid, name, flags=4):
    return f'''"AppState"
{{
\t"appid"\t\t"{appid}"
\t"name"\t\t"{name}"
\t"StateFlags"\t\t"{flags}"
\t"UserConfig"
\t{{
\t\t"name"\t\t"Not The Game Name"
\t}}
}}
'''


@pytest.fixture
def library(tmp_path):
    main, extra = tmp_path / 'Steam', tmp_path / 'GamesDrive'
    for root in (main, extra):
        (root / 'steamapps').mkdir(parents=True)
    (main / 'steamapps' / 'libraryfolders.vdf').write_text(
        LIBRARY % (str(main).replace('\\', '\\\\'), str(extra).replace('\\', '\\\\')), encoding='utf-8')
    (main / 'steamapps' / 'appmanifest_739630.acf').write_text(manifest(739630, 'Phasmophobia'), encoding='utf-8')
    (main / 'steamapps' / 'appmanifest_228980.acf').write_text(
        manifest(228980, 'Steamworks Common Redistributables'), encoding='utf-8')
    (extra / 'steamapps' / 'appmanifest_1245620.acf').write_text(manifest(1245620, 'ELDEN RING'), encoding='utf-8')
    (extra / 'steamapps' / 'appmanifest_1.acf').write_text(manifest(1, 'Half-Installed', flags=1026),
                                                           encoding='utf-8')
    (extra / 'steamapps' / 'appmanifest_bad.acf').write_text('not a manifest', encoding='utf-8')
    return main


def test_vdf_text_parses_into_nested_mappings():
    parsed = steam.parse_vdf(manifest(7, 'Seven'))
    assert parsed['AppState']['appid'] == '7'
    assert parsed['AppState']['name'] == 'Seven'
    assert parsed['AppState']['UserConfig']['name'] == 'Not The Game Name'


def test_library_folders_include_every_library(library):
    paths = steam.library_folders(library)
    assert [Path(p).name for p in paths] == ['Steam', 'GamesDrive']


def test_installed_games_come_from_every_library_and_skip_tooling_and_partial_installs(library):
    games = steam.installed_games(library)
    assert sorted((game.appid, game.name) for game in games) == [('1245620', 'ELDEN RING'), ('739630', 'Phasmophobia')]


def test_a_missing_or_unreadable_steam_install_yields_no_games(tmp_path):
    assert steam.installed_games(tmp_path / 'nope') == []
    assert steam.installed_games(None) == [] if steam.steam_root() is None else True


def test_games_join_the_application_index_with_a_steam_launch_url(library):
    apps = steam.steam_applications(library)
    assert Application('ELDEN RING', 'steam://rungameid/1245620') in apps
    found = resolve_application('elden ring', apps, {})
    assert found.target == 'steam://rungameid/1245620'


def test_opening_a_steam_game_launches_its_url(library, monkeypatch):
    from jarvis.platform.windows import apps as apps_module
    launched = []
    monkeypatch.setattr(apps_module.APP_INDEX, 'applications', lambda: steam.steam_applications(library))
    monkeypatch.setattr(apps_module.os, 'startfile', launched.append)
    result = apps_module.open_application('phasmophobia', {})
    assert launched == ['steam://rungameid/739630']
    assert result == {'action': 'open_requested', 'application': 'Phasmophobia'}


def test_application_discovery_includes_steam_games(library, monkeypatch):
    from jarvis.platform.windows import apps as apps_module
    monkeypatch.setattr(apps_module, '_discover_system_applications', lambda: [Application('Word', 'word.lnk')])
    monkeypatch.setattr(apps_module, 'steam_applications', lambda: steam.steam_applications(library))
    names = {app.name for app in apps_module.discover_applications()}
    assert {'Word', 'ELDEN RING', 'Phasmophobia'} <= names


def test_a_steam_failure_never_breaks_application_discovery(monkeypatch):
    from jarvis.platform.windows import apps as apps_module
    monkeypatch.setattr(apps_module, '_discover_system_applications', lambda: [Application('Word', 'word.lnk')])

    def broken():
        raise OSError('registry unavailable')
    monkeypatch.setattr(apps_module, 'steam_applications', broken)
    assert [app.name for app in apps_module.discover_applications()] == ['Word']


@pytest.mark.integration
def test_the_real_steam_library_can_be_read_without_launching_anything():
    games = steam.installed_games()
    assert all(game.appid.isdigit() and game.name for game in games)

"""Whole finalised utterances select one available routine tool or fall through."""
import pytest


def targets():
    from jarvis.fastpath.matcher import FastTarget
    return (
        FastTarget(('word', 'microsoft word'), 'Microsoft Word', 'winword', 'Word'),
        FastTarget(('chrome', 'google chrome'), 'Google Chrome', 'chrome', 'Chrome'),
        FastTarget(('matlab',), 'MATLAB', 'matlab', 'MATLAB'),
        FastTarget(('spotify',), 'Spotify', 'spotify', 'Spotify'),
    )


TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'systemInfo', 'openPath'}


@pytest.mark.parametrize('text,tool,args', [
    ('What time is it?', 'getTime', {'local_only': True}),
    ("What's the time?", 'getTime', {'local_only': True}),
    ('What day is it?', 'getTime', {'local_only': True}),
    ("What's today's date?", 'getTime', {'local_only': True}),
    ('Open Word', 'appControl', {'action': 'open', 'target': 'Microsoft Word'}),
    ('Launch Chrome', 'appControl', {'action': 'open', 'target': 'Google Chrome'}),
    ('Open MATLAB', 'appControl', {'action': 'open', 'target': 'MATLAB'}),
    ('Close Word', 'appControl', {'action': 'close', 'target': 'winword', 'match': 'process'}),
    ('Switch to Spotify', 'appControl', {'action': 'focus', 'target': 'spotify', 'match': 'process'}),
    ('Minimize Chrome', 'windowControl', {'action': 'minimise', 'target': 'chrome', 'match': 'process'}),
    ('Maximise Word', 'windowControl', {'action': 'maximise', 'target': 'winword', 'match': 'process'}),
    ('Restore Spotify', 'windowControl', {'action': 'restore', 'target': 'spotify', 'match': 'process'}),
    ('Set volume to 30%', 'systemVolume', {'action': 'set', 'percent': 30}),
    ('Turn the volume up', 'systemVolume', {'action': 'up'}),
    ('Turn the volume down', 'systemVolume', {'action': 'down'}),
    ('Mute the computer', 'systemVolume', {'action': 'mute'}),
    ('Unmute', 'systemVolume', {'action': 'unmute'}),
    ('Pause the music', 'mediaControl', {'action': 'pause'}),
    ('Play the music', 'mediaControl', {'action': 'play'}),
    ('Next track', 'mediaControl', {'action': 'next'}),
    ('Previous song', 'mediaControl', {'action': 'previous'}),
    ("What's using the most RAM?", 'systemInfo', {'action': 'top_memory'}),
    ("What's using the most CPU?", 'systemInfo', {'action': 'top_cpu'}),
    ('How much RAM am I using?', 'systemInfo', {'action': 'ram'}),
    ("What's my CPU usage?", 'systemInfo', {'action': 'cpu'}),
    ('Open Downloads', 'openPath', {'target': 'Downloads'}),
    ('Open my Documents folder', 'openPath', {'target': 'Documents'}),
    ('Open Desktop', 'openPath', {'target': 'Desktop'}),
    ('PLEASE, Could you OPEN Word?!', 'appControl', {'action': 'open', 'target': 'Microsoft Word'}),
    ('Launch Chrome, please.', 'appControl', {'action': 'open', 'target': 'Google Chrome'}),
    ('Set the volume to 30 percent please', 'systemVolume', {'action': 'set', 'percent': 30}),
    ('Increase the volume', 'systemVolume', {'action': 'up'}),
    ('Skip to the next song', 'mediaControl', {'action': 'next'}),
    ('What is using the most memory?', 'systemInfo', {'action': 'top_memory'}),
    ('What’s today’s date?', 'getTime', {'local_only': True}),
    ('What time is it now?', 'getTime', {'local_only': True}),
    ('What day is it today?', 'getTime', {'local_only': True}),
    ("What's the date today?", 'getTime', {'local_only': True}),
    ("What's the volume?", 'systemVolume', {'action': 'get'}),
    ('What is the current volume?', 'systemVolume', {'action': 'get'}),
    ('Mute the sound', 'systemVolume', {'action': 'mute'}),
    ('Unmute the sound', 'systemVolume', {'action': 'unmute'}),
    ('Skip this song', 'mediaControl', {'action': 'next'}),
    ('Skip this track.', 'mediaControl', {'action': 'next'}),
    ('Go back a song', 'mediaControl', {'action': 'previous'}),
    ('Pause the song', 'mediaControl', {'action': 'pause'}),
    ('Resume the song', 'mediaControl', {'action': 'play'}),
    ('What song is this?', 'mediaControl', {'action': 'now_playing'}),
    ("What's playing?", 'mediaControl', {'action': 'now_playing'}),
    ('How hot is my GPU?', 'systemInfo', {'action': 'gpu'}),
    ("What's my GPU temperature?", 'systemInfo', {'action': 'gpu'}),
    ('How much disk space do I have?', 'systemInfo', {'action': 'disk'}),
])
def test_whole_command(text, tool, args):
    from jarvis.fastpath.matcher import match
    result = match(text, 'en', targets=targets(), available_tools=TOOLS)
    assert result is not None
    assert (result.tool_name, result.args) == (tool, args)


@pytest.mark.parametrize('text', [
    'Open something useful', 'Can you help me decide what app to use?',
    'Open Word and then summarise this document', 'Open Word; close Chrome',
    'Why is my computer using so much RAM?', 'I wonder if you can open Word',
    'Do not open Word', 'Open NotInstalled', 'Open Word tomorrow',
    'Set volume to -1%', 'Set volume to 101%', 'Set volume to thirty%',
    'Set volume to 30%%', 'Set volume to 3e1%', 'Set volume to 30-ish%',
    'Set volume to 30.5.2%', 'Set volume to +30%',
    'Mute and unmute', 'Next song after this one', 'What time is it in Tokyo?',
    'Close everything', 'Force close Word', 'Delete Downloads', 'Shutdown',
    'Open Word or Chrome', 'Open Word & launch Chrome', 'Open Word\nclose Chrome',
    'Play music by Mozart', 'I said pause the music', 'Can you open Word for me later?',
    'What is the volume of a sphere?', 'Should I mute the sound?', 'What time is it now in Tokyo?',
    "What's playing at the cinema?", 'What song is this from?', 'How hot is it today?',
    'Skip this song and the next one', 'How much disk space does Word need?',
])
def test_uncertain_or_compound_requests_fall_through(text):
    from jarvis.fastpath.matcher import match
    assert match(text, 'en', targets=targets(), available_tools=TOOLS) is None


def test_unsupported_language_or_unavailable_tool_falls_through():
    from jarvis.fastpath.matcher import match
    assert match('Open Word', 'fr', targets=targets(), available_tools=TOOLS) is None
    assert match('Open Word', 'en', targets=targets(), available_tools={'getTime'}) is None
    assert match('Open Word', 'en', available_tools=TOOLS) is None


def test_ambiguous_catalogue_and_close_fuzzy_candidates_fall_through():
    from jarvis.fastpath.matcher import FastTarget, match
    ambiguous = (FastTarget(('word',), 'Word A', 'worda', 'Word A'),
                 FastTarget(('word',), 'Word B', 'wordb', 'Word B'))
    assert match('Open Word', targets=ambiguous, available_tools=TOOLS) is None
    near = (FastTarget(('application1',), 'Application1', 'app1', 'Application1'),
            FastTarget(('application2',), 'Application2', 'app2', 'Application2'))
    assert match('Open application', targets=near, available_tools=TOOLS) is None


def test_spelling_tolerance_cannot_change_version_numbers():
    from jarvis.fastpath.matcher import FastTarget, match
    apps = (FastTarget(('matlab r2026a',), 'MATLAB R2026a', 'matlab', 'MATLAB R2026a'),)
    assert match('Open MATLAB R2025a', targets=apps, available_tools=TOOLS) is None


def test_only_small_unambiguous_spelling_variation_matches():
    from jarvis.fastpath.matcher import FastTarget, match
    apps = (FastTarget(('matlab',), 'MATLAB', 'matlab', 'MATLAB'),)
    assert match('Open mattlab', targets=apps, available_tools=TOOLS) is not None
    assert match('Open chatlab', targets=apps, available_tools=TOOLS) is None


def test_disabled_config_falls_through(mock_config):
    from jarvis.fastpath.dispatcher import match_command
    mock_config.fast_commands_enabled = False
    assert match_command('What time is it?', mock_config) is None


def test_application_snapshot_does_not_start_discovery_or_wait():
    from jarvis.platform.windows.apps import ApplicationIndex
    index = ApplicationIndex()
    assert index.snapshot() is None
    assert not index._started


def test_catalogue_duplicates_share_application_identity(mock_config, monkeypatch):
    from jarvis.platform.windows.apps import Application, APP_INDEX
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    from jarvis.fastpath.matcher import match
    catalogue = (Application('Google Chrome', 'Chrome.lnk', 'C:/apps/chrome.exe'),
                 Application('chrome', 'C:/apps/chrome.exe', 'C:/apps/chrome.exe'))
    monkeypatch.setattr(APP_INDEX, 'snapshot', lambda: catalogue)
    mock_config.windows_app_aliases = {'browser': 'Google Chrome'}
    known = AppControlTool().fast_targets(mock_config)
    for text in ('Open Chrome', 'Open browser'):
        route = match(text, targets=known, available_tools=TOOLS)
        assert route is not None
        assert route.args == {'action': 'open', 'target': 'Google Chrome'}


def test_configured_disable_and_locale_filter_are_loaded(monkeypatch):
    import jarvis.config as config
    from jarvis.fastpath.dispatcher import match_command
    monkeypatch.setattr(config, '_load_json', lambda path: {'fast_commands_enabled': False, 'fast_commands_locales': ['fr']})
    cfg = config.load_settings()
    assert match_command('What time is it?', cfg, 'en') is None
    monkeypatch.setattr(config, '_load_json', lambda path: {'fast_commands_enabled': True, 'fast_commands_locales': []})
    assert match_command('What time is it?', config.load_settings(), 'en') is None


@pytest.mark.parametrize('suffix', ['no', 'not', '2', 'later', 'please no'])
def test_long_application_name_cannot_absorb_extra_tokens(suffix):
    from jarvis.fastpath.matcher import FastTarget, match
    apps = (FastTarget(('microsoft visual studio code',), 'Microsoft Visual Studio Code', 'code', 'Code'),)
    assert match('Close Microsoft Visual Studio Code ' + suffix,
                 targets=apps, available_tools=TOOLS) is None


def test_window_actions_cannot_conflate_distinct_installed_versions():
    from jarvis.fastpath.matcher import FastTarget, match
    apps = (FastTarget(('matlab r2026a',), 'MATLAB R2026a', 'matlab', 'MATLAB R2026a'),
            FastTarget(('matlab r2025b',), 'MATLAB R2025b', 'matlab', 'MATLAB R2025b'))
    assert match('Close MATLAB R2026a', targets=apps, available_tools=TOOLS) is None


def test_catalogue_emits_targets_that_preserve_alias_resolution(mock_config, monkeypatch):
    from jarvis.platform.windows.apps import Application, APP_INDEX, resolve_application
    from jarvis.tools.builtin.windows.desktop_control import AppControlTool
    from jarvis.fastpath.matcher import match
    apps = (Application('Microsoft Word', 'word.lnk', 'C:/apps/winword.exe'),
            Application('Google Chrome', 'chrome.lnk', 'C:/apps/chrome.exe'))
    monkeypatch.setattr(APP_INDEX, 'snapshot', lambda: apps)
    mock_config.windows_app_aliases = {'Microsoft Word': 'Google Chrome'}
    route = match('Open winword', targets=AppControlTool().fast_targets(mock_config), available_tools=TOOLS)
    assert route is not None
    actual = resolve_application(route.args['target'], apps, mock_config.windows_app_aliases)
    assert actual.target == 'word.lnk'

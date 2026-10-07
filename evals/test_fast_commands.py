"""Golden utterance corpus for deterministic-versus-conversational routing.

No LLM judge is needed: the observable result is a concrete tool/argument
pair or a deliberate fallthrough, independently labelled for each utterance.
"""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.eval

CASES = [
    ('What time is it?', 'getTime', None),
    ("What's today's date?", 'getTime', None),
    ('PLEASE OPEN WORD!', 'appControl', 'open'),
    ('Could you launch Chrome please?', 'appControl', 'open'),
    ('Open MATLAB', 'appControl', 'open'),
    ('Switch to Spotify', 'appControl', 'focus'),
    ('Close Word', 'appControl', 'close'),
    ('Minimize Chrome', 'windowControl', 'minimise'),
    ('Maximize Word', 'windowControl', 'maximise'),
    ('Restore Spotify', 'windowControl', 'restore'),
    ('Set volume to 30%', 'systemVolume', 'set'),
    ('Turn the volume up', 'systemVolume', 'up'),
    ('Turn the volume down', 'systemVolume', 'down'),
    ('Mute the computer', 'systemVolume', 'mute'),
    ('Unmute', 'systemVolume', 'unmute'),
    ('Pause the music', 'mediaControl', 'pause'),
    ('Play the music', 'mediaControl', 'play'),
    ('Next track', 'mediaControl', 'next'),
    ('Previous song', 'mediaControl', 'previous'),
    ("What's using the most RAM?", 'systemInfo', 'top_memory'),
    ("What's using the most CPU?", 'systemInfo', 'top_cpu'),
    ('How much RAM am I using?', 'systemInfo', 'ram'),
    ("What's my CPU usage?", 'systemInfo', 'cpu'),
    ('Open Downloads', 'openPath', None),
    ('Open Documents', 'openPath', None),
    ('Open Desktop', 'openPath', None),
    ('Open Bluetooth settings', 'systemSettings', 'open_page'),
    ('Open the sound settings', 'systemSettings', 'open_page'),
    ('Set brightness to 40%', 'systemSettings', 'brightness'),
    ('Turn the brightness down', 'systemSettings', 'brightness'),
    ('Switch to the balanced power plan', 'systemSettings', 'power_plan'),
    ('Switch audio to my headphones', 'systemSettings', 'audio_output'),
    ('Switch to desktop 2', 'windowControl', 'desktop_switch'),
    ('Next desktop', 'windowControl', 'desktop_switch'),
    ('Create a new desktop', 'windowControl', 'desktop_new'),
    ('Press control shift escape', 'inputControl', 'hotkey'),
    ('What time is it now?', 'getTime', None),
    ('What day is it today?', 'getTime', None),
    ("What's the volume?", 'systemVolume', 'get'),
    ('What is the current volume?', 'systemVolume', 'get'),
    ('Mute the sound.', 'systemVolume', 'mute'),
    ('Skip this song.', 'mediaControl', 'next'),
    ('Go back a song', 'mediaControl', 'previous'),
    ('What song is this?', 'mediaControl', 'now_playing'),
    ("What's playing?", 'mediaControl', 'now_playing'),
    ('How hot is my GPU?', 'systemInfo', 'gpu'),
    ('How much disk space do I have?', 'systemInfo', 'disk'),
    ('Make the screen a bit dimmer.', 'systemSettings', 'brightness'),
    ('Show the desktop', 'inputControl', 'hotkey'),
    ('Open something useful', None, None),
    ('Open nonsense settings', None, None),
    ('Set brightness to 140%', None, None),
    ('Press alt f4', None, None),
    ('Press delete', None, None),
    ('Switch to desktop 2 and open Word', None, None),
    ('Should I open the Bluetooth settings?', None, None),
    ('Why is my brightness so low?', None, None),
    ('Can you help me decide what app to use?', None, None),
    ('Open Word and then summarise this document', None, None),
    ('Why is my computer using so much RAM?', None, None),
    ('I was thinking about opening Word', None, None),
    ('Open imaginaryapp', None, None),
    ('Set volume to 999%', None, None),
    ('Close all apps', None, None),
    ('Should I pause the music?', None, None),
    ('What time is it in Paris?', None, None),
    ('Open Word; maximise Chrome', None, None),
    ('Never mute the computer', None, None),
    ('What is the volume of a sphere?', None, None),
    ('Should I skip this song?', None, None),
    ("What's playing at the cinema tonight?", None, None),
    ('How hot is it outside?', None, None),
    ('What song is this from?', None, None),
    ('What time is it now in Paris?', None, None),
    # Several Start Menu entries run cmd.exe, explorer.exe or msiexec.exe (see the catalogue below).
    ('Open Command Prompt', 'appControl', 'open'),
    ('Launch Stop Dashboard', 'appControl', 'open'),
    ('Close Command Prompt', None, None),
    ('Open explorer', None, None),
    ('Open Uninstall Node.js', None, None),
]


@pytest.mark.parametrize('query,tool,action', CASES)
def test_fast_versus_fallthrough_corpus(query, tool, action, monkeypatch):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.platform.windows.apps import APP_INDEX, Application
    from jarvis.tools.registry import configure_windows_tools
    import jarvis.tools.registry as registry
    monkeypatch.setattr(registry, 'BUILTIN_TOOLS', registry.BUILTIN_TOOLS.copy())
    cfg = SimpleNamespace(windows_tools_enabled=True, windows_app_aliases={},
                          fast_commands_enabled=True, fast_commands_locales=['en'])
    configure_windows_tools(cfg, platform='win32', start_index=False)
    monkeypatch.setattr(APP_INDEX, 'snapshot', lambda: (
        Application('Word', 'word.lnk', 'C:/apps/winword.exe'),
        Application('Google Chrome', 'chrome.lnk', 'C:/apps/chrome.exe'),
        Application('MATLAB', 'matlab.lnk', 'C:/apps/matlab.exe'),
        Application('Spotify', 'spotify.lnk', 'C:/apps/spotify.exe'),
        Application('Command Prompt', 'cmd.lnk', 'C:/Windows/System32/cmd.exe'),
        Application('RGB Lighting Control', 'rgb.lnk', 'C:/Windows/System32/cmd.exe', '/c rgb.bat'),
        Application('Start Dashboard', 'start.lnk', 'C:/Python/pythonw.exe', 'dashboard.py start'),
        Application('Stop Dashboard', 'stop.lnk', 'C:/Python/pythonw.exe', 'dashboard.py stop'),
        Application('Windows Software Development Kit', 'sdk.lnk', 'C:/Windows/explorer.exe', 'C:/Kits/10'),
        Application('Uninstall Node.js', 'node.lnk', 'C:/Windows/System32/msiexec.exe', '/x {GUID}',
                    uninstaller=True),
    ))
    route = match_command(query, cfg, 'en')
    if tool is None:
        assert route is None, f'Unsafe/uncertain bypass: {query}: {route}'
    else:
        assert route is not None, f'Missing confident route: {query}'
        assert (route.tool_name, route.args.get('action')) == (tool, action)

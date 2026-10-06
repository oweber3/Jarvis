"""TV commands are deterministic fast routes that name the TV, so the PC's own commands are untouched."""
import json
from pathlib import Path

import pytest

TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'tvControl', 'openPath'}


def tv_apps(*names):
    from jarvis.fastpath.matcher import FastTarget
    return tuple(FastTarget((name,), name, str(i), name) for i, name in enumerate(names, 1))


APPS = tv_apps('Netflix', 'Prime Video', 'Hulu', 'Disney Plus', 'HBO Max', 'Apple TV', 'YouTube', 'HDMI 1')


def route(text, apps=APPS, tools=TOOLS, targets=()):
    from jarvis.fastpath.matcher import match
    return match(text, 'en', tv_apps=apps, targets=targets, available_tools=tools)


def locale_rule(rule_id):
    data = json.loads((Path(__file__).resolve().parents[1] / 'src/jarvis/fastpath/phrases/en.json')
                      .read_text(encoding='utf-8'))
    return next(rule for rule in data['rules'] if rule['id'] == rule_id)


@pytest.mark.parametrize('text,rule_id', [
    ('Turn on the TV', 'tv.power_on'), ('turn the tv on', 'tv.power_on'), ('Switch on the TV', 'tv.power_on'),
    ('Turn off the TV', 'tv.power_off'), ('turn the TV off', 'tv.power_off'),
    ('Please turn off the tv', 'tv.power_off'), ('switch the tv off', 'tv.power_off'),
    ('Turn on my TV.', 'tv.power_on'), ('mute my tv', 'tv.mute'), ('turn my tv volume down', 'tv.volume_down'),
    ('TV volume up', 'tv.volume_up'), ('Turn the TV volume up', 'tv.volume_up'),
    ('turn up the tv volume', 'tv.volume_up'), ('Raise the TV volume', 'tv.volume_up'),
    ('TV volume down', 'tv.volume_down'), ('Turn the TV volume down', 'tv.volume_down'),
    ('lower the tv volume', 'tv.volume_down'),
    ('Mute the TV', 'tv.mute'), ('unmute the tv', 'tv.mute'),
    ('Pause the TV', 'tv.play_pause'), ('Play the TV', 'tv.play_pause'), ('resume the tv', 'tv.play_pause'),
    ('TV home', 'tv.home'), ('Go to the TV home screen', 'tv.home'),
])
def test_tv_key_phrases_send_the_rules_key(text, rule_id):
    rule = locale_rule(rule_id)
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('tvControl', rule['args'])
    assert rule['args']['action'] == 'key' and rule['args']['key']


def test_volume_rules_step_more_than_once_so_the_change_is_audible():
    assert locale_rule('tv.volume_up')['args']['repeat'] > 1
    assert locale_rule('tv.volume_down')['args']['repeat'] > 1


@pytest.mark.parametrize('text,app', [
    ('Put on Netflix', 'Netflix'), ('put Netflix on', 'Netflix'),
    ('Open Hulu on the TV', 'Hulu'), ('launch disney plus on the tv', 'Disney Plus'),
    ('Start YouTube on the TV', 'YouTube'), ('watch Prime Video on the tv', 'Prime Video'),
    ('Please put on Apple TV', 'Apple TV'), ('Put on HBO Max', 'HBO Max'), ('put on hdmi 1', 'HDMI 1'),
    ('Put on Netflixx', 'Netflix'),
])
def test_tv_app_phrases_resolve_against_the_apps_the_tv_reported(text, app):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('tvControl', {'action': 'launch', 'app': app})
    assert result.slots['tv_app'] == app


def test_an_app_the_tv_does_not_have_falls_through():
    assert route('Put on Crunchyroll') is None
    assert route('Open Crunchyroll on the TV') is None


def test_without_a_cached_app_list_launch_phrases_fall_through():
    assert route('Put on Netflix', apps=()) is None


def test_ambiguous_tv_apps_fall_through():
    apps = tv_apps('Plex Player', 'Plex Media')
    assert route('Put on Plex', apps=apps) is None


@pytest.mark.parametrize('text,tool,args', [
    ('Turn the volume down', 'systemVolume', {'action': 'down'}),
    ('Turn the volume up', 'systemVolume', {'action': 'up'}),
    ('Mute', 'systemVolume', {'action': 'mute'}),
    ('Mute the computer', 'systemVolume', {'action': 'mute'}),
    ('Pause the music', 'mediaControl', {'action': 'pause'}),
])
def test_pc_commands_still_go_to_the_pc(text, tool, args):
    result = route(text)
    assert (result.tool_name, result.args) == (tool, args)


def test_open_on_the_pc_does_not_take_the_tv_route():
    from jarvis.fastpath.matcher import FastTarget
    pc = (FastTarget(('netflix',), 'Netflix', 'netflix', 'Netflix'),)
    result = route('Open Netflix', targets=pc)
    assert result is not None and result.tool_name == 'appControl'


@pytest.mark.parametrize('text', [
    'Turn off the TV and open Word', 'Turn off the TV; mute it', 'Do not turn off the TV',
    'Turn the TV volume down to 20', 'Turn the TV up', 'What is on the TV',
    'Put on some music', 'Put on Netflix and Hulu', 'Turn off', 'Turn on', 'Pause',
    'Why is the TV volume so loud',
])
def test_uncertain_requests_fall_through(text):
    assert route(text) is None, text


def test_without_the_tool_no_tv_phrase_matches():
    without = TOOLS - {'tvControl'}
    for text in ('Turn on the TV', 'Mute the TV', 'Put on Netflix'):
        assert route(text, tools=without) is None


def test_other_languages_fall_through():
    from jarvis.fastpath.matcher import match
    assert match('Turn on the TV', 'fr', tv_apps=APPS, available_tools=TOOLS) is None


# --- dispatcher ------------------------------------------------------------------------------------

@pytest.fixture
def configured(fake_tv, mock_config):
    from jarvis.devices.roku import get_device
    from jarvis.tools.builtin.tv_control import TvControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    mock_config.roku_host = '127.0.0.1'
    mock_config.windows_tools_enabled = False
    BUILTIN_TOOLS['tvControl'] = TvControlTool()
    get_device('127.0.0.1').apps()
    fake_tv.requests.clear()
    yield fake_tv
    BUILTIN_TOOLS.pop('tvControl', None)


def test_dispatcher_matches_tv_phrases_when_the_tool_is_registered(configured, mock_config):
    from jarvis.fastpath.dispatcher import match_command
    assert match_command('Turn off the TV', mock_config).args == {'action': 'key', 'key': 'PowerOff'}
    assert match_command('Put on Netflix', mock_config).args == {'action': 'launch', 'app': 'Netflix'}
    assert configured.requests == []  # matching never touches the TV


def test_dispatcher_does_not_match_tv_phrases_without_the_tool(mock_config):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS.pop('tvControl', None)
    assert match_command('Turn off the TV', mock_config) is None


def test_dispatch_executes_once_and_replies_with_the_template(configured, mock_config):
    from jarvis.fastpath.dispatcher import dispatch, match_command
    result = match_command('Turn the TV volume down', mock_config)
    reply = dispatch(result, None, mock_config, 'Turn the TV volume down', quiet=True)
    assert configured.keys() == ['VolumeDown'] * locale_rule('tv.volume_down')['args']['repeat']
    assert 'TV' in reply


def test_dispatch_reports_a_forbidden_tv_verbatim_not_as_success(configured, mock_config):
    from jarvis.fastpath.dispatcher import dispatch, match_command
    configured.mode = 'limited'
    result = match_command('Turn off the TV', mock_config)
    reply = dispatch(result, None, mock_config, 'Turn off the TV', quiet=True)
    assert 'Control by mobile apps' in reply

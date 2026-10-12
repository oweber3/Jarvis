"""Starting and stopping gesture snapping are whole-utterance fast routes, available only while the tool is."""
import pytest

TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'gestureControl'}


def route(text, tools=TOOLS):
    from jarvis.fastpath.matcher import match
    return match(text, 'en', targets=(), available_tools=tools)


@pytest.mark.parametrize('text', ['Start gesture control', 'turn on gesture control', 'enable gesture control',
                                  'Turn on gesture snapping', 'start gesture snapping', 'Please start gesture control.'])
def test_start_phrases_start_gesture_snapping(text):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': 'start'})


@pytest.mark.parametrize('text', ['Stop gesture control', 'turn off gesture control', 'disable gesture control',
                                  'Turn off gesture snapping', 'stop gesture snapping', 'turn gesture snapping off'])
def test_stop_phrases_stop_gesture_snapping(text):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': 'stop'})


@pytest.mark.parametrize('text', ['Start gesture controls', 'start gestures', 'Turn on gesture controls',
                                  'turn the gesture controls on', 'switch on gestures', 'enable hand gestures',
                                  'activate gesture mode', 'turn on the gesture controls', 'gestures on',
                                  'start using gestures', 'Could you start gesture controls please?',
                                  'start the gesture control', 'switch gesture snapping on'])
def test_everyday_start_variants_start_gesture_snapping(text):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': 'start'})


@pytest.mark.parametrize('text', ['Stop gesture controls', 'stop gestures', 'Turn off gesture controls',
                                  'turn the gesture controls off', 'switch off gestures', 'disable hand gestures',
                                  'deactivate gesture mode', 'turn off the gesture controls', 'gestures off',
                                  'stop using gestures', 'Can you stop the gestures please',
                                  'switch gesture snapping off'])
def test_everyday_stop_variants_stop_gesture_snapping(text):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': 'stop'})


def _bundled_gesture_phrases():
    import json
    from pathlib import Path
    import jarvis.fastpath
    for path in sorted((Path(jarvis.fastpath.__file__).parent / 'phrases').glob('*.json')):
        for rule in json.loads(path.read_text(encoding='utf-8'))['rules']:
            if rule['tool'] == 'gestureControl':
                for phrase in rule['phrases']:
                    yield path.stem, phrase, rule['args']


@pytest.mark.parametrize('language,phrase,args', list(_bundled_gesture_phrases()))
def test_every_gesture_phrase_routes_to_its_own_action_beside_apps_and_routines(language, phrase, args):
    """No other family (apps, routines, devices) claims a gesture phrase, so none of them falls through."""
    from jarvis.fastpath.matcher import FastTarget, match
    apps = tuple(FastTarget((name,), name, f'{name}.exe', name) for name in ('Word', 'Chrome', 'Spotify'))
    routines = (FastTarget(('morning',), 'morning', 'morning', 'morning'),)
    result = match(phrase, language, targets=apps, routines=routines,
                   available_tools=TOOLS | {'routineControl', 'systemSettings', 'openPath'})
    assert result is not None, phrase
    assert (result.tool_name, result.args) == ('gestureControl', args)


def test_without_the_tool_the_phrases_fall_through():
    tools = TOOLS - {'gestureControl'}
    assert route('Start gesture control', tools) is None
    assert route('Stop gesture control', tools) is None


@pytest.mark.parametrize('text', ['start gesture control and open word', 'what is gesture control',
                                  'stop the music', 'what are gesture controls', 'tell me about gestures',
                                  'how do I turn on gesture controls', 'are gestures on', 'is gesture control on',
                                  'gestures', 'gesture controls', 'turn off touchpad gestures',
                                  'start gestures and open word', 'explain gesture mode', 'stop gestures then mute',
                                  'why did gesture controls stop'])
def test_other_requests_are_not_gesture_snap_routes(text):
    result = route(text)
    assert result is None or result.tool_name != 'gestureControl'


def test_the_routes_are_routine_and_so_eligible(mock_config):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    for args in ({'action': 'start'}, {'action': 'stop'}):
        assert evaluate_safety('gestureControl', args, mock_config, tool=GestureControlTool()).tier == SafetyTier.SAFE


@pytest.mark.parametrize('text,action', [('click', 'click'), ('Click.', 'click'), ('click it', 'click'),
                                         ('click here', 'click'), ('left click', 'click'),
                                         ('double click', 'double_click'), ('double-click', 'double_click'),
                                         ('right click', 'right_click'), ('Right-click.', 'right_click')])
def test_click_phrases_click(text, action):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': action})


@pytest.mark.parametrize('text', ['click the save button', 'click on settings', 'how do I right click',
                                  'what does double click do', 'clicking'])
def test_other_click_requests_are_not_spoken_clicks(text):
    result = route(text)
    assert result is None or result.tool_name != 'gestureControl'


def test_a_click_route_waits_for_gesture_snapping_to_run(mock_config, monkeypatch):
    """Matched or not, a click is dispatched only while gesture snapping runs; otherwise the model decides."""
    from jarvis.fastpath import dispatcher
    from jarvis.gestures import window_snap_service
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    monkeypatch.setitem(BUILTIN_TOOLS, 'gestureControl', GestureControlTool())
    monkeypatch.setattr(window_snap_service, '_service', None)
    assert dispatcher.match_command('click', mock_config, 'en') is None
    started = dispatcher.match_command('start gesture control', mock_config, 'en')
    assert started is not None and started.args == {'action': 'start'}



def test_a_spoken_click_works_addressed_and_in_the_hot_window(mock_config, monkeypatch):
    """No wake word needed: the hot window and addressed requests both click (bare speech: test_bare_click.py)."""
    from jarvis.fastpath import dispatcher
    from jarvis.gestures import window_snap_service
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS

    class Running:
        running = True
    monkeypatch.setitem(BUILTIN_TOOLS, 'gestureControl', GestureControlTool())
    monkeypatch.setattr(window_snap_service, '_service', Running())
    assert dispatcher.match_command('click', mock_config, 'en', addressed=False).args == {'action': 'click'}
    assert dispatcher.match_command('click', mock_config, 'en', addressed=True).args == {'action': 'click'}

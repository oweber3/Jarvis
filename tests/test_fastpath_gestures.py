"""Starting and stopping the air mouse are whole-utterance fast routes, available only while the tool is."""
import pytest

TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'gestureControl'}


def route(text, tools=TOOLS):
    from jarvis.fastpath.matcher import match
    return match(text, 'en', targets=(), available_tools=tools)


@pytest.mark.parametrize('text', ['Start gesture control', 'turn on gesture control', 'enable gesture control',
                                  'Turn on the air mouse', 'start air mouse', 'Please start gesture control.'])
def test_start_phrases_start_the_air_mouse(text):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': 'start'})


@pytest.mark.parametrize('text', ['Stop gesture control', 'turn off gesture control', 'disable gesture control',
                                  'Turn off the air mouse', 'stop air mouse', 'turn the air mouse off'])
def test_stop_phrases_stop_the_air_mouse(text):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('gestureControl', {'action': 'stop'})


def test_without_the_tool_the_phrases_fall_through():
    tools = TOOLS - {'gestureControl'}
    assert route('Start gesture control', tools) is None
    assert route('Stop gesture control', tools) is None


@pytest.mark.parametrize('text', ['start gesture control and open word', 'what is gesture control',
                                  'stop the music'])
def test_other_requests_are_not_air_mouse_routes(text):
    result = route(text)
    assert result is None or result.tool_name != 'gestureControl'


def test_the_routes_are_routine_and_so_eligible(mock_config):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    for args in ({'action': 'start'}, {'action': 'stop'}):
        assert evaluate_safety('gestureControl', args, mock_config, tool=GestureControlTool()).tier == SafetyTier.SAFE

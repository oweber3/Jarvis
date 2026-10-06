"""Local-model routing and argument evals. OS actions are never executed."""
import json
import os

import pytest

CASES = [
    ('Open Word', 'appControl', 'open', 'word'),
    ('Open Chrome', 'appControl', 'open', 'chrome'),
    ('Open MATLAB', 'appControl', 'open', 'matlab'),
    ('Open my Downloads folder', 'openPath', None, 'downloads'),
    ('Switch to Spotify', 'appControl', 'focus', 'spotify'),
    ('Minimize Chrome', 'windowControl', 'minimise', 'chrome'),
    ('Open my design workspace', 'workspaceControl', 'open', 'design'),
    ('Set up my design workspace', 'workspaceControl', 'open', 'design'),
]


@pytest.fixture(autouse=True)
def workspace_tool_offered():
    """The router sees workspaceControl as it does once a user has configured a workspace, so every
    case here also checks that the extra tool does not disturb routing of the others."""
    from jarvis.tools.builtin.windows import WorkspaceControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS['workspaceControl'] = WorkspaceControlTool()
    yield
    BUILTIN_TOOLS.pop('workspaceControl', None)


def _ask(query):
    """Route one utterance through the local model's tool selection and chat call."""
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_json_schema, generate_tools_description
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.selection import select_tools, ToolSelectionStrategy

    backend = OllamaBackend('http://127.0.0.1:11434')
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=30)
    response = backend.chat(model, [
        {'role': 'system', 'content': build_system_prompt() + '\n' + generate_tools_description(selected)},
        {'role': 'user', 'content': query}], timeout_sec=45,
        extra_options={'temperature': 0, 'num_predict': 1024},
        tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    return selected, response.get('message', {}).get('tool_calls', [])


def _arguments(call):
    args = call['function']['arguments']
    return json.loads(args) if isinstance(args, str) else args


@pytest.mark.eval
@pytest.mark.parametrize('query,tool,action,target', CASES)
def test_local_windows_command_tool_path(query, tool, action, target):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: selected {selected}'
    assert len(calls) == 1, f'{query}: expected one tool call, got {calls}'
    assert calls[0]['function']['name'] == tool
    args = _arguments(calls[0])
    if action:
        assert args.get('action') == action
    assert args.get('target', '').casefold() == target


# A model that does not know the display identifiers may look them up first, or place directly;
# either is correct. A placement request must never be reduced to an unplaced open or a bare restore.
PLACEMENT_CASES = [
    ('Move Chrome to my left monitor', {('windowControl', 'place'), ('windowControl', 'displays')}),
    ('Put Apple Music on my second display', {('windowControl', 'place'), ('windowControl', 'displays')}),
    ('Open Word on my second monitor', {('appControl', 'open'), ('windowControl', 'displays')}),
]


@pytest.mark.eval
@pytest.mark.parametrize('query,accepted', PLACEMENT_CASES)
def test_local_placement_requests_use_placement_arguments(query, accepted):
    selected, calls = _ask(query)
    assert len(calls) == 1, f'{query}: expected one tool call, got {calls}'
    name, args = calls[0]['function']['name'], _arguments(calls[0])
    assert (name, args.get('action')) in accepted, f'{query}: called {name} with {args}'
    if args.get('action') != 'displays':
        assert str(args.get('monitor', '')).strip(), f'{query}: placement ignored the display: {args}'


# Natural monitor references and zone names: the structured arguments the model emits must select the
# intended display and zone through the real resolver. Looking displays up first is equally correct.
# Layout used for resolution: DISPLAY2 sits left of the primary DISPLAY1.
ZONE_CASES = [
    ('Open Apple Music in the left zone of my second monitor', ('appControl', 'open'), r'\\.\display2', 'left'),
    ('Move Chrome into zone 2 of my primary monitor', ('windowControl', 'place'), r'\\.\display1', '2'),
    ('Move Chrome to the right monitor', ('windowControl', 'place'), r'\\.\display1', None),
    ('Open Notepad on my left monitor in the top zone', ('appControl', 'open'), r'\\.\display2', 'top'),
]


def _intended_display(monitor):
    from jarvis.platform.windows.displays import Monitor, resolve_monitor
    primary = Monitor(r'\\.\DISPLAY1', (0, 0, 2560, 1440), (0, 0, 2560, 1392), True)
    left = Monitor(r'\\.\DISPLAY2', (-1080, -162, 0, 1758), (-1080, -162, 0, 1710), False)
    try:
        return resolve_monitor(monitor, [primary, left], {}).device.casefold()
    except ValueError:
        return ''  # not a reference the tool accepts, so it selects no display


@pytest.mark.eval
@pytest.mark.parametrize('query,action,device,zone', ZONE_CASES)
def test_local_zone_requests_resolve_to_the_intended_display_and_zone(query, action, device, zone):
    selected, calls = _ask(query)
    assert len(calls) == 1, f'{query}: expected one tool call, got {calls}'
    name, args = calls[0]['function']['name'], _arguments(calls[0])
    if (name, args.get('action')) == ('windowControl', 'displays'):
        return  # looking the layout up first is acceptable
    assert (name, args.get('action')) == action, f'{query}: called {name} with {args}'
    assert _intended_display(str(args.get('monitor', ''))) == device, f'{query}: wrong display in {args}'
    if zone:
        assert str(args.get('zone', '')).strip().casefold() == zone, f'{query}: wrong zone in {args}'


# System controls. These phrasings are deliberately outside the deterministic fast path, so the model
# has to pick the tool and the arguments. Each expectation lists accepted values per argument.
SYSTEM_CASES = [
    ('Take me to the Wi-Fi settings', 'systemSettings', {'action': {'open_page'}, 'page': {'wifi', 'wi-fi'}}),
    ('Could you make my screen a bit dimmer?', 'systemSettings', {'action': {'brightness'}}),
    ('Put the computer in high performance mode', 'systemSettings',
     {'action': {'power_plan'}, 'name': {'high_performance', 'high performance'}}),
    ('Send my sound to the headphones', 'systemSettings', {'action': {'audio_output'}}),
    ('Move Notepad to desktop 2', 'windowControl', {'action': {'move_to_desktop'}, 'desktop': {'2'}}),
    ('What is on my clipboard right now?', 'inputControl', {'action': {'clipboard_read'}}),
    ('Open my proposal', 'openPath', {'target': {'proposal', 'my proposal'}}),
    ('Open Elden Ring', 'appControl', {'action': {'open'}, 'target': {'elden ring'}}),
]


@pytest.mark.eval
@pytest.mark.parametrize('query,tool,expected', SYSTEM_CASES)
def test_local_system_control_requests_pick_the_right_tool(query, tool, expected):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: selected {selected}'
    assert len(calls) == 1, f'{query}: expected one tool call, got {calls}'
    assert calls[0]['function']['name'] == tool, f'{query}: called {calls[0]["function"]["name"]}'
    args = _arguments(calls[0])
    for key, accepted in expected.items():
        assert str(args.get(key, '')).strip().casefold() in accepted, f'{query}: {key} was {args.get(key)!r} in {args}'

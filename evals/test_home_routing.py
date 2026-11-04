"""Smart home routing: home devices reach homeControl, and TV, PC volume and media requests do not.

Runs the local model's tool selection and chat call with ``homeControl`` offered next to ``tvControl`` and
the Windows tools. Nothing is executed and no hub is contacted.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_home_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
import json
import os

import pytest

# (utterance, tool, expected arguments, or None to check the tool only)
CASES = [
    ('Turn off the kitchen lights', 'homeControl', {'action': 'turn_off'}),
    ('Turn on the bedroom lamp', 'homeControl', {'action': 'turn_on'}),
    ('Dim the living room lights to 30 percent', 'homeControl', {'action': 'set'}),
    ('Make the desk light blue', 'homeControl', {'action': 'set'}),
    ('Is the garage door open?', 'homeControl', {'action': 'status'}),
    ("What's the temperature in the bedroom?", 'homeControl', {'action': 'status'}),
    ('Set the thermostat to 21 degrees', 'homeControl', {'action': 'set'}),
    ('Close the blinds in the lounge', 'homeControl', {'action': 'close'}),
    ('Lock the front door', 'homeControl', {'action': 'lock'}),
    ('Turn on the coffee machine plug', 'homeControl', {'action': 'turn_on'}),
    ('Activate movie night', 'homeControl', {'action': 'activate'}),
    ('What smart devices do I have?', 'homeControl', {'action': 'list'}),
    # Neighbours it must not steal from.
    ('Turn off the TV', 'tvControl', {'action': 'key', 'key': 'PowerOff'}),
    ('Mute the TV', 'tvControl', {'action': 'key', 'key': 'VolumeMute'}),
    ('Turn the volume down', 'systemVolume', {'action': 'down'}),
    ('Pause the music', 'mediaControl', {'action': 'pause'}),
    ('Open Word', 'appControl', None),
]


@pytest.fixture(autouse=True)
def home_and_tv_tools_offered():
    from jarvis.tools.builtin.home_control import HomeControlTool
    from jarvis.tools.builtin.tv_control import TvControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS['homeControl'] = HomeControlTool()
    BUILTIN_TOOLS['tvControl'] = TvControlTool()
    yield
    BUILTIN_TOOLS.pop('homeControl', None)
    BUILTIN_TOOLS.pop('tvControl', None)


def _ask(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend(os.environ.get('EVAL_OLLAMA_URL', 'http://127.0.0.1:11434'))
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=30)
    response = backend.chat(model, [
        {'role': 'system', 'content': build_system_prompt() + '\n' + generate_tools_description(selected)},
        {'role': 'user', 'content': query}], timeout_sec=60,
        extra_options={'temperature': 0, 'num_predict': 1024}, tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    return selected, response.get('message', {}).get('tool_calls', [])


@pytest.mark.eval
@pytest.mark.parametrize('query,tool,expected', CASES, ids=[c[0] for c in CASES])
def test_home_tv_and_pc_requests_reach_the_right_tool(query, tool, expected):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f'{query}: chose {name} {args}'
    for key, value in (expected or {}).items():
        assert str(args.get(key, '')).casefold() == str(value).casefold(), f'{query}: {args}'

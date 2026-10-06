"""Offline uiControl evals: replay a recorded UI Automation snapshot and score the model's next call.

No window is touched. Each case gives the local model a request, a uiControl snapshot call and its
recorded result (``evals/fixtures/uia``), then checks the action, element and value it chooses next.
Small models (``qwen3.5:0.8b``) are not expected to pass reliably; the scores are reported with the change.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_ui_control_choices.py -m eval -v
"""
import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / 'fixtures' / 'uia'


def _load(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))['snapshot']


def _ids(snapshot, name):
    return {e['id'].casefold() for e in snapshot['elements'] if e['name'].casefold().rstrip(':') == name.casefold()}


# (fixture, request, accepted actions, element names accepted (by id or name), words the value must contain)
CASES = [
    ('notepad_classic', 'Open the Format menu', {'menu', 'click', 'expand'}, 'Format', ['format']),
    ('notepad_classic', 'Turn on word wrap', {'menu'}, None, ['word wrap']),
    ('save_as_dialog', 'Click Save in this dialog', {'click'}, 'Save', []),
    ('save_as_dialog', 'Call the file lab results', {'set_text'}, 'File name', ['lab results']),
    ('settings_bluetooth', 'Turn Bluetooth off', {'toggle', 'click'}, 'Bluetooth', []),
    ('apple_music', 'Skip to the next song', {'click'}, 'Next', []),
    ('apple_music', 'Search Apple Music for Coldplay', {'set_text'}, 'Search', ['coldplay']),
    ('win32_controls', 'Tick word wrap', {'toggle', 'click'}, 'Word wrap', []),
    ('win32_controls', 'Set the subject to Quarterly report', {'set_text'}, 'Subject', ['quarterly report']),
    ('win32_controls', 'Pick Green as the colour', {'select'}, 'Colour', ['green']),
]


def _next_call(snapshot, request):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.builtin.windows import UiControlTool
    from jarvis.tools.builtin.windows.ui_control import snapshot_result
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema

    backend = OllamaBackend('http://127.0.0.1:11434')
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    BUILTIN_TOOLS.setdefault('uiControl', UiControlTool())
    tools = ['uiControl']
    messages = [
        {'role': 'system', 'content': build_system_prompt() + '\n' + generate_tools_description(tools)},
        {'role': 'user', 'content': request},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'function': {'name': 'uiControl', 'arguments': {'action': 'snapshot'}}}]},
        {'role': 'tool', 'content': json.dumps(snapshot_result(snapshot), ensure_ascii=False)},
    ]
    response = backend.chat(model, messages, timeout_sec=60, extra_options={'temperature': 0, 'num_predict': 1024},
                            tools=generate_tools_json_schema(tools))
    assert response, 'No model response'
    calls = response.get('message', {}).get('tool_calls', [])
    assert calls, f'{request}: no tool call, replied {response.get("message", {}).get("content", "")[:200]!r}'
    args = calls[0]['function']['arguments']
    return calls[0]['function']['name'], json.loads(args) if isinstance(args, str) else args


@pytest.mark.eval
@pytest.mark.parametrize('fixture,request_text,actions,element,value_words', CASES,
                         ids=[f'{c[0]}:{c[1]}' for c in CASES])
def test_model_chooses_the_right_control_from_a_snapshot(fixture, request_text, actions, element, value_words):
    snapshot = _load(fixture)
    name, args = _next_call(snapshot, request_text)
    assert name == 'uiControl', f'{request_text}: called {name}'
    assert args.get('action') in actions, f'{request_text}: {args}'
    chosen = str(args.get('element', '')).strip().casefold()
    value = str(args.get('value', '')).casefold()
    if element:
        menu_route = args.get('action') == 'menu' and element.casefold() in value
        assert menu_route or chosen in _ids(snapshot, element) or chosen.rstrip(':') == element.casefold(), (
            f'{request_text}: element {chosen!r} in {args}')
    if args.get('action') in ('menu', 'set_text', 'select'):
        assert all(word in value for word in value_words), f'{request_text}: value {args}'


# One-step requests through the router, with no snapshot taken: the control is named in the request,
# so acting on it by name in one call (or taking a snapshot first) are the only acceptable first calls.
ONE_STEP_CASES = [
    ('Click Save in this dialog', {'click'}, 'save', ''),
    ('In Notepad, open the Format menu', {'menu', 'click', 'expand'}, 'format', 'format'),
    ('Tick the word wrap box in this window', {'toggle', 'click'}, 'word wrap', ''),
    ('Type Quarterly report into the subject field', {'set_text'}, 'subject', 'quarterly report'),
    ('Click the Browse tab in Apple Music', {'click', 'select'}, 'browse', ''),
]


def _first_call(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend('http://127.0.0.1:11434')
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=30)
    response = backend.chat(model, [
        {'role': 'system', 'content': build_system_prompt() + '\n' + generate_tools_description(selected)},
        {'role': 'user', 'content': query}], timeout_sec=60,
        extra_options={'temperature': 0, 'num_predict': 1024}, tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    calls = response.get('message', {}).get('tool_calls', [])
    assert calls, f'{query}: selected {selected}, no tool call'
    args = calls[0]['function']['arguments']
    return selected, calls[0]['function']['name'], json.loads(args) if isinstance(args, str) else args


@pytest.mark.eval
@pytest.mark.parametrize('query,actions,element,value', ONE_STEP_CASES, ids=[c[0] for c in ONE_STEP_CASES])
def test_named_controls_are_acted_on_in_one_call(query, actions, element, value):
    selected, name, args = _first_call(query)
    assert 'uiControl' in selected, f'{query}: router selected {selected}'
    assert name == 'uiControl', f'{query}: called {name} {args}'
    assert args.get('action') in actions, f'{query}: {args}'
    target = f"{args.get('element', '')} {args.get('value', '')}".casefold()
    assert element in target, f'{query}: {args}'
    if value and args.get('action') in ('set_text', 'menu'):
        assert value in str(args.get('value', '')).casefold(), f'{query}: {args}'

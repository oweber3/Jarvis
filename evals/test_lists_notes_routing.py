"""Lists and notes routing: list and note requests reach listControl and noteControl with the right
arguments, and their neighbours (meal logging, weather) keep their own tools.

Runs the local model's tool selection and chat call with the full built-in catalogue. Nothing is
executed and no list or note is stored.

    EVAL_LISTS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_lists_notes_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
import json
import os

import pytest

# (utterance, tool, expected arguments, or None to check the tool only). A list value means the
# argument must hold each of those entries (case folded); a string must be contained in it.
CASES = [
    ('Add milk and eggs to my shopping list', 'listControl',
     {'action': 'add', 'list': 'shopping', 'items': ['milk', 'eggs']}),
    ('Put call the bank on my to-do list', 'listControl', {'action': 'add', 'items': ['call the bank']}),
    ("What's on my shopping list?", 'listControl', {'action': 'show', 'list': 'shopping'}),
    ('Take bread off the shopping list', 'listControl', {'action': 'remove', 'items': ['bread']}),
    ('Tick eggs off my shopping list', 'listControl', {'action': 'check', 'items': ['eggs']}),
    ('Clear my shopping list', 'listControl', {'action': 'clear', 'list': 'shopping'}),
    ('Note that the wifi password is on the router', 'noteControl', {'action': 'add', 'text': 'router'}),
    ('Take a note: dentist appointment on Tuesday at 3', 'noteControl', {'action': 'add', 'text': 'dentist'}),
    ('What notes did I take about the wifi?', 'noteControl', {'action': 'search', 'query': 'wifi'}),
    ('Read my last note', 'noteControl', {'action': 'recent'}),
    ('I had two eggs and toast for breakfast', 'logMeal', None),
    ("What's the weather like today?", 'getWeather', None),
]


@pytest.fixture(autouse=True)
def lists_and_notes_offered():
    from jarvis.tools.registry import BUILTIN_TOOLS
    original = dict(BUILTIN_TOOLS)
    if os.environ.get('EVAL_WITHOUT_LISTS'):  # set it to measure the neighbour cases without the two tools
        BUILTIN_TOOLS.pop('listControl', None)
        BUILTIN_TOOLS.pop('noteControl', None)
    yield
    BUILTIN_TOOLS.clear()
    BUILTIN_TOOLS.update(original)


def _ask(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend(os.environ.get('EVAL_OLLAMA_URL', 'http://127.0.0.1:11434'))
    model = os.environ.get('EVAL_LISTS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=30)
    response = backend.chat(model, [
        {'role': 'system', 'content': build_system_prompt() + '\n' + generate_tools_description(selected)},
        {'role': 'user', 'content': query}], timeout_sec=60,
        extra_options={'temperature': 0, 'num_predict': 1024}, tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    return selected, response.get('message', {}).get('tool_calls', [])


def _holds(actual, expected):
    if isinstance(expected, list):
        values = actual if isinstance(actual, list) else [actual]
        folded = [str(v).casefold() for v in values]
        return all(any(e.casefold() in v for v in folded) for e in expected)
    return str(expected).casefold() in str(actual or '').casefold()


@pytest.mark.eval
@pytest.mark.parametrize('query,tool,expected', CASES, ids=[c[0] for c in CASES])
def test_list_and_note_requests_reach_the_right_tool(query, tool, expected):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f'{query}: chose {name} {args}'
    for key, value in (expected or {}).items():
        assert _holds(args.get(key), value), f'{query}: {args}'

"""Routine requests reach routineControl, and look-alike requests do not.

Runs the local model's tool selection and chat call with the full catalogue (routineControl is always
registered). Nothing is executed: a routine is never run, saved or changed.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_routine_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
import json
import os

import pytest

# (utterance, tool, expected arguments, or None to check the tool only)
CASES = [
    ('Save that as movie mode', 'routineControl', {'action': 'save', 'name': 'movie mode'}),
    ('Save what you just did as a routine called bedtime', 'routineControl', {'action': 'save', 'name': 'bedtime'}),
    ('Run my tidy up routine', 'routineControl', {'action': 'run', 'name': 'tidy up'}),
    ('What routines do I have?', 'routineControl', {'action': 'list'}),
    ('Delete my movie mode routine', 'routineControl', {'action': 'delete', 'name': 'movie mode'}),
    ('Rename the movie mode routine to cinema', 'routineControl',
     {'action': 'rename', 'name': 'movie mode', 'new_name': 'cinema'}),
    ('Open Apple Music', 'appControl', None),
    ('Set the volume to 30 percent', 'systemVolume', None),
]


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
def test_routine_requests_reach_the_routine_tool(query, tool, expected):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f'{query}: chose {name} {args}'
    for key, value in (expected or {}).items():
        assert str(args.get(key, '')).casefold() == str(value).casefold(), f'{query}: {args}'

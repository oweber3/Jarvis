"""Sums and unit conversions reach the calculator with arguments that give the right answer.

Runs the local model's tool selection and chat call with the default catalogue, then runs the calculator
on the arguments the model chose (it is pure and local) and checks the number. Neighbouring requests must
keep their own tools.

    EVAL_WINDOWS_MODEL=qwen3.5:4b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_calculator_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
import json
import os
import re

import pytest

# (utterance, expected number in the calculator's result)
CALCULATIONS = [
    ('What is 17 times 23?', 391),
    ('How many cups in a litre?', 4.22675),
    ('Convert 72 Fahrenheit to Celsius', 22.2222),
    ("What's 15% of 80?", 12),
    ('What is 1234 multiplied by 5678?', 7006652),
    ('How many feet are in 3 metres?', 9.84252),
    ('What is the square root of 529?', 23),
    ('How many kilometres is 26.2 miles?', 42.1648),
]

# (utterance, tool that must be called instead of the calculator)
NEIGHBOURS = [
    ('What is the weather like today?', 'getWeather'),
    ('What time is it in Tokyo?', 'getTime'),
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
        {'role': 'user', 'content': query}], timeout_sec=90,
        extra_options={'temperature': 0, 'num_predict': 1024}, tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    return selected, response.get('message', {}).get('tool_calls', [])


def _first_call(calls):
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    return name, json.loads(args) if isinstance(args, str) else args


@pytest.mark.eval
@pytest.mark.parametrize('query,expected', CALCULATIONS, ids=[c[0] for c in CALCULATIONS])
def test_sums_and_conversions_use_the_calculator_correctly(query, expected):
    from jarvis.tools.base import ToolContext
    from jarvis.tools.builtin.calculator import CalculatorTool

    selected, calls = _ask(query)
    assert 'calculator' in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call, the model answered by itself'
    name, args = _first_call(calls)
    assert name == 'calculator', f'{query}: chose {name} {args}'
    context = ToolContext(None, None, '', query, query, 1, lambda _text: None)
    result = CalculatorTool().run(args, context)
    assert result.success, f'{query}: {args} -> {result.reply_text}'
    found = float(re.findall(r'-?\d+(?:\.\d+)?', result.reply_text.split('=')[-1])[0])
    assert found == pytest.approx(expected, rel=1e-4), f'{query}: {args} -> {result.reply_text}'


@pytest.mark.eval
@pytest.mark.parametrize('query,tool', NEIGHBOURS, ids=[n[0] for n in NEIGHBOURS])
def test_neighbouring_requests_keep_their_tools(query, tool):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls and _first_call(calls)[0] == tool, f'{query}: {calls}'

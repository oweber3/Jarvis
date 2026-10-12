"""Sums and unit conversions go through the calculator and the reply states the exact answer.

Runs the full reply engine (router, planner, tool loop, reply) against the local model with the fast path
off, so the model itself must choose the calculator. The calculator really runs (it is pure and local);
other tools are not reached. The reply must contain the right number.

    EVAL_JUDGE_MODEL=qwen3.5:4b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_calculator_routing.py -m eval -v

Set EVAL_TOOL_MODEL to a model name to run in tool-model mode. Set EVAL_WITHOUT_CALCULATOR=1 to measure the same requests with the calculator removed from the catalogue
(the model doing the arithmetic itself).
"""
import os
import re
from unittest.mock import patch

import pytest

from conftest import requires_judge_llm
from helpers import ToolCallCapture, assert_not_fallback_reply

# (utterance, numbers any one of which must appear in the reply)
CASES = [
    ('What is 17 times 23?', ['391']),
    ('How many cups in a litre?', ['4.2']),
    ('Convert 72 Fahrenheit to Celsius', ['22.2']),
    ("What's 15% of 80?", ['12']),
    ('What is 1234 multiplied by 5678?', ['7006652', '7,006,652']),
    ('How many feet are in 3 metres?', ['9.84']),
    ('What is 4.7% of 2380?', ['111.86']),
    ('How many kilometres is 26.2 miles?', ['42.16', '42.2']),
]


@pytest.fixture
def catalogue():
    from jarvis.tools.registry import BUILTIN_TOOLS
    original = dict(BUILTIN_TOOLS)
    if os.environ.get('EVAL_WITHOUT_CALCULATOR'):
        BUILTIN_TOOLS.pop('calculator', None)
    yield
    BUILTIN_TOOLS.clear()
    BUILTIN_TOOLS.update(original)


def _ask(mock_config, eval_db, eval_dialogue_memory, query):
    from helpers import JUDGE_MODEL
    from jarvis.reply.engine import run_reply_engine
    from jarvis.tools.registry import run_tool_with_retries
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = JUDGE_MODEL
    mock_config.fast_commands_enabled = False
    mock_config.location_enabled = False
    # Tool-model mode (reply.spec.md): a dedicated model chooses the tools.
    mock_config.tool_model = os.environ.get('EVAL_TOOL_MODEL', '')
    capture = ToolCallCapture()

    def run_tool(db, cfg, tool_name, tool_args, **kwargs):
        capture.record(tool_name, tool_args or {})
        if tool_name == 'calculator':
            return run_tool_with_retries(db=db, cfg=cfg, tool_name=tool_name, tool_args=tool_args, **kwargs)
        return ToolExecutionResult(success=False, reply_text='That tool is not available in this test.')

    with patch('jarvis.reply.engine.run_tool_with_retries', side_effect=run_tool):
        reply = run_reply_engine(db=eval_db, cfg=mock_config, tts=None, text=query,
                                 dialogue_memory=eval_dialogue_memory)
    return capture, reply or ''


@pytest.mark.eval
@requires_judge_llm
@pytest.mark.parametrize('query,answers', CASES, ids=[c[0] for c in CASES])
def test_reply_states_the_exact_answer_via_the_calculator(catalogue, mock_config, eval_db, eval_dialogue_memory,
                                                         query, answers):
    capture, reply = _ask(mock_config, eval_db, eval_dialogue_memory, query)
    print(f'\n  {query} -> tools {capture.tool_names() or "none"} {capture.calls}\n  reply: {reply[:300]}')
    assert_not_fallback_reply(reply, context=query)
    if not os.environ.get('EVAL_WITHOUT_CALCULATOR'):
        assert capture.has_tool('calculator'), f'{query}: calculator not used: {capture.tool_names()}'
    # A reply may spell the number out for speech; when it uses digits they must be the right ones.
    flat = re.sub(r'(?<=\d),(?=\d{3})', '', reply)
    if re.search(r'\d', flat):
        assert any(answer.replace(',', '') in flat for answer in answers), f'{query}: wrong answer: {reply}'

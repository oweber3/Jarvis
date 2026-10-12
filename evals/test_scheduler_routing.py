"""Timer, alarm and reminder requests reach scheduleControl with usable times; look-alikes do not.

Runs the local model's tool selection and chat call with the full catalogue (scheduleControl is always
registered) and the current date and time in the system prompt, as a real reply has. Nothing is executed.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_scheduler_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
from datetime import datetime, timedelta
import json
import os
import time

import pytest


def _tomorrow_at(hour, minute=0):
    day = datetime.now().date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def _next(hour, minute=0):
    """The next time this clock time comes round (a 12-hour reading may be either half of the day)."""
    now = datetime.now()
    options = []
    for h in {hour % 24, (hour + 12) % 24}:
        moment = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if moment <= now:
            moment += timedelta(days=1)
        options.append(moment.timestamp())
    return options


def _due(args):
    """When the tool would fire for these arguments, using the tool's own time handling."""
    from jarvis.tools.builtin.schedule_control import _due_at
    now = time.time()
    if args.get('at'):
        return _due_at(str(args['at']), now)
    return now + float(args.get('seconds') or 0)


# (utterance, tool, expected arguments or None, check on the arguments or None)
CASES = [
    ('Set a timer for 5 minutes', 'scheduleControl', {'action': 'set_timer'},
     lambda a: int(float(a.get('seconds', 0))) == 300),
    ('Set a 10 minute pasta timer', 'scheduleControl', {'action': 'set_timer'},
     lambda a: int(float(a.get('seconds', 0))) == 600 and 'pasta' in str(a.get('label', '')).lower()),
    ('Remind me tomorrow at 9 to email Sam', 'scheduleControl', {'action': 'set_reminder'},
     lambda a: abs(_due(a) - _tomorrow_at(9)) < 60 and 'sam' in str(a.get('label', '')).lower()),
    ('Remind me at 2:30 to call the dentist', 'scheduleControl', {'action': 'set_reminder'},
     lambda a: any(abs(_due(a) - t) < 60 for t in _next(2, 30)) and 'dentist' in str(a.get('label', '')).lower()),
    ('Wake me up at 7', 'scheduleControl', {'action': 'set_reminder'},
     lambda a: any(abs(_due(a) - t) < 60 for t in _next(7))),
    ('Remind me in 20 minutes to check the oven', 'scheduleControl', {'action': 'set_reminder'},
     lambda a: abs(_due(a) - (time.time() + 1200)) < 90),
    ('How long is left on the pasta timer?', 'scheduleControl', {'action': 'list'}, None),
    ('Cancel the pasta timer', 'scheduleControl', {'action': 'cancel'}, None),
    ('What reminders do I have?', 'scheduleControl', {'action': 'list'}, None),
    ('Set the volume to 30 percent', 'systemVolume', None, None),
    ('Run my morning briefing every weekday at 7', 'routineControl', {'action': 'schedule'}, None),
    ('Run my movie mode routine', 'routineControl', {'action': 'run'}, None),
]


def _ask(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools
    from jarvis.reply.prompts.model_variants import detect_model_size, get_system_prompts
    from jarvis.utils.time_context import format_time_context

    backend = OllamaBackend(os.environ.get('EVAL_OLLAMA_URL', 'http://127.0.0.1:11434'))
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=30)
    # The tool incentives and guidance a real reply carries for this model size, and its time line.
    prompts = get_system_prompts(detect_model_size(model))
    system = '\n\n'.join([build_system_prompt(), prompts.tool_incentives, prompts.tool_guidance,
                          generate_tools_description(selected), f'Current date and time: {format_time_context()}'])
    response = backend.chat(model, [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': query}], timeout_sec=90,
        extra_options={'temperature': 0, 'num_predict': 1024}, tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    return selected, response.get('message', {}).get('tool_calls', [])


@pytest.mark.eval
@pytest.mark.parametrize('query,tool,expected,check', CASES, ids=[c[0] for c in CASES])
def test_timer_and_reminder_requests_reach_the_scheduler(query, tool, expected, check):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f'{query}: chose {name} {args}'
    for key, value in (expected or {}).items():
        assert str(args.get(key, '')).casefold() == str(value).casefold(), f'{query}: {args}'
    if check is not None:
        try:
            ok = check(args)
        except ValueError as exc:
            ok = False
            args = {**args, 'error': str(exc)}
        assert ok, f'{query}: {args}'

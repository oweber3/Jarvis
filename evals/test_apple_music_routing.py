"""Apple Music routing: playing a named thing reaches playMusic; transport and loudness stay with their tools.

Runs the local model's tool selection and chat call with ``playMusic`` offered next to the Windows tools.
Nothing is executed, the app is never opened and Apple is never contacted.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_apple_music_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
import json
import os

import pytest

# (utterance, tool, expected arguments, or None to check the tool only). Query values are checked as
# "contains", since the model may add the artist it heard.
CASES = [
    ('Play Bohemian Rhapsody', 'playMusic', {'kind': 'song', 'query': 'bohemian rhapsody'}),
    ('Put on some Taylor Swift', 'playMusic', {'kind': 'artist', 'query': 'taylor swift'}),
    ('Play the album Rumours by Fleetwood Mac', 'playMusic', {'kind': 'album', 'query': 'rumours'}),
    ('Play my Road Trip playlist', 'playMusic', {'kind': 'playlist', 'query': 'road trip'}),
    ('Play Bohemian Rhapsody on Apple Music', 'playMusic', {'kind': 'song', 'query': 'bohemian rhapsody'}),
    ('Pause the music', 'mediaControl', {'action': 'pause'}),
    ('Next song', 'mediaControl', {'action': 'next'}),
    ("What's playing right now?", 'mediaControl', {'action': 'now_playing'}),
    ('Turn the music up', 'systemVolume', {'action': 'up'}),
    ('Open Apple Music', 'appControl', None),
]
_CONTAINS = {'query'}


@pytest.fixture(autouse=True)
def apple_music_offered():
    from jarvis.tools.builtin.apple_music import AppleMusicTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    if not os.environ.get('EVAL_WITHOUT_APPLE_MUSIC'):  # set it to measure the neighbours without the new tool
        BUILTIN_TOOLS['playMusic'] = AppleMusicTool()
    yield
    BUILTIN_TOOLS.pop('playMusic', None)


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
def test_music_requests_reach_the_right_tool(query, tool, expected):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f'{query}: chose {name} {args}'
    for key, value in (expected or {}).items():
        actual = str(args.get(key, '')).casefold()
        if key in _CONTAINS:
            assert str(value).casefold() in actual, f'{query}: {args}'
        else:
            assert actual == str(value).casefold(), f'{query}: {args}'

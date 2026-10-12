"""Document search routing: questions about what the user's documents say reach ``documentSearch``, while
finding files by name or date, the web and the PDF on screen keep their own tools.

Runs the local model's tool selection and chat call with ``documentSearch`` offered next to ``localFiles``,
``webSearch`` and ``pdfNavigate``. Nothing is executed and no document is read.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_document_search_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434. EVAL_WITHOUT_DOCUMENTS=1
leaves the tool out, to measure the neighbouring cases without it.
"""
import json
import os

import pytest

# (utterance, tool)
CASES = [
    ('What did my lease say about pets?', 'documentSearch'),
    ('Find my notes about the thermodynamics exam', 'documentSearch'),
    ('When does my car insurance renew? It should be in my documents.', 'documentSearch'),
    ('What does my tenancy agreement say about the deposit?', 'documentSearch'),
    ('Find the PDF I downloaded yesterday', 'localFiles'),
    ('List the files in my Downloads folder', 'localFiles'),
    ('Search the web for the latest news about the Mars mission', 'webSearch'),
    ('Go to page 12 in this PDF', 'pdfNavigate'),
    ("What's the weather tomorrow?", 'getWeather'),
]


@pytest.fixture(autouse=True)
def tools_offered():
    from jarvis.tools.builtin.document_search import DocumentSearchTool
    from jarvis.tools.builtin.windows import PdfNavigateTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    original = dict(BUILTIN_TOOLS)
    BUILTIN_TOOLS['pdfNavigate'] = PdfNavigateTool()
    if not os.environ.get('EVAL_WITHOUT_DOCUMENTS'):
        BUILTIN_TOOLS['documentSearch'] = DocumentSearchTool()
    yield
    BUILTIN_TOOLS.clear()
    BUILTIN_TOOLS.update(original)


def _ask(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend(os.environ.get('EVAL_OLLAMA_URL', 'http://127.0.0.1:11434'))
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=60)
    response = backend.chat(model, [
        {'role': 'system', 'content': build_system_prompt() + '\n' + generate_tools_description(selected)},
        {'role': 'user', 'content': query}], timeout_sec=120,
        extra_options={'temperature': 0, 'num_predict': 1024}, tools=generate_tools_json_schema(selected))
    assert response, 'No model response'
    return selected, response.get('message', {}).get('tool_calls', [])


@pytest.mark.eval
@pytest.mark.parametrize('query,tool', CASES, ids=[c[0] for c in CASES])
def test_document_questions_reach_document_search(query, tool):
    selected, calls = _ask(query)
    assert tool in selected, f'{query}: router selected {selected}'
    assert calls, f'{query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f'{query}: chose {name} {args}'
    if tool == 'documentSearch':
        assert str(args.get('query') or '').strip(), f'{query}: empty query {args}'

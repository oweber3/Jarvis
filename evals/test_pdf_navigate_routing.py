"""PDF page, chapter and topic requests: routing and arguments through the local model.

Each request runs twice: with ``pdfNavigate`` as its own tool (the shipped design) and with the same
actions folded into ``uiControl`` as an action group (the alternative the spec rejects). The comparison
is the evidence for the choice recorded in ``ui_automation.spec.md``. No window is touched.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_pdf_navigate_routing.py -m eval -v
"""
import json
import os

import pytest

CASES = [
    ('Go to page 42', 'goto', {'page': 42}),
    ('Jump to page twelve in this PDF', 'goto', {'page': 12}),
    ('Jump to the chapter on soil preparation', 'find', {'query': 'soil preparation'}),
    ('Find where it talks about seedlings', 'find', {'query': 'seedling'}),
    ('Show me the chapters of this PDF', 'outline', {}),
]


def _merged_tool():
    """uiControl with the PDF actions folded in, as the rejected single-tool design would have it."""
    from jarvis.tools.builtin.windows import UiControlTool

    class UiControlWithPdf(UiControlTool):
        description = ('Click buttons, type into fields, choose menu items or read text in any open app window, '
                       'and go to pages, chapters or topics of the open PDF, via UI Automation. Take a snapshot '
                       'first. NOT for opening or switching apps (use appControl).')

        @property
        def inputSchema(self):
            schema = json.loads(json.dumps(super().inputSchema))
            schema['properties']['action']['enum'] += ['pdf_goto', 'pdf_find', 'pdf_outline']
            schema['properties']['action']['description'] += (
                ' pdf_goto shows a page of the open PDF; pdf_find finds a chapter or topic in it; pdf_outline '
                'lists its chapters.')
            schema['properties']['page'] = {'type': 'integer', 'description': 'Page number for pdf_goto.'}
            schema['properties']['query'] = {'type': 'string', 'description': 'Chapter or words for pdf_find.'}
            return schema

    return UiControlWithPdf()


@pytest.fixture(params=['separate_tool', 'ui_action_group'])
def design(request):
    from jarvis.tools.registry import BUILTIN_TOOLS
    from jarvis.tools.builtin.windows import PdfNavigateTool, UiControlTool
    original = dict(BUILTIN_TOOLS)
    if request.param == 'separate_tool':
        BUILTIN_TOOLS['uiControl'] = UiControlTool()
        BUILTIN_TOOLS['pdfNavigate'] = PdfNavigateTool()
    else:
        BUILTIN_TOOLS.pop('pdfNavigate', None)
        BUILTIN_TOOLS['uiControl'] = _merged_tool()
    yield request.param
    BUILTIN_TOOLS.clear()
    BUILTIN_TOOLS.update(original)


def _ask(query):
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
    return selected, response.get('message', {}).get('tool_calls', [])


@pytest.mark.eval
@pytest.mark.parametrize('query,action,expected', CASES, ids=[c[0] for c in CASES])
def test_pdf_requests_reach_the_pdf_actions(design, query, action, expected):
    tool, wanted_action = (('pdfNavigate', action) if design == 'separate_tool' else ('uiControl', f'pdf_{action}'))
    selected, calls = _ask(query)
    assert tool in selected, f'{design}: {query}: router selected {selected}'
    assert calls, f'{design}: {query}: no tool call'
    name = calls[0]['function']['name']
    args = calls[0]['function']['arguments']
    args = json.loads(args) if isinstance(args, str) else args
    assert (name, args.get('action')) == (tool, wanted_action), f'{design}: {query}: {name} {args}'
    if 'page' in expected:
        assert str(args.get('page')) == str(expected['page']), f'{design}: {query}: {args}'
    if 'query' in expected:
        assert expected['query'] in str(args.get('query', '')).casefold(), f'{design}: {query}: {args}'


# The router is told which application is in front (the process name only), so a request that says
# "it" instead of naming the PDF still routes to pdfNavigate while a PDF viewer is in front, and to
# webSearch otherwise. Run on Windows with the shipped design:
#     EVAL_WINDOWS_MODEL=qwen3.5:0.8b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_pdf_navigate_routing.py -k foreground -m eval -v
FOREGROUND_CASES = [
    ('Find where it talks about seedlings', 'pdfeditor', 'pdfNavigate'),
    ('Find where it mentions the boundary layer', 'pdfeditor', 'pdfNavigate'),
    ('Find where it talks about seedlings', '', None),  # baseline without the hint, reported only
]


@pytest.mark.eval
@pytest.mark.parametrize('query,foreground,expected', FOREGROUND_CASES,
                         ids=[f'{c[0]}|{c[1] or "no hint"}' for c in FOREGROUND_CASES])
def test_the_foreground_app_steers_ambiguous_pdf_requests(query, foreground, expected):
    from types import SimpleNamespace
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.reply.engine import _build_enrichment_context_hint
    from jarvis.tools.builtin.windows import PdfNavigateTool, UiControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend('http://127.0.0.1:11434')
    model = os.environ.get('EVAL_WINDOWS_MODEL', 'qwen3.5:0.8b')
    assert model in backend.list_models(), 'Install or select a local eval model'
    tools = {**BUILTIN_TOOLS, 'uiControl': UiControlTool(), 'pdfNavigate': PdfNavigateTool()}
    hint = _build_enrichment_context_hint(SimpleNamespace(location_enabled=False), [], '', foreground=foreground)
    selected = select_tools(query, tools, {}, ToolSelectionStrategy.LLM, llm_backend=backend, llm_model=model,
                            llm_timeout_sec=30, context_hint=hint)
    print(f'{query!r} foreground={foreground or "-"}: {selected}')
    if expected:
        assert expected in selected, f'{query}: router selected {selected}'

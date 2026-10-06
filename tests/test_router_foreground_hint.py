"""The tool router is told which application the user is working in, so "find where it talks about
seedlings" with a PDF viewer in front can route to pdfNavigate rather than webSearch.

Only the process name reaches the router, never the window title, and a phone request has no
foreground at all. Routing quality itself is measured by evals/test_pdf_navigate_routing.py against a
live model."""
import sys
from types import SimpleNamespace

import pytest

from jarvis.reply import engine
from jarvis.tools.selection import ToolSelectionStrategy, select_tools


def _cfg(**over):
    base = dict(location_enabled=False, windows_tools_enabled=True)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def on_windows(monkeypatch):
    """Pretend to be on Windows with the user working in PDFgear."""
    from jarvis.platform.windows import ui_automation
    state = {'process': 'pdfeditor', 'calls': 0}

    def foreground_target():
        state['calls'] += 1
        if isinstance(state['process'], Exception):
            raise state['process']
        return {'hwnd': 77, 'process': state['process'], 'application': 'PDFgear',
                'monitor': r'\\.\DISPLAY1', 'state': 'maximised'}

    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(ui_automation, 'foreground_target', foreground_target)
    return state


def _process(cfg, origin='voice'):
    window = engine._foreground_window(cfg, origin)
    return window.process if window else ''


class _CapturingBackend:
    def __init__(self):
        self.prompts = []

    def direct(self, model, system_prompt, user_prompt, **kwargs):
        self.prompts.append((system_prompt, user_prompt))
        return 'pdfNavigate'


class _Tool:
    def __init__(self, description):
        self.description = description


def _route(context_hint):
    backend = _CapturingBackend()
    tools = {'pdfNavigate': _Tool('Go to a page or topic in the open PDF'),
             'webSearch': _Tool('Search the web'), 'stop': _Tool('stop'), 'toolSearchTool': _Tool('search')}
    select_tools(query='find where it talks about seedlings', builtin_tools=tools, mcp_tools={},
                 strategy=ToolSelectionStrategy.LLM, llm_backend=backend, llm_model='m', llm_timeout_sec=1.0,
                 context_hint=context_hint)
    return backend.prompts[0][1]


def test_the_router_sees_the_foreground_process_among_its_known_facts(on_windows):
    process = _process(_cfg())
    hint = engine._build_enrichment_context_hint(_cfg(), [], '', foreground=process)
    prompt = _route(hint)
    facts = prompt.split('KNOWN FACTS', 1)[1].split('User query:', 1)[0]
    assert on_windows['process'] in facts
    assert prompt.rstrip().endswith("'none'):")  # the query stays last, after the hint


def test_the_foreground_rides_with_recent_dialogue_without_joining_it(on_windows):
    msgs = [{'role': 'user', 'content': 'open my garden notes'}]
    hint = engine._build_enrichment_context_hint(_cfg(), msgs, '', foreground=_process(_cfg()))
    prompt = _route(hint)
    facts, dialogue = prompt.split('RECENT DIALOGUE', 1)
    assert on_windows['process'] in facts and on_windows['process'] not in dialogue


def test_no_foreground_line_without_a_process():
    hint = engine._build_enrichment_context_hint(_cfg(), [], '', foreground='')
    assert 'oreground' not in hint


def test_the_whole_foreground_window_is_read_once_without_its_title(on_windows):
    window = engine._foreground_window(_cfg(), 'chat')
    assert (window.hwnd, window.process, window.application, window.state) == (77, 'pdfeditor', 'PDFgear',
                                                                               'maximised')
    assert not hasattr(window, 'title') and on_windows['calls'] == 1


def test_a_phone_request_reads_no_foreground(on_windows):
    assert engine._foreground_window(_cfg(), 'phone') is None
    assert on_windows['calls'] == 0


@pytest.mark.parametrize('cfg_over,platform', [({'windows_tools_enabled': False}, 'win32'), ({}, 'linux')])
def test_no_lookup_off_windows_or_with_windows_tools_disabled(on_windows, monkeypatch, cfg_over, platform):
    monkeypatch.setattr(sys, 'platform', platform)
    assert engine._foreground_window(_cfg(**cfg_over), 'voice') is None
    assert on_windows['calls'] == 0


def test_an_unreadable_foreground_leaves_the_hint_out(on_windows):
    on_windows['process'] = OSError('access denied')
    assert engine._foreground_window(_cfg(), 'voice') is None


def test_router_cache_keys_differ_by_foreground():
    a = engine._router_cache_key('find seedlings', ToolSelectionStrategy.LLM, {}, 'pdfeditor')
    b = engine._router_cache_key('find seedlings', ToolSelectionStrategy.LLM, {}, 'chrome')
    assert a != b
    assert a == engine._router_cache_key('find seedlings', ToolSelectionStrategy.LLM, {}, 'pdfeditor')

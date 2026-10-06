"""'Go to page {number}' is a fast route to pdfNavigate, offered only while the user is in a PDF viewer."""
import json
from pathlib import Path

import pytest

from jarvis.tools.types import ToolExecutionResult

TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'openPath', 'pdfNavigate'}


def route(text, tools=TOOLS):
    from jarvis.fastpath.matcher import match
    return match(text, 'en', available_tools=tools)


def locale():
    return json.loads((Path(__file__).resolve().parents[1] / 'src/jarvis/fastpath/phrases/en.json')
                      .read_text(encoding='utf-8'))


@pytest.mark.parametrize('text,page', [
    ('Go to page 42', 42), ('page 7', 7), ('Page 7.', 7), ('jump to page 120', 120),
    ('turn to page 1234', 1234), ('go to page one', 1), ('Please go to page ten', 10),
    ('skip to page 3', 3),
])
def test_page_phrases_go_to_that_page_of_the_open_pdf(text, page):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('pdfNavigate', {'action': 'goto', 'page': page})


@pytest.mark.parametrize('text', [
    'page 0', 'go to page', 'go to page 42 and zoom in', 'go to page forty two point five',
    'go to page -3', 'go to page 123456', 'what is on page 4', 'page 4 of the manual',
])
def test_uncertain_page_requests_fall_through(text):
    assert route(text) is None


def test_without_the_tool_no_page_phrase_matches():
    assert route('go to page 42', tools=TOOLS - {'pdfNavigate'}) is None


def test_desktop_numbers_keep_their_meaning():
    assert route('go to desktop 2').args == {'action': 'desktop_switch', 'target': '', 'desktop': '2'}


def test_number_words_come_from_the_locale_table():
    word, value = next(iter(locale()['numbers'].items()))
    assert route(f'go to page {word}').args['page'] == value


# --- dispatcher ------------------------------------------------------------------------------------

class _FakeViewer:
    """Stands in for the window the user is working in."""

    def __init__(self):
        self.process, self.title = 'pdfeditor', 'Garden.pdf - PDFgear'

    def window(self):
        return {'hwnd': 101, 'process': self.process, 'title': self.title, 'top_hwnd': 101}


@pytest.fixture
def pdf_tool(mock_config, monkeypatch):
    from jarvis.platform.windows import ui_automation
    from jarvis.tools.builtin.windows.pdf_navigate import PdfNavigateTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    viewer = _FakeViewer()
    monkeypatch.setattr(ui_automation, 'resolve_hwnd', lambda window='': 101)
    monkeypatch.setattr(ui_automation, 'window_info', lambda hwnd: viewer.window())
    mock_config.windows_tools_enabled = True
    BUILTIN_TOOLS['pdfNavigate'] = PdfNavigateTool()
    yield viewer
    BUILTIN_TOOLS.pop('pdfNavigate', None)


def test_dispatcher_offers_the_page_route_while_a_pdf_viewer_is_in_front(pdf_tool, mock_config):
    from jarvis.fastpath.dispatcher import match_command
    assert match_command('go to page 42', mock_config).args == {'action': 'goto', 'page': 42}


def test_an_edge_window_showing_a_pdf_counts_as_a_viewer(pdf_tool, mock_config):
    from jarvis.fastpath.dispatcher import match_command
    pdf_tool.process, pdf_tool.title = 'msedge', 'Garden.pdf - Personal - Microsoft Edge'
    assert match_command('page 5', mock_config) is not None


@pytest.mark.parametrize('process,title', [
    ('WINWORD', 'Report.docx - Word'),
    ('msedge', 'BBC News - Personal - Microsoft Edge'),
])
def test_in_any_other_window_the_request_goes_to_the_model(pdf_tool, mock_config, process, title):
    from jarvis.fastpath.dispatcher import match_command
    pdf_tool.process, pdf_tool.title = process, title
    assert match_command('go to page 42', mock_config) is None


def test_no_route_when_the_foreground_cannot_be_read(pdf_tool, mock_config, monkeypatch):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.platform.windows import ui_automation

    def no_window(window=''):
        raise ValueError('No application window is open.')

    monkeypatch.setattr(ui_automation, 'resolve_hwnd', no_window)
    assert match_command('go to page 42', mock_config) is None


def test_no_route_when_windows_tools_are_disabled(pdf_tool, mock_config):
    from jarvis.fastpath.dispatcher import match_command
    mock_config.windows_tools_enabled = False
    assert match_command('go to page 42', mock_config) is None


def test_dispatch_runs_once_and_confirms_the_page(pdf_tool, mock_config):
    from jarvis.fastpath.dispatcher import dispatch, match_command
    calls = []

    def executor(**kwargs):
        calls.append((kwargs['tool_name'], kwargs['tool_args']))
        return ToolExecutionResult(success=True, reply_text='{"file": "Garden.pdf", "page": 42}')

    result = match_command('go to page 42', mock_config)
    reply = dispatch(result, None, mock_config, 'go to page 42', executor=executor, quiet=True)
    assert calls == [('pdfNavigate', {'action': 'goto', 'page': 42})]
    assert '42' in reply and '{' not in reply


def test_a_failure_is_reported_verbatim(pdf_tool, mock_config):
    from jarvis.fastpath.dispatcher import dispatch, match_command

    def executor(**kwargs):
        return ToolExecutionResult(success=False, reply_text=None, error_message='The document has pages 1 to 30.')

    result = match_command('go to page 42', mock_config)
    assert dispatch(result, None, mock_config, 'go to page 42', executor=executor, quiet=True) == \
        'The document has pages 1 to 30.'


def test_a_chrome_window_showing_a_local_pdf_counts_as_a_viewer(pdf_tool, mock_config, monkeypatch, tmp_path):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.platform.windows import ui_automation
    book = tmp_path / 'Garden.pdf'
    book.write_bytes(b'%PDF-1.4')
    pdf_tool.process, pdf_tool.title = 'chrome', 'Gardening Handbook - Google Chrome'
    monkeypatch.setattr(ui_automation, 'address_bar_values', lambda hwnd: [book.as_posix()])
    assert match_command('page 5', mock_config) is not None
    monkeypatch.setattr(ui_automation, 'address_bar_values', lambda hwnd: ['https://www.bbc.co.uk/news'])
    assert match_command('page 5', mock_config) is None

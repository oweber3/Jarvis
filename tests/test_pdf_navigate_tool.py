"""pdfNavigate adapter over a real generated PDF, with the viewer and Edge faked."""
import json
from dataclasses import replace

import pytest

from jarvis.platform.windows import pdf_viewer as pv
from pdf_fixtures import make_pdf

PAGES = ['Cover', 'Chapter 1 Basics\nUnits and dimensions.', 'Chapter 2 Soil Preparation\nDigging basics.',
         'Mulch and soil preparation timings.', 'Chapter 3 Pots\nSeedlings of tomato plants.',
         'Seedlings again in spring.', 'References']
OUTLINE = [('Chapter 1 Basics', 1), ('Chapter 2 Soil Preparation', 2), ('Chapter 3 Pots', 4), ('References', 6)]


@pytest.fixture
def book(tmp_path):
    from jarvis.utils import pdf_document
    pdf_document.clear_cache()
    yield make_pdf(tmp_path / 'Garden.pdf', PAGES, OUTLINE)
    pdf_document.clear_cache()


@pytest.fixture
def cfg():
    from jarvis.config import load_settings
    return load_settings()


class FakeViewer:
    def __init__(self, monkeypatch, book, *, title=None, document=None):
        self.shown = []
        self.viewer = pv.Viewer('pdfgear', 99, 'pdfeditor', title or f'{book.name} - PDFgear')
        monkeypatch.setattr(pv, 'find_viewer', lambda window='': self.viewer)
        monkeypatch.setattr(pv, 'document_for', document or (lambda viewer, **_kw: book))

        def show(viewer, path, page, count, launcher=None):
            self.shown.append((viewer.kind if viewer else None, path.name, page, count))
            return {'page': page, 'method': 'page_box', 'viewer': 'pdfgear'}
        monkeypatch.setattr(pv, 'show_page', show)


def _run(cfg, args):
    from jarvis.tools.builtin.windows import PdfNavigateTool
    return PdfNavigateTool().execute(None, cfg, args, '', '', '', 1, lambda _m: None)


def test_goto_shows_the_page_in_the_open_viewer(cfg, monkeypatch, book):
    fake = FakeViewer(monkeypatch, book)
    result = _run(cfg, {'action': 'goto', 'page': 5})
    assert result.success, result.error_message
    assert fake.shown == [('pdfgear', 'Garden.pdf', 5, len(PAGES))]
    data = json.loads(result.reply_text)
    assert (data['page'], data['page_count'], data['file']) == (5, len(PAGES), 'Garden.pdf')


def test_goto_accepts_numeric_strings_and_rejects_pages_outside_the_document(cfg, monkeypatch, book):
    fake = FakeViewer(monkeypatch, book)
    assert _run(cfg, {'action': 'goto', 'page': '3'}).success
    for page in (0, 99, 'twelve', None):
        assert not _run(cfg, {'action': 'goto', 'page': page}).success
    assert [shown[2] for shown in fake.shown] == [3]


def test_find_jumps_straight_to_a_single_matching_chapter(cfg, monkeypatch, book):
    fake = FakeViewer(monkeypatch, book)
    result = _run(cfg, {'action': 'find', 'query': 'soil preparation'})
    data = json.loads(result.reply_text)
    assert fake.shown and fake.shown[0][2] == 3
    assert data['page'] == 3 and data['matched'] == 'outline' and data['title'] == 'Chapter 2 Soil Preparation'


def test_find_returns_candidates_with_snippets_when_vague(cfg, monkeypatch, book):
    fake = FakeViewer(monkeypatch, book)
    result = _run(cfg, {'action': 'find', 'query': 'seedlings'})
    data = json.loads(result.reply_text)
    assert result.success and fake.shown == []
    pages = [candidate['page'] for candidate in data['candidates']]
    assert set(pages) == {5, 6}
    assert all(candidate.get('snippet') for candidate in data['candidates'])
    assert data['pages_searched'] == data['page_count'] == len(PAGES)


def test_outline_lists_chapters(cfg, monkeypatch, book):
    FakeViewer(monkeypatch, book)
    data = json.loads(_run(cfg, {'action': 'outline'}).reply_text)
    assert [(e['title'], e['page']) for e in data['outline']][:2] == [('Chapter 1 Basics', 2),
                                                                      ('Chapter 2 Soil Preparation', 3)]


def test_ambiguous_documents_return_candidates_and_ask(cfg, monkeypatch, book, tmp_path):
    def ambiguous(viewer, **_kw):
        raise pv.AmbiguousDocumentError([str(tmp_path / 'a' / 'Garden.pdf'), str(tmp_path / 'b' / 'Garden.pdf')])
    fake = FakeViewer(monkeypatch, book, document=ambiguous)
    result = _run(cfg, {'action': 'goto', 'page': 2})
    assert not result.success and fake.shown == []
    assert len(json.loads(result.reply_text)['candidates']) == 2


def test_a_given_file_is_used_even_without_a_viewer(cfg, monkeypatch, book):
    fake = FakeViewer(monkeypatch, book)
    monkeypatch.setattr(pv, 'find_viewer', lambda window='': None)
    result = _run(cfg, {'action': 'goto', 'page': 2, 'file': str(book)})
    assert result.success, result.error_message
    assert fake.shown == [(None, 'Garden.pdf', 2, len(PAGES))]


def test_no_viewer_and_no_file_is_an_honest_failure(cfg, monkeypatch, book):
    FakeViewer(monkeypatch, book)
    monkeypatch.setattr(pv, 'find_viewer', lambda window='': None)
    result = _run(cfg, {'action': 'goto', 'page': 2})
    assert not result.success and 'PDF' in result.error_message


def test_disabled_and_invalid_requests_touch_nothing(cfg, monkeypatch, book):
    fake = FakeViewer(monkeypatch, book)
    assert not _run(replace(cfg, windows_tools_enabled=False), {'action': 'goto', 'page': 2}).success
    assert not _run(cfg, {'action': 'print'}).success
    assert not _run(cfg, {'action': 'find', 'query': ' '}).success
    assert not _run(cfg, {'action': 'goto', 'page': 2, 'file': str(book.with_suffix('.txt'))}).success
    assert fake.shown == []


@pytest.mark.parametrize('remote', [
    r'\\files.example\share\Garden.pdf', '//files.example/share/Garden.pdf', r'\\?\UNC\files.example\s\Garden.pdf',
])
def test_a_network_share_path_is_refused_without_touching_it(cfg, monkeypatch, book, remote):
    """Probing a UNC path makes Windows connect to that host with the user's credentials."""
    from pathlib import Path
    fake = FakeViewer(monkeypatch, book)
    probed = []
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda self: probed.append(str(self)) or real_is_file(self))
    result = _run(cfg, {'action': 'outline', 'file': remote})
    assert not result.success and fake.shown == []
    assert not any('example' in p for p in probed)


def test_the_file_check_runs_within_the_time_limit(cfg, monkeypatch, book):
    """The existence check is an OS call, so it runs on the bounded worker, not the listener thread."""
    import threading
    from pathlib import Path
    fake = FakeViewer(monkeypatch, book)
    monkeypatch.setattr(pv, 'find_viewer', lambda window='': None)
    threads = []
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file',
                        lambda self: threads.append(threading.current_thread().name) or real_is_file(self))
    assert _run(cfg, {'action': 'goto', 'page': 2, 'file': str(book)}).success
    assert threads and threading.main_thread().name not in threads

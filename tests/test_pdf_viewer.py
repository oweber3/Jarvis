"""Which PDF a viewer shows, and how a page is shown, without touching real windows."""
import sys
from pathlib import Path

import pytest

from jarvis.platform.windows import pdf_viewer as pv
from jarvis.platform.windows.ui_automation import PageBoxCandidate, choose_page_box


@pytest.mark.parametrize('title,expected', [
    ('Soil Notes.pdf - PDFgear', ['Soil Notes.pdf']),
    ('PDFgear - chapter 3.PDF', ['PDFgear - chapter 3.PDF', 'chapter 3.PDF']),
    ('Smith - Garden.pdf - PDFgear', ['Smith - Garden.pdf', 'Garden.pdf']),
    ('Gardening - PDFgear', ['Gardening.pdf']),
    ('', []),
])
def test_file_names_come_from_the_viewer_title(title, expected):
    assert pv.title_file_names(title) == expected


@pytest.mark.parametrize('process,kind', [
    ('pdfeditor', 'pdfgear'), ('PDFLauncher', 'pdfgear'), ('msedge', 'edge'), ('notepad', None)])
def test_viewer_kinds_are_known_by_process(process, kind):
    assert pv.viewer_kind(process) == kind


def test_a_title_name_found_in_one_known_folder_is_the_document(tmp_path):
    docs = tmp_path / 'Documents' / 'Uni'
    docs.mkdir(parents=True)
    book = docs / 'Garden.pdf'
    book.write_bytes(b'%PDF-1.4')
    found = pv.find_files(['garden.pdf'], recent_dir=tmp_path / 'Recent', folders=[tmp_path / 'Documents'])
    assert [Path(p) for p in found] == [book]


def test_the_same_name_in_two_places_is_ambiguous_and_never_guessed(tmp_path):
    for folder in ('Downloads', 'Desktop'):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / 'Garden.pdf').write_bytes(b'%PDF-1.4')
    viewer = pv.Viewer('pdfgear', 1, 'pdfeditor', 'Garden.pdf - PDFgear')
    with pytest.raises(pv.AmbiguousDocumentError) as caught:
        pv.document_for(viewer, recent_dir=tmp_path / 'Recent',
                        folders=[tmp_path / 'Downloads', tmp_path / 'Desktop'])
    assert sorted(Path(p).parent.name for p in caught.value.candidates) == ['Desktop', 'Downloads']


def test_an_unknown_title_asks_for_the_path(tmp_path):
    viewer = pv.Viewer('pdfgear', 1, 'pdfeditor', 'Missing.pdf - PDFgear')
    with pytest.raises(ValueError, match='path'):
        pv.document_for(viewer, recent_dir=tmp_path, folders=[tmp_path])


def _write_shortcut(link: Path, target: Path) -> None:
    """Create a Windows shell link the way Explorer records a Recent item."""
    import threading

    def write():
        import comtypes
        import comtypes.client
        from comtypes.persist import IPersistFile
        from comtypes.shelllink import ShellLink, IShellLinkW
        comtypes.CoInitialize()
        try:
            shortcut = comtypes.client.CreateObject(ShellLink, interface=IShellLinkW)
            shortcut.SetPath(str(target))
            shortcut.QueryInterface(IPersistFile).Save(str(link), True)
            del shortcut
        finally:
            comtypes.CoUninitialize()

    worker = threading.Thread(target=write)
    worker.start()
    worker.join()
    assert link.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows shell links')
def test_recent_items_shortcuts_point_to_the_document(tmp_path):
    elsewhere = tmp_path / 'Somewhere' / 'Deep'
    elsewhere.mkdir(parents=True)
    book = elsewhere / 'Handbook.pdf'
    book.write_bytes(b'%PDF-1.4')
    recent = tmp_path / 'Recent'
    recent.mkdir()
    _write_shortcut(recent / 'Handbook.pdf.lnk', book)
    assert [Path(p) for p in pv.find_files(['Handbook.pdf'], recent_dir=recent, folders=[])] == [book]


def test_edge_address_bars_give_the_exact_file(tmp_path):
    book = tmp_path / 'My Book.pdf'
    book.write_bytes(b'%PDF-1.4')
    url = pv.edge_page_url(book, 12)
    assert url.endswith('#page=12') and '%20' in url
    assert Path(pv.path_from_address(url)) == book
    assert pv.path_from_address('https://example.com/a.pdf') is None
    assert Path(pv.path_from_address(str(book))) == book


def test_page_boxes_are_chosen_by_page_range_and_automation_id():
    boxes = [PageBoxCandidate('ZoomBox', 100.0), PageBoxCandidate('', 3.0), PageBoxCandidate('Search', None)]
    assert choose_page_box(boxes, 40) == 1
    assert choose_page_box([PageBoxCandidate('A', 2.0), PageBoxCandidate('B', 5.0)], 40) is None
    assert choose_page_box([PageBoxCandidate('A', 2.0), PageBoxCandidate('PageNumberBox', 5.0)], 40) == 1
    assert choose_page_box([PageBoxCandidate('', 2.5)], 40) is None


def test_show_page_uses_the_viewer_page_box_first(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pv, 'set_viewer_page', lambda hwnd, page, count: calls.append((hwnd, page, count))
                        or {'page': page, 'method': 'page_box'})
    viewer = pv.Viewer('pdfgear', 77, 'pdfeditor', 'a.pdf - PDFgear')
    result = pv.show_page(viewer, tmp_path / 'a.pdf', 5, 40, launcher=lambda argv: pytest.fail('Edge opened'))
    assert calls == [(77, 5, 40)] and result['method'] == 'page_box'


def test_without_a_usable_page_box_the_file_opens_in_edge_at_the_page(monkeypatch, tmp_path):
    def no_box(hwnd, page, count):
        raise LookupError('No page number box was found in the viewer.')
    monkeypatch.setattr(pv, 'set_viewer_page', no_box)
    monkeypatch.setattr(pv, 'edge_executable', lambda: r'C:\Edge\msedge.exe')
    launched = []
    book = tmp_path / 'a.pdf'
    result = pv.show_page(pv.Viewer('pdfgear', 77, 'pdfeditor', 'a.pdf'), book, 9, 40, launcher=launched.append)
    assert launched == [[r'C:\Edge\msedge.exe', pv.edge_page_url(book, 9)]]
    assert result['method'] == 'opened_in_edge' and result['page'] == 9


def test_edge_viewers_and_no_viewer_go_straight_to_edge(monkeypatch, tmp_path):
    monkeypatch.setattr(pv, 'set_viewer_page', lambda *a: pytest.fail('page box used for Edge'))
    monkeypatch.setattr(pv, 'edge_executable', lambda: 'msedge.exe')
    launched = []
    pv.show_page(pv.Viewer('edge', 5, 'msedge', 'a.pdf'), tmp_path / 'a.pdf', 3, 10, launcher=launched.append)
    pv.show_page(None, tmp_path / 'a.pdf', 4, 10, launcher=launched.append)
    assert [argv[1].rsplit('#', 1)[1] for argv in launched] == ['page=3', 'page=4']


# --- Chrome ----------------------------------------------------------------------------

class _Desk:
    """Windows in z-order, with the address bar each browser window shows (``None`` = no browser)."""

    def __init__(self, monkeypatch, windows, foreground=None):
        from jarvis.platform.windows import ui_automation, windows_mgmt
        from jarvis.platform.windows.windows_mgmt import Window
        self.windows = [Window(hwnd, title, process, 1000 + hwnd) for hwnd, process, title, _ in windows]
        self.addresses = {hwnd: address for hwnd, _, _, address in windows}
        self.reads = []
        by_hwnd = {w.hwnd: w for w in self.windows}

        def resolve(window=''):
            if window:
                return int(window)
            if foreground is None:
                raise ValueError('No application window is open.')
            return foreground

        monkeypatch.setattr(ui_automation, 'resolve_hwnd', resolve)
        monkeypatch.setattr(ui_automation, 'window_info', lambda hwnd: {
            'hwnd': hwnd, 'process': by_hwnd[hwnd].process, 'title': by_hwnd[hwnd].title, 'top_hwnd': hwnd})
        monkeypatch.setattr(ui_automation, 'address_bar_values', self.address_bar)
        monkeypatch.setattr(windows_mgmt, 'list_windows', lambda: list(self.windows))

    def address_bar(self, hwnd):
        self.reads.append(hwnd)
        address = self.addresses.get(hwnd)
        return [address] if address else []


@pytest.fixture
def book(tmp_path):
    path = tmp_path / 'Garden Handbook.pdf'
    path.write_bytes(b'%PDF-1.4')
    return path


def test_chrome_is_a_viewer_kind():
    assert pv.viewer_kind('chrome') == 'chrome'


def test_chrome_showing_a_local_pdf_is_the_viewer_whatever_its_title(monkeypatch, book):
    # Chrome titles a PDF by the document's own title, not its file name.
    _Desk(monkeypatch, [(10, 'chrome', 'Gardening Basics - Google Chrome', book.as_posix())],
          foreground=10)
    viewer = pv.find_viewer()
    assert (viewer.kind, viewer.hwnd) == ('chrome', 10)
    assert pv.document_for(viewer) == book
    assert pv.foreground_viewer().kind == 'chrome'


def test_chrome_on_a_web_page_is_not_a_viewer_and_the_pdf_window_behind_it_is_found(monkeypatch, book):
    desk = _Desk(monkeypatch, [(10, 'chrome', 'BBC News - Google Chrome', 'https://www.bbc.co.uk/news'),
                               (11, 'chrome', 'Handbook - Google Chrome', book.as_uri())], foreground=10)
    assert pv.foreground_viewer() is None
    viewer = pv.find_viewer()
    assert viewer.hwnd == 11 and pv.document_for(viewer) == book
    assert set(desk.reads) <= {10, 11}


def test_a_named_chrome_window_without_a_pdf_is_refused(monkeypatch):
    _Desk(monkeypatch, [(10, 'chrome', 'BBC News - Google Chrome', 'https://www.bbc.co.uk/news')], foreground=10)
    with pytest.raises(ValueError):
        pv.find_viewer('10')


def test_only_a_few_chrome_windows_are_read_when_searching(monkeypatch):
    desk = _Desk(monkeypatch, [(hwnd, 'chrome', f'Page {hwnd} - Google Chrome', 'https://example.com')
                               for hwnd in range(1, 11)])
    assert pv.find_viewer() is None
    assert len(desk.reads) <= 4


def test_chrome_shows_a_page_through_its_page_box(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pv, 'set_viewer_page', lambda hwnd, page, count: calls.append((hwnd, page))
                        or {'page': page, 'method': 'page_box'})
    viewer = pv.Viewer('chrome', 10, 'chrome', 'Handbook - Google Chrome')
    result = pv.show_page(viewer, tmp_path / 'a.pdf', 42, 300, launcher=lambda argv: pytest.fail('opened'))
    assert calls == [(10, 42)] and result['method'] == 'page_box' and result['viewer'] == 'chrome'


def test_chrome_without_a_usable_page_box_opens_the_page_in_chrome_not_edge(monkeypatch, tmp_path):
    def no_box(hwnd, page, count):
        raise LookupError('No page number box was found in the viewer.')
    monkeypatch.setattr(pv, 'set_viewer_page', no_box)
    monkeypatch.setattr(pv, 'chrome_executable', lambda: r'C:\Chrome\chrome.exe')
    monkeypatch.setattr(pv, 'edge_executable', lambda: pytest.fail('Edge opened for a Chrome viewer'))
    launched = []
    book = tmp_path / 'a.pdf'
    result = pv.show_page(pv.Viewer('chrome', 10, 'chrome', 'x'), book, 9, 40, launcher=launched.append)
    assert launched == [[r'C:\Chrome\chrome.exe', pv.edge_page_url(book, 9)]]
    assert result['method'] == 'opened_in_chrome' and result['page'] == 9

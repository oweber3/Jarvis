"""PDF text and bookmarks through PDFium, shared by PDF navigation and document search."""
import threading

import pytest

from pdf_fixtures import make_pdf


def test_bookmarks_keep_their_level_and_page(tmp_path):
    from jarvis.utils.pdf_text import PdfText
    path = make_pdf(tmp_path / 'book.pdf', ['Cover', 'One', 'Two'],
                    [('Chapter 1', 1, [('Problems', 2)]), ('Index', 0)])
    with PdfText(path) as pdf:
        assert pdf.page_count == 3
        assert [(b.title, b.level, b.page_index) for b in pdf.bookmarks()] == [
            ('Chapter 1', 0, 1), ('Problems', 1, 2), ('Index', 0, 0)]


def test_page_text_is_read(tmp_path):
    from jarvis.utils.pdf_text import PdfText
    path = make_pdf(tmp_path / 'book.pdf', ['Bernoulli equation', 'Problem 4.13 a pipe'])
    with PdfText(path) as pdf:
        assert 'Bernoulli' in pdf.page_text(0)
        assert '4.13' in pdf.page_text(1)


def test_documents_can_be_read_from_several_threads_at_once(tmp_path):
    from jarvis.utils.pdf_text import PdfText
    paths = [make_pdf(tmp_path / f'doc{n}.pdf', [f'document {n} page {p}' for p in range(30)]) for n in range(4)]
    errors = []

    def read(n, path):
        try:
            with PdfText(path) as pdf:
                for p in range(pdf.page_count):
                    assert f'document {n} page {p}' in pdf.page_text(p)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=read, args=(n, path)) for n, path in enumerate(paths)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_damaged_and_password_protected_files_fail_clearly(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from jarvis.utils.pdf_text import PdfText, PdfTextError
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(b'%PDF-1.4 garbage')
    plain = make_pdf(tmp_path / 'plain.pdf', ['secret'])
    writer = PdfWriter(clone_from=PdfReader(plain))
    writer.encrypt('hunter2')
    locked = tmp_path / 'locked.pdf'
    writer.write(locked)
    for path in (broken, locked, tmp_path / 'missing.pdf'):
        with pytest.raises(PdfTextError):
            PdfText(path)


def test_a_password_protected_file_says_so(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from jarvis.utils.pdf_text import PdfText, PdfTextError
    plain = make_pdf(tmp_path / 'plain.pdf', ['open to read'])
    locked, owner_only = tmp_path / 'locked.pdf', tmp_path / 'owner.pdf'
    for path, user in ((locked, 'hunter2'), (owner_only, '')):
        writer = PdfWriter(clone_from=PdfReader(plain))
        writer.encrypt(user_password=user, owner_password='owner')
        writer.write(path)
    with pytest.raises(PdfTextError) as caught:
        PdfText(locked)
    assert caught.value.needs_password
    with pytest.raises(PdfTextError) as caught:
        PdfText(tmp_path / 'missing.pdf')
    assert not caught.value.needs_password
    with PdfText(owner_only) as pdf:  # an empty user password opens without asking
        assert 'open to read' in pdf.page_text(0)


def test_a_bookmark_into_another_file_has_no_page_here(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
    from jarvis.utils.pdf_text import PdfText
    base = make_pdf(tmp_path / 'base.pdf', ['One', 'Two', 'Three'], [('Local', 1)])
    writer = PdfWriter(clone_from=PdfReader(base))
    remote = DictionaryObject({
        NameObject('/S'): NameObject('/GoToR'),
        NameObject('/F'): TextStringObject('other.pdf'),
        NameObject('/D'): ArrayObject([NumberObject(2), NameObject('/Fit')]),
    })
    writer.add_outline_item_dict(DictionaryObject({
        NameObject('/Title'): TextStringObject('Elsewhere'), NameObject('/A'): remote}))
    path = tmp_path / 'linked.pdf'
    writer.write(path)
    with PdfText(path) as pdf:
        pages = {b.title: b.page_index for b in pdf.bookmarks()}
    assert pages == {'Local': 1, 'Elsewhere': None}

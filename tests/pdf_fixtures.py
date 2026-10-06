"""Generate small real PDF files (text pages and a nested outline) for PDF tests."""
from pathlib import Path
from typing import Sequence, Tuple


def _escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def make_pdf(path: Path, pages: Sequence[str], outline: Sequence[Tuple] = ()) -> Path:
    """Write ``pages`` (one string per page, newlines allowed) and ``outline``.

    ``outline`` entries are ``(title, page_index)`` or ``(title, page_index, children)`` with
    zero-based page indexes, nested as deeply as wanted.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)

    writer = PdfWriter()
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
        NameObject('/Encoding'): NameObject('/WinAnsiEncoding'),
    })
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        lines = text.split('\n')
        body = ['BT', '/F1 11 Tf', '14 TL', '72 740 Td']
        for line in lines:
            body.append(f'({_escape(line)}) Tj T*')
        body.append('ET')
        stream = DecodedStreamObject()
        stream.set_data('\n'.join(body).encode('latin-1'))
        page[NameObject('/Contents')] = writer._add_object(stream)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})

    def add(entries, parent=None):
        for entry in entries:
            title, index = entry[0], entry[1]
            item = writer.add_outline_item(title, index, parent=parent)
            if len(entry) > 2:
                add(entry[2], item)

    add(outline)
    path = Path(path)
    with path.open('wb') as handle:
        writer.write(handle)
    return path

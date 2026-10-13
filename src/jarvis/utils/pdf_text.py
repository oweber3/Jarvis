"""PDF page text and bookmarks through PDFium (``pypdfium2``), shared by every PDF reader in Jarvis.

PDFium is native code: it reads a large book in seconds and releases the interpreter while it
works, so extraction never starves the audio capture thread. It is not thread safe, even across
different documents, so every call goes through one process-wide lock. See
``platform/windows/ui_automation.spec.md`` (Document data) and ``documents/documents.spec.md``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_LOCK = threading.RLock()
_ERR_PASSWORD = 4  # FPDF_ERR_PASSWORD: the file opens only with a password


class PdfTextError(ValueError):
    """The file could not be opened as a PDF (damaged, not a PDF, or needs a password)."""

    def __init__(self, message: str, needs_password: bool = False):
        super().__init__(message)
        self.needs_password = needs_password


@dataclass(frozen=True)
class Bookmark:
    title: str
    level: int  # 0 for top-level bookmarks
    page_index: Optional[int]  # 0-based, None when the bookmark points nowhere usable


class PdfText:
    """One open PDF. Close it, or use it as a context manager, to release the file."""

    def __init__(self, path: Path):
        import pypdfium2 as pdfium
        with _LOCK:
            try:
                self._doc = pdfium.PdfDocument(str(path))
                self.page_count = len(self._doc)
            except (pdfium.PdfiumError, OSError, ValueError) as exc:
                needs_password = getattr(exc, 'err_code', None) == _ERR_PASSWORD
                raise PdfTextError('This PDF needs a password.' if needs_password
                                   else 'This file could not be read as a PDF.', needs_password) from exc

    def page_text(self, index: int) -> str:
        with _LOCK:
            page = self._doc[index]
            try:
                textpage = page.get_textpage()
                try:
                    return textpage.get_text_range() or ''
                finally:
                    textpage.close()
            finally:
                page.close()

    def bookmarks(self) -> List[Bookmark]:
        import pypdfium2.raw as pdfium_c
        with _LOCK:
            found = []
            for bookmark in self._doc.get_toc():
                dest = pdfium_c.FPDFBookmark_GetDest(self._doc, bookmark)
                if not dest:
                    # Many books link bookmarks through a GoTo action rather than a direct destination.
                    action = pdfium_c.FPDFBookmark_GetAction(bookmark)
                    dest = pdfium_c.FPDFAction_GetDest(self._doc, action) if action else None
                index = pdfium_c.FPDFDest_GetDestPageIndex(self._doc, dest) if dest else -1
                found.append(Bookmark(bookmark.get_title() or '', bookmark.level,
                                      index if 0 <= index < self.page_count else None))
            return found

    def close(self) -> None:
        with _LOCK:
            self._doc.close()

    def __enter__(self) -> 'PdfText':
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


"""Local, deterministic PDF reading: outline with page numbers and keyword search with snippets.

Pure ``pypdf``; no OS or model calls. Text extraction is bounded per call and continues on a
background worker, so a large book never blocks the caller. See
``platform/windows/ui_automation.spec.md`` (pdfNavigate, Document data).
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..debug import debug_log

_WORD_RE = re.compile(r'\w+', re.UNICODE)
_CACHE_SIZE = 3
_SNIPPET_CHARS = 160
_INFLECTION_MIN = 4
_INFLECTION_MAX_EXTRA = 3
DEFAULT_SEARCH_BUDGET_SEC = 4.0


@dataclass(frozen=True)
class OutlineEntry:
    title: str
    page: int  # 1-based physical page
    level: int


@dataclass(frozen=True)
class PageHit:
    page: int
    hits: int
    snippet: str


@dataclass(frozen=True)
class SearchResult:
    hits: List[PageHit]
    pages_searched: int
    page_count: int

    @property
    def complete(self) -> bool:
        return self.pages_searched >= self.page_count


def words(text: str) -> List[str]:
    return [word.casefold() for word in _WORD_RE.findall(text or '')]


def word_matches(query_word: str, text_word: str) -> bool:
    """Equal, or (for longer words) one extends the other by a few characters, in any language."""
    if query_word == text_word:
        return True
    shorter, longer = sorted((query_word, text_word), key=len)
    return (len(shorter) >= _INFLECTION_MIN and len(longer) - len(shorter) <= _INFLECTION_MAX_EXTRA
            and longer.startswith(shorter))


def _contains_all(query: List[str], text_words: List[str]) -> bool:
    return all(any(word_matches(q, t) for t in text_words) for q in query)


class PdfDocument:
    """One PDF file: outline read on open, page text extracted lazily and kept."""

    def __init__(self, path: Path):
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
        try:
            self._reader = PdfReader(str(path))
            self.page_count = len(self._reader.pages)
        except (PdfReadError, OSError, ValueError) as exc:
            raise ValueError('This file could not be read as a PDF.') from exc
        self.path = path
        self._texts: List[str] = []
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._outline = self._read_outline()

    def _read_outline(self) -> List[OutlineEntry]:
        entries: List[OutlineEntry] = []

        def visit(items, level):
            for item in items:
                if isinstance(item, list):
                    visit(item, level + 1)
                    continue
                try:
                    page = self._reader.get_destination_page_number(item) + 1
                except Exception:  # noqa: BLE001 - a broken bookmark is skipped, not fatal
                    continue
                if page >= 1:
                    entries.append(OutlineEntry(str(item.title or '').strip(), page, level))

        try:
            visit(self._reader.outline, 0)
        except Exception as exc:  # noqa: BLE001 - a document without a readable outline has none
            debug_log(f'PDF outline unreadable ({type(exc).__name__}).', 'pdf')
        return entries

    def outline(self) -> List[OutlineEntry]:
        return list(self._outline)

    def outline_matches(self, query: str) -> List[OutlineEntry]:
        wanted = words(query)
        if not wanted:
            return []
        return [entry for entry in self._outline if _contains_all(wanted, words(entry.title))]

    def _extract_next(self) -> bool:
        """Extract one more page; False when every page is done."""
        with self._lock:
            index = len(self._texts)
            if index >= self.page_count:
                return False
            try:
                text = self._reader.pages[index].extract_text() or ''
            except Exception as exc:  # noqa: BLE001 - an unreadable page has no text
                debug_log(f'PDF page text unreadable ({type(exc).__name__}).', 'pdf')
                text = ''
            self._texts.append(text)
            return True

    def _extract_for(self, budget_sec: float) -> None:
        deadline = time.monotonic() + max(0.0, budget_sec)
        while time.monotonic() < deadline and self._extract_next():
            pass

    def _continue_in_background(self) -> None:
        with self._lock:
            if len(self._texts) >= self.page_count or (self._worker and self._worker.is_alive()):
                return

            def run():
                while self._extract_next():
                    pass
                debug_log(f'PDF indexed in background ({self.page_count} pages).', 'pdf')

            self._worker = threading.Thread(target=run, name='pdf-index', daemon=True)
            self._worker.start()

    def search(self, query: str, budget_sec: float = DEFAULT_SEARCH_BUDGET_SEC, limit: int = 8) -> SearchResult:
        wanted = words(query)
        self._extract_for(budget_sec)
        with self._lock:
            texts = list(self._texts)
        if len(texts) < self.page_count:
            self._continue_in_background()
        hits: List[PageHit] = []
        if wanted:
            for index, text in enumerate(texts):
                page_words = words(text)
                if not _contains_all(wanted, page_words):
                    continue
                count = sum(1 for t in page_words if any(word_matches(q, t) for q in wanted))
                hits.append(PageHit(index + 1, count, _snippet(text, wanted)))
        hits.sort(key=lambda hit: (-hit.hits, hit.page))
        return SearchResult(hits[:max(1, limit)], len(texts), self.page_count)


def _snippet(text: str, wanted: List[str]) -> str:
    flat = ' '.join(text.split())
    position = 0
    for match in _WORD_RE.finditer(flat):
        if any(word_matches(q, match.group().casefold()) for q in wanted):
            position = match.start()
            break
    start = max(0, position - _SNIPPET_CHARS // 3)
    snippet = flat[start:start + _SNIPPET_CHARS]
    return ('…' if start else '') + snippet + ('…' if start + _SNIPPET_CHARS < len(flat) else '')


_CACHE: 'OrderedDict[str, Tuple[Tuple[int, int], PdfDocument]]' = OrderedDict()
_CACHE_LOCK = threading.Lock()


def open_document(path) -> PdfDocument:
    """The document at ``path``, reused while its size and modification time are unchanged."""
    path = Path(path)
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise ValueError('The PDF file was not found.') from exc
    key, stamp = str(path.resolve()).casefold(), (stat.st_size, stat.st_mtime_ns)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == stamp:
            _CACHE.move_to_end(key)
            return cached[1]
    document = PdfDocument(path)
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, document)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return document


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


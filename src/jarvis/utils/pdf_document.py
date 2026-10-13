"""Local, deterministic PDF reading: outline with page numbers and keyword search with snippets.

Reads through PDFium (``utils/pdf_text.py``); no OS or model calls. Text extraction is bounded
per call and continues on a background worker, so a large book never blocks the caller. The file
is open only while it is being read, so the viewer can still save it. See
``platform/windows/ui_automation.spec.md`` (pdfNavigate, Document data).
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..debug import debug_log
from .pdf_text import PdfText

# A section number such as "1.6" is one word, so "chapter 6" never matches section 1.6.
_WORD_RE = re.compile(r'\d+(?:\.\d+)+|\w+', re.UNICODE)
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
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
    parents: Tuple[str, ...] = ()  # titles of the entries above it, outermost first

    @property
    def label(self) -> str:
        return ' > '.join(self.parents + (self.title,))


@dataclass(frozen=True)
class PageHit:
    page: int
    hits: int
    snippet: str
    partial: bool = False  # the page has some query words, not all


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
    """Equal, or (for longer words) one extends the other by a few characters, in any language.

    Numbers match only exactly: "10.12" is not "10.1" and "2024" is not "202".
    """
    if query_word == text_word:
        return True
    if any(ch.isdigit() for ch in query_word + text_word):
        return False
    shorter, longer = sorted((query_word, text_word), key=len)
    return (len(shorter) >= _INFLECTION_MIN and len(longer) - len(shorter) <= _INFLECTION_MAX_EXTRA
            and longer.startswith(shorter))


def _contains_all(query: List[str], text_words: List[str]) -> bool:
    return all(any(word_matches(q, t) for t in text_words) for q in query)


def _shared(query: List[str], text_words: List[str]) -> int:
    return sum(1 for q in query if any(word_matches(q, t) for t in text_words))


class PdfDocument:
    """One PDF file: outline read on open, page text extracted lazily and kept."""

    def __init__(self, path: Path):
        self.path = path
        self._texts: List[str] = []
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._pdf: Optional[PdfText] = None
        with PdfText(path) as pdf:  # PdfTextError is a ValueError
            self.page_count = pdf.page_count
            self._outline = self._read_outline(pdf)

    @staticmethod
    def _read_outline(pdf: PdfText) -> List[OutlineEntry]:
        try:
            bookmarks = pdf.bookmarks()
        except Exception as exc:  # noqa: BLE001 - a document without a readable outline has none
            debug_log(f'PDF outline unreadable ({type(exc).__name__}).', 'pdf')
            return []
        entries: List[OutlineEntry] = []
        path: List[str] = []  # titles of the bookmarks enclosing the current one
        for bookmark in bookmarks:
            title = _CONTROL_RE.sub('', bookmark.title).strip()
            del path[bookmark.level:]
            parents = tuple(path)
            path.append(title)
            if bookmark.page_index is not None:  # a broken bookmark is skipped, its children kept
                entries.append(OutlineEntry(title, bookmark.page_index + 1, bookmark.level, parents))
        return entries

    def _release(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None

    def outline(self) -> List[OutlineEntry]:
        return list(self._outline)

    def _scored(self, wanted: List[str]) -> List[Tuple[OutlineEntry, int]]:
        """Entries with how many query words their title and the titles above it share.

        An entry is kept when its own title shares a word or it shares more than every entry above
        it, so a section that adds nothing to its chapter's match is not listed beside the chapter.
        """
        scored = []
        for entry in self._outline:
            path = entry.parents + (entry.title,)
            shares = [_shared(wanted, words(' '.join(path[:depth]))) for depth in range(1, len(path) + 1)]
            if shares[-1] and (shares[-1] > max(shares[:-1], default=0) or _shared(wanted, words(entry.title))):
                scored.append((entry, shares[-1]))
        return scored

    def outline_matches(self, query: str) -> List[OutlineEntry]:
        """Entries whose title, with the titles above it, contains every word of ``query``."""
        wanted = words(query)
        if not wanted:
            return []
        return [entry for entry, shared in self._scored(wanted) if shared == len(wanted)]

    def outline_closest(self, query: str, limit: int) -> List[OutlineEntry]:
        """Entries sharing more than half but not all query words, most shared first, outer entries first.

        Sharing half or fewer (only "problems" of "problem 4.13") says too little to offer the entry.
        """
        wanted = words(query)
        partial = [(entry, shared) for entry, shared in self._scored(wanted)
                   if len(wanted) / 2 < shared < len(wanted)]
        partial.sort(key=lambda item: (-item[1], item[0].level, item[0].page))
        return [entry for entry, _shared_count in partial[:max(0, limit)]]

    def _extract_next(self) -> bool:
        """Extract one more page; False when every page is done."""
        with self._lock:
            index = len(self._texts)
            if index >= self.page_count:
                return False
            if self._pdf is None:
                try:
                    self._pdf = PdfText(self.path)
                except ValueError:
                    debug_log('PDF could not be reopened for its text.', 'pdf')
                    self._texts.extend([''] * (self.page_count - index))  # moved or deleted since
                    return False
            try:
                text = self._pdf.page_text(index)
            except Exception as exc:  # noqa: BLE001 - an unreadable page has no text
                debug_log(f'PDF page text unreadable ({type(exc).__name__}).', 'pdf')
                text = ''
            self._texts.append(text)
            if len(self._texts) >= self.page_count:
                self._release()
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
        unique = list(dict.fromkeys(wanted))
        found = []  # (index, text, hit count, query words on the page)
        for index, text in enumerate(texts):
            page_words = words(text)
            vocabulary = set(page_words)
            present = frozenset(q for q in unique if any(word_matches(q, t) for t in vocabulary))
            if present:
                count = sum(1 for t in page_words if any(word_matches(q, t) for q in present))
                found.append((index, text, count, present))
        # Pages with every word lead. Then pages with some, scored by how rare their words are in
        # the document (inverse document frequency), so "4.13" outweighs "problem".
        frequency = {q: sum(1 for item in found if q in item[3]) for q in unique}
        weight = {q: math.log((len(texts) + 1) / (frequency[q] + 1)) for q in unique}
        full = sorted((item for item in found if len(item[3]) == len(unique)), key=lambda i: (-i[2], i[0]))
        partial = sorted((item for item in found if len(item[3]) < len(unique)),
                         key=lambda i: (-sum(weight[q] for q in i[3]), -i[2], i[0]))
        hits = [PageHit(i + 1, count, _snippet(text, unique)) for i, text, count, _present in full]
        for index, text, count, present in partial[:max(0, limit - len(hits))]:
            rarest = max(present, key=lambda q: weight[q])
            hits.append(PageHit(index + 1, count, _snippet(text, [rarest]), partial=True))
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


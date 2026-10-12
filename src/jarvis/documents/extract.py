"""Text extraction for document search, and chunking into passages.

Reads ``.txt``, ``.md``, ``.pdf`` (``pypdf``) and ``.docx`` (the archive's ``word/document.xml``, standard
library only). Every reader is bounded. No OS, network or model calls. See ``documents.spec.md``.
"""
from __future__ import annotations

import codecs
import re
import zipfile
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple
from xml.etree import ElementTree

SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".docx"})
MAX_TEXT_CHARS = 2_000_000
MAX_PDF_PAGES = 1000
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 200
MAX_CHUNKS_PER_FILE = 400

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPHS = re.compile(r"\n\s*\n")
_SPACE = re.compile(r"\s+")

Segment = Tuple[Optional[int], str]


class ExtractError(Exception):
    """The file cannot be read as a supported document (reason code in ``args[0]``)."""


class Chunk(NamedTuple):
    page: Optional[int]
    text: str


def extract_segments(path: Path) -> List[Segment]:
    """``(page, text)`` pieces of the file: one per PDF page (pages start at 1), otherwise one with page None."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ExtractError("unsupported")
    if suffix == ".pdf":
        segments = _pdf(path)
    elif suffix == ".docx":
        segments = [(None, _docx(path))]
    else:
        segments = [(None, _plain(path))]
    return _bounded(segments)


def _bounded(segments: List[Segment]) -> List[Segment]:
    kept: List[Segment] = []
    left = MAX_TEXT_CHARS
    for page, text in segments:
        if left <= 0:
            break
        kept.append((page, text[:left]))
        left -= len(text)
    return kept


def _plain(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_TEXT_CHARS * 4)
    except OSError as exc:
        raise ExtractError("unreadable") from exc
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def _pdf(path: Path) -> List[Segment]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("encrypted")
        segments: List[Segment] = []
        for number, page in enumerate(reader.pages, start=1):
            if number > MAX_PDF_PAGES:
                break
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                segments.append((number, text))
        return segments
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError("pdf_unreadable") from exc


def _docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ExtractError("docx_too_large")
            with archive.open(info) as handle:
                paragraphs: List[str] = []
                parts: List[str] = []
                for _event, element in ElementTree.iterparse(handle, events=("end",)):
                    tag = element.tag
                    if tag == _W + "t" and element.text:
                        parts.append(element.text)
                    elif tag == _W + "tab":
                        parts.append("\t")
                    elif tag in (_W + "br", _W + "cr"):
                        parts.append("\n")
                    elif tag == _W + "p":
                        paragraphs.append("".join(parts))
                        parts = []
                        element.clear()
                return "\n\n".join(p for p in paragraphs if p.strip())
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError("docx_unreadable") from exc


def chunk_segments(segments: List[Segment]) -> List[Chunk]:
    """Passages of about ``CHUNK_CHARS`` characters, split on paragraphs, overlapping by ``CHUNK_OVERLAP``."""
    chunks: List[Chunk] = []
    for page, text in segments:
        for passage in _chunk_text(text):
            chunks.append(Chunk(page, passage))
            if len(chunks) >= MAX_CHUNKS_PER_FILE:
                return chunks
    return chunks


def _pieces(text: str) -> List[str]:
    """Paragraphs with whitespace collapsed; one longer than a passage is cut at word boundaries."""
    pieces: List[str] = []
    for paragraph in _PARAGRAPHS.split(text):
        paragraph = _SPACE.sub(" ", paragraph).strip()
        while len(paragraph) > CHUNK_CHARS:
            cut = paragraph.rfind(" ", 0, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            pieces.append(paragraph)
    return pieces


def _tail(text: str) -> str:
    """The last ``CHUNK_OVERLAP`` characters of ``text``, starting at a word."""
    if len(text) <= CHUNK_OVERLAP:
        return text
    tail = text[-CHUNK_OVERLAP:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < len(tail) - 1 else tail


def _chunk_text(text: str) -> List[str]:
    passages: List[str] = []
    current = ""
    for piece in _pieces(text):
        if current and len(current) + 1 + len(piece) > CHUNK_CHARS:
            passages.append(current)
            current = _tail(current) + " " + piece
        else:
            current = f"{current} {piece}" if current else piece
    if current and (not passages or current != _tail(passages[-1])):
        passages.append(current)
    return passages

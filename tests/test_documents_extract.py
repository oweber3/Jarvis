"""Document search: text extraction from each supported format, and chunking."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jarvis.documents import extract
from pdf_fixtures import make_pdf


def make_docx(path: Path, paragraphs, *, padding: int = 0) -> Path:
    """A minimal real .docx: the parts Word needs and one paragraph per entry."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs)
    body += f"<w:p><w:r><w:t>{'x' * padding}</w:t></w:r></w:p>" if padding else ""
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return path


@pytest.mark.unit
class TestExtraction:
    def test_plain_text_and_markdown(self, tmp_path):
        (tmp_path / "a.txt").write_text("Pets are allowed with a deposit.", encoding="utf-8")
        (tmp_path / "b.md").write_text("# Exam\n\nEntropy always increases.", encoding="utf-8")
        assert extract.extract_segments(tmp_path / "a.txt") == [(None, "Pets are allowed with a deposit.")]
        assert "Entropy always increases." in extract.extract_segments(tmp_path / "b.md")[0][1]

    def test_byte_order_marks_are_honoured(self, tmp_path):
        (tmp_path / "u8.txt").write_bytes("﻿café".encode("utf-8"))
        (tmp_path / "u16.txt").write_bytes("naïve".encode("utf-16"))
        assert extract.extract_segments(tmp_path / "u8.txt")[0][1] == "café"
        assert extract.extract_segments(tmp_path / "u16.txt")[0][1] == "naïve"

    def test_undecodable_bytes_do_not_fail(self, tmp_path):
        (tmp_path / "bad.txt").write_bytes(b"ok \xff\xfe\xfa still ok")
        text = extract.extract_segments(tmp_path / "bad.txt")[0][1]
        assert text.startswith("ok") and text.endswith("still ok")

    def test_pdf_pages_keep_their_numbers(self, tmp_path):
        pdf = make_pdf(tmp_path / "lease.pdf", ["Rent is due monthly.", "No pets without written consent."])
        segments = extract.extract_segments(pdf)
        assert [page for page, _ in segments] == [1, 2]
        assert "pets" in segments[1][1]

    def test_password_protected_pdf_is_skipped(self, tmp_path):
        from pypdf import PdfReader, PdfWriter
        plain = make_pdf(tmp_path / "plain.pdf", ["Salary details."])
        writer = PdfWriter(clone_from=PdfReader(plain))
        writer.encrypt(user_password="hunter2", owner_password="owner")
        locked = tmp_path / "locked.pdf"
        writer.write(locked)
        with pytest.raises(extract.ExtractError) as caught:
            extract.extract_segments(locked)
        assert caught.value.args[0] == "encrypted"

    def test_docx_paragraphs(self, tmp_path):
        doc = make_docx(tmp_path / "notes.docx", ["Thermodynamics exam on Friday.", "Revise the Carnot cycle."])
        text = extract.extract_segments(doc)[0][1]
        assert "Thermodynamics exam on Friday." in text and "Carnot" in text
        assert text.index("Friday.") < text.index("Revise")

    def test_oversized_docx_part_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(extract, "MAX_DOCX_XML_BYTES", 1000)
        doc = make_docx(tmp_path / "bomb.docx", ["hi"], padding=5000)
        with pytest.raises(extract.ExtractError):
            extract.extract_segments(doc)

    def test_broken_files_raise_extract_error(self, tmp_path):
        (tmp_path / "broken.docx").write_bytes(b"not a zip")
        (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
        for name in ("broken.docx", "broken.pdf"):
            with pytest.raises(extract.ExtractError):
                extract.extract_segments(tmp_path / name)

    def test_unsupported_extension_is_refused(self, tmp_path):
        (tmp_path / "x.exe").write_bytes(b"MZ")
        with pytest.raises(extract.ExtractError):
            extract.extract_segments(tmp_path / "x.exe")

    def test_text_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(extract, "MAX_TEXT_CHARS", 100)
        (tmp_path / "long.txt").write_text("word " * 1000, encoding="utf-8")
        assert sum(len(t) for _, t in extract.extract_segments(tmp_path / "long.txt")) <= 100


@pytest.mark.unit
class TestChunking:
    def test_short_text_is_one_passage(self):
        assert [c.text for c in extract.chunk_segments([(None, "One short note.")])] == ["One short note."]

    def test_passages_respect_size_and_overlap(self):
        paragraphs = [f"Paragraph {i} " + "lorem ipsum dolor " * 12 for i in range(20)]
        chunks = extract.chunk_segments([(None, "\n\n".join(paragraphs))])
        assert len(chunks) > 1
        assert all(len(c.text) <= extract.CHUNK_CHARS + extract.CHUNK_OVERLAP for c in chunks)
        # Neighbouring passages share text, so a sentence on a boundary is still found whole.
        for first, second in zip(chunks, chunks[1:]):
            tail = first.text[-40:]
            assert tail.split()[-1] in second.text

    def test_one_long_paragraph_is_cut_at_words(self):
        chunks = extract.chunk_segments([(None, "alpha " * 600)])
        assert len(chunks) > 1
        assert all(c.text.split() and set(c.text.split()) == {"alpha"} for c in chunks)

    def test_pages_are_kept_per_passage(self):
        chunks = extract.chunk_segments([(1, "first page text"), (2, "second page text")])
        assert [(c.page, c.text) for c in chunks] == [(1, "first page text"), (2, "second page text")]

    def test_whitespace_is_collapsed_and_empty_text_gives_nothing(self):
        assert extract.chunk_segments([(None, "  \n\n \t ")]) == []
        assert extract.chunk_segments([(None, "a   b\t\tc")])[0].text == "a b c"

    def test_passages_per_file_are_capped(self, monkeypatch):
        monkeypatch.setattr(extract, "MAX_CHUNKS_PER_FILE", 3)
        assert len(extract.chunk_segments([(None, "word " * 5000)])) == 3

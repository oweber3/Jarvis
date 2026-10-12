"""Web chat uploads: what a file is is decided by its content, never its name, and nothing is kept on disk.

See ``webchat/webchat.spec.md`` (Attachments).
"""
import base64
import io

import pytest
from PIL import Image

from jarvis.reply.attachments import FILE_START, MAX_FILE_CHARS, RequestAttachment
from jarvis.tools.types import ToolImage
from jarvis.webchat import uploads
from jarvis.webchat.uploads import UploadRefused, UploadStore, classify
from pdf_fixtures import make_pdf


def _png(size=(40, 30), mode="RGB", colour="red") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _refused(data: bytes) -> UploadRefused:
    with pytest.raises(UploadRefused) as caught:
        classify(data)
    return caught.value


class TestImages:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
    def test_real_images_of_every_supported_kind_are_accepted(self, fmt):
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), "blue").save(buffer, format=fmt)
        attached = classify(buffer.getvalue())
        assert attached.kind == "image" and attached.size == len(buffer.getvalue())
        assert attached.image.mime_type == "image/jpeg"
        decoded = Image.open(io.BytesIO(base64.b64decode(attached.image.data)))
        assert decoded.size == (40, 30)

    def test_a_large_image_is_scaled_down_like_a_screenshot(self):
        from jarvis.tools.builtin.screenshot import MAX_IMAGE_EDGE
        attached = classify(_png((MAX_IMAGE_EDGE * 2, MAX_IMAGE_EDGE)))
        decoded = Image.open(io.BytesIO(base64.b64decode(attached.image.data)))
        assert max(decoded.size) == MAX_IMAGE_EDGE

    def test_a_transparent_image_is_flattened_rather_than_refused(self):
        attached = classify(_png(mode="RGBA", colour=(255, 0, 0, 0)))
        assert attached.kind == "image"

    def test_embedded_metadata_such_as_location_does_not_travel_on(self):
        source = Image.new("RGB", (40, 30), "green")
        exif = Image.Exif()
        exif[0x010F] = "SomeCamera"
        exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)
        assert b"SomeCamera" in buffer.getvalue()
        sent = base64.b64decode(classify(buffer.getvalue()).image.data)
        assert b"SomeCamera" not in sent and not Image.open(io.BytesIO(sent)).getexif()

    def test_a_broken_image_is_refused(self):
        refused = _refused(_png()[:30])
        assert refused.code == "unreadable"

    def test_a_photo_taken_sideways_is_turned_upright_before_the_metadata_goes(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # the camera says: rotate to display
        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), "blue").save(buffer, format="JPEG", exif=exif)
        decoded = Image.open(io.BytesIO(base64.b64decode(classify(buffer.getvalue()).image.data)))
        assert decoded.size == (20, 40)

    def test_an_image_with_an_absurd_pixel_count_is_refused(self):
        buffer = io.BytesIO()
        Image.new("L", (uploads.MAX_PIXELS // 4000 + 1, 4000)).save(buffer, format="PNG")
        assert len(buffer.getvalue()) < uploads.MAX_IMAGE_BYTES
        assert _refused(buffer.getvalue()).code == "too_large"

    def test_an_oversized_image_file_is_refused_before_it_is_decoded(self):
        assert _refused(_png() + b"\0" * uploads.MAX_IMAGE_BYTES).code == "too_large"


class TestTextFiles:
    def test_plain_text_is_fenced_as_data(self):
        attached = classify(b"print('hello')\n")
        assert attached.kind == "text" and attached.size == 15
        assert FILE_START in attached.text and "print('hello')" in attached.text

    def test_utf8_with_and_without_a_byte_order_mark(self):
        assert "café" in classify("café".encode("utf-8")).text
        assert "café" in classify(b"\xef\xbb\xbf" + "café".encode("utf-8")).text

    def test_utf16_with_a_byte_order_mark(self):
        assert "naïve" in classify("naïve".encode("utf-16")).text

    def test_a_windows_code_page_file_is_still_text(self):
        assert "café" in classify("café".encode("cp1252")).text

    def test_code_csv_and_markdown_are_text_whatever_they_are_called(self):
        for sample in (b"a,b,c\n1,2,3\n", b"# Title\n\n* item\n", b"<svg xmlns='x'><rect/></svg>"):
            assert classify(sample).kind == "text"

    def test_a_long_file_is_capped_not_refused(self):
        attached = classify(b"Q" * (MAX_FILE_CHARS * 3))
        assert attached.text.count("Q") == MAX_FILE_CHARS and "cut" in attached.text

    def test_a_huge_text_file_is_refused(self):
        assert _refused(b"Q" * (uploads.MAX_TEXT_BYTES + 1)).code == "too_large"

    def test_binary_data_is_refused_as_unsupported(self):
        for sample in (b"MZ\x90\x00\x03\x00\x00\x00" + bytes(range(256)),
                       b"PK\x03\x04" + bytes(100),
                       bytes(range(256)) * 8):
            assert _refused(sample).code == "unsupported"

    def test_an_empty_file_is_refused(self):
        assert _refused(b"").code == "empty"


class TestPdf:
    def test_the_text_of_a_pdf_is_read_locally(self, tmp_path):
        path = make_pdf(tmp_path / "doc.pdf", ["First page words", "Second page words"])
        attached = classify(path.read_bytes())
        assert attached.kind == "text"
        assert "First page words" in attached.text and "Second page words" in attached.text
        assert FILE_START in attached.text

    def test_a_pdf_with_no_text_is_refused_with_its_reason(self, tmp_path):
        path = make_pdf(tmp_path / "blank.pdf", [""])
        assert _refused(path.read_bytes()).code == "no_text"

    def test_reading_a_pdf_never_prints_what_pypdf_found_in_the_file(self, tmp_path, capfd):
        for sample in (b"%PDF-1.4 SECRET-IN-FILE not really a pdf", make_pdf(tmp_path / "d.pdf", ["words"]).read_bytes()[:90]):
            with pytest.raises(UploadRefused):
                classify(sample)
        captured = capfd.readouterr()
        assert "SECRET-IN-FILE" not in captured.out + captured.err and not captured.err

    def test_a_text_file_that_merely_mentions_the_pdf_signature_is_still_text(self):
        assert classify(b"notes: files start with %PDF- as a header").kind == "text"

    def test_a_damaged_pdf_is_refused(self, tmp_path):
        path = make_pdf(tmp_path / "doc.pdf", ["words"])
        assert _refused(path.read_bytes()[:60]).code == "unreadable"

    def test_a_password_protected_pdf_is_refused(self, tmp_path):
        from pypdf import PdfReader, PdfWriter
        path = make_pdf(tmp_path / "doc.pdf", ["words"])
        writer = PdfWriter()
        for page in PdfReader(str(path)).pages:
            writer.add_page(page)
        writer.encrypt("secret")
        buffer = io.BytesIO()
        writer.write(buffer)
        assert _refused(buffer.getvalue()).code == "encrypted"


class TestHoldingUploads:
    @staticmethod
    def _attachment(size=10):
        return RequestAttachment("image", size, image=ToolImage("image/jpeg", "QUJD"))

    def test_an_upload_is_held_under_an_unguessable_id_and_handed_over_once(self):
        store = UploadStore()
        one = store.add(self._attachment())
        assert len(one) >= 16 and one != store.add(self._attachment())
        assert store.take([one])[0].kind == "image"
        with pytest.raises(UploadRefused) as gone:
            store.take([one])
        assert gone.value.code == "unknown_attachment"

    def test_a_request_with_one_unknown_id_takes_nothing(self):
        store = UploadStore()
        known = store.add(self._attachment())
        with pytest.raises(UploadRefused):
            store.take([known, "nope"])
        assert store.peek([known])[0].kind == "image"

    def test_peeking_does_not_consume(self):
        store = UploadStore()
        known = store.add(self._attachment())
        store.peek([known])
        assert len(store.take([known])) == 1

    def test_discarding_removes_it(self):
        store = UploadStore()
        known = store.add(self._attachment())
        store.discard(known)
        assert len(store) == 0
        store.discard("never existed")  # harmless

    def test_unclaimed_uploads_expire(self):
        now = [1000.0]
        store = UploadStore(ttl_sec=60, clock=lambda: now[0])
        known = store.add(self._attachment())
        now[0] += 61
        with pytest.raises(UploadRefused):
            store.take([known])
        assert len(store) == 0

    def test_too_many_waiting_uploads_are_refused(self):
        store = UploadStore()
        for _ in range(uploads.MAX_HELD):
            store.add(self._attachment())
        with pytest.raises(UploadRefused) as full:
            store.add(self._attachment())
        assert full.value.code == "too_many"

    def test_a_message_cannot_carry_more_than_the_limits(self):
        store = UploadStore()
        ids = [store.add(self._attachment()) for _ in range(uploads.MAX_HELD)]
        from jarvis.reply.attachments import MAX_ATTACHMENTS
        with pytest.raises(UploadRefused) as many:
            store.take(ids[:MAX_ATTACHMENTS + 1])
        assert many.value.code == "too_many"

    def test_clearing_forgets_everything(self):
        store = UploadStore()
        store.add(self._attachment())
        store.clear()
        assert len(store) == 0

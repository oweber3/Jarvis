"""Web chat attachments over real sockets: the upload endpoint, the Screenshot button, sending with
attachments, what is refused and what is (not) kept.

The daemon is a fake and storage is a temporary file. See ``webchat/webchat.spec.md`` (Attachments).
"""
import http.client
import io
import json

import pytest
from PIL import Image

from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ChatStore
from jarvis.reply.attachments import MAX_ATTACHMENTS, RequestAttachment
from jarvis.tools.types import ToolImage
from jarvis.webchat import uploads
from jarvis.webchat.hub import ChatHub
from jarvis.webchat.server import ServerSettings, WebChatServer

SECRET = "ZEBRA-QUARTERLY-FIGURES-7731"


@pytest.fixture
def parts(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>Jarvis Chat</title>")
    backend = FakeWebChatBackend()
    store = ChatStore(str(tmp_path / "jarvis.db"))
    hub = ChatHub(backend, store, sync_interval_sec=0.05)
    server = WebChatServer(hub, ServerSettings(host="127.0.0.1", port=0, static_dir=static, poll_timeout_sec=0.3))
    server.start()
    yield type("Parts", (), {"server": server, "backend": backend, "store": store, "hub": hub, "dir": tmp_path})()
    server.stop()
    store.close()


def call(parts, method, path, body=None, headers=None, *, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", parts.server.port, timeout=10)
    sent = {"Host": f"127.0.0.1:{parts.server.port}"}
    payload = raw
    if body is not None:
        payload = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    if payload is not None:
        sent.setdefault("Content-Length", str(len(payload)))
    for name, value in sent.items():
        if value is not None:
            conn.putheader(name, value)
    conn.endheaders(payload)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    try:
        parsed = json.loads(data) if data and response.getheader("Content-Type", "").startswith("application/json") else data
    except ValueError:
        parsed = data
    return response.status, parsed, response


def upload(parts, data: bytes, **kw):
    headers = {"Content-Type": "application/octet-stream"}
    headers.update(kw.pop("headers", {}))
    return call(parts, "POST", "/api/attachments", raw=data, headers=headers, **kw)


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def screenshot_attachment() -> RequestAttachment:
    return RequestAttachment("screenshot", 5000, image=ToolImage("image/jpeg", "QUJD"), text="<screen text block>")


class TestUploading:
    def test_an_image_is_accepted_and_described_without_its_content(self, parts):
        status, body, _ = upload(parts, png())
        assert status == 201
        assert body["kind"] == "image" and body["size"] == len(png()) and len(body["id"]) >= 16
        assert set(body) == {"id", "kind", "size"}

    def test_a_text_file_is_accepted_by_its_content_not_its_name(self, parts):
        status, body, _ = upload(parts, f"notes: {SECRET}\n".encode(), headers={"X-File-Name": "evil.exe"})
        assert status == 201 and body["kind"] == "text"

    def test_an_executable_renamed_to_text_is_refused(self, parts):
        status, body, _ = upload(parts, b"MZ\x90\x00" + bytes(300), headers={"X-File-Name": "notes.txt"})
        assert (status, body["error"]) == (415, "unsupported")

    def test_an_image_renamed_is_still_an_image(self, parts):
        status, body, _ = upload(parts, png(), headers={"X-File-Name": "notes.txt"})
        assert status == 201 and body["kind"] == "image"

    def test_an_oversized_upload_is_refused(self, parts):
        status, body, _ = upload(parts, b"\0" * (uploads.MAX_UPLOAD_BYTES + 1))
        assert (status, body["error"]) == (413, "too_large")

    def test_an_oversized_text_file_is_refused_with_its_reason(self, parts):
        status, body, _ = upload(parts, b"Q" * (uploads.MAX_TEXT_BYTES + 1))
        assert (status, body["error"]) == (413, "too_large")

    def test_an_empty_upload_is_refused(self, parts):
        status, body, _ = upload(parts, b"")
        assert (status, body["error"]) == (400, "empty")

    def test_the_body_must_be_declared_as_raw_bytes(self, parts):
        status, _, _ = call(parts, "POST", "/api/attachments", raw=png(), headers={"Content-Type": "image/png"})
        assert status == 415
        status, _, _ = call(parts, "POST", "/api/attachments", raw=png(), headers={"Content-Type": None})
        assert status == 415

    def test_the_json_api_keeps_its_small_limit(self, parts):
        status, _, _ = call(parts, "POST", "/api/chat", raw=b"x" * 70_000, headers={"Content-Type": "application/json"})
        assert status == 413

    def test_a_foreign_host_or_origin_is_refused_like_everywhere_else(self, parts):
        assert upload(parts, png(), headers={"Host": "evil.example"})[0] == 403
        assert upload(parts, png(), headers={"Origin": "http://evil.example"})[0] == 403
        assert upload(parts, png(), headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert call(parts, "POST", "/api/attachments/screenshot", body={},
                    headers={"Origin": "http://evil.example"})[0] == 403
        assert len(parts.hub.uploads) == 0

    def test_uploads_only_exist_for_posting(self, parts):
        assert call(parts, "GET", "/api/attachments")[0] == 404

    def test_a_waiting_upload_can_be_withdrawn(self, parts):
        _, body, _ = upload(parts, png())
        assert call(parts, "DELETE", f"/api/attachments/{body['id']}")[0] == 200
        assert len(parts.hub.uploads) == 0
        assert call(parts, "DELETE", f"/api/attachments/{body['id']}")[0] == 200  # harmless to repeat

    def test_the_number_of_waiting_uploads_is_limited(self, parts):
        for _ in range(uploads.MAX_HELD):
            assert upload(parts, b"some text")[0] == 201
        status, body, _ = upload(parts, b"some text")
        assert (status, body["error"]) == (409, "too_many")


class TestScreenshotButton:
    def test_the_screen_is_captured_into_an_attachment(self, parts):
        parts.backend.screenshot_result = screenshot_attachment()
        status, body, _ = call(parts, "POST", "/api/attachments/screenshot", body={})
        assert status == 201 and body["kind"] == "screenshot" and body["size"] == 5000
        assert "<screen text block>" not in json.dumps(body)

    def test_screen_awareness_off_is_explained(self, parts):
        parts.backend.screenshot_error = "screen_awareness_off"
        status, body, _ = call(parts, "POST", "/api/attachments/screenshot", body={})
        assert (status, body["error"]) == (409, "screen_awareness_off")

    def test_a_failed_capture_is_reported(self, parts):
        parts.backend.screenshot_error = "screenshot_unavailable"
        status, body, _ = call(parts, "POST", "/api/attachments/screenshot", body={})
        assert (status, body["error"]) == (503, "screenshot_unavailable")


class TestWhatAttachingCanDo:
    def test_the_page_can_ask_what_is_possible_and_the_limits(self, parts):
        status, body, _ = call(parts, "GET", "/api/attachments/support")
        assert status == 200
        assert body["images"] == {"ok": True, "reason": None}
        assert body["screenshot"] == {"ok": True, "reason": None}
        assert body["limits"]["max_upload_bytes"] == uploads.MAX_UPLOAD_BYTES
        assert body["limits"]["max_attachments"] == MAX_ATTACHMENTS

    def test_the_reasons_are_passed_on(self, parts):
        parts.backend.support = {"images": {"ok": False, "reason": "no_vision"},
                                 "screenshot": {"ok": False, "reason": "screen_awareness_off"}}
        _, body, _ = call(parts, "GET", "/api/attachments/support")
        assert body["images"]["reason"] == "no_vision" and body["screenshot"]["reason"] == "screen_awareness_off"


class TestSendingWithAttachments:
    def _send(self, parts, ids, text="what is this?"):
        return call(parts, "POST", "/api/chat", body={"text": text, "attachments": ids})

    def test_the_request_carries_the_attachments_to_the_assistant(self, parts):
        _, image, _ = upload(parts, png())
        _, note, _ = upload(parts, f"memo {SECRET}".encode())
        status, _, _ = self._send(parts, [image["id"], note["id"]])
        assert status == 202
        [received] = parts.backend.attachments_received
        assert [a.kind for a in received] == ["image", "text"]
        assert received[0].image.mime_type == "image/jpeg" and SECRET in received[1].text
        assert "memo" not in parts.backend.submitted[0]  # the typed text itself is untouched

    def test_an_attachment_is_used_once(self, parts):
        _, note, _ = upload(parts, b"some text")
        assert self._send(parts, [note["id"]])[0] == 202
        status, body, _ = self._send(parts, [note["id"]])
        assert (status, body["error"]) == (404, "unknown_attachment")
        assert len(parts.backend.submitted) == 1

    def test_an_unknown_attachment_stops_the_whole_request(self, parts):
        _, note, _ = upload(parts, b"some text")
        status, body, _ = self._send(parts, [note["id"], "0" * 32])
        assert (status, body["error"]) == (404, "unknown_attachment")
        assert parts.backend.submitted == []
        assert len(parts.hub.uploads) == 1  # nothing was taken

    def test_a_message_cannot_carry_too_many(self, parts):
        ids = [upload(parts, b"some text")[1]["id"] for _ in range(MAX_ATTACHMENTS + 1)]
        status, body, _ = self._send(parts, ids)
        assert (status, body["error"]) == (409, "too_many")
        assert parts.backend.submitted == []

    def test_attachments_must_be_a_list_of_ids(self, parts):
        for bad in ("abc", [1, 2], {"a": 1}, [None]):
            status, body, _ = call(parts, "POST", "/api/chat", body={"text": "hi", "attachments": bad})
            assert (status, body["error"]) == (400, "bad_attachments")

    def test_a_message_still_needs_words(self, parts):
        _, note, _ = upload(parts, b"some text")
        status, body, _ = self._send(parts, [note["id"]], text="   ")
        assert (status, body["error"]) == (400, "bad_text")

    def test_a_plain_message_is_unchanged(self, parts):
        assert call(parts, "POST", "/api/chat", body={"text": "hi"})[0] == 202
        assert parts.backend.attachments_received == [()]

    def test_the_stored_message_records_only_kind_and_size(self, parts):
        _, image, _ = upload(parts, png())
        _, note, _ = upload(parts, f"memo {SECRET}".encode())
        self._send(parts, [image["id"], note["id"]])
        parts.hub.sync()
        chat_id = parts.store.get_active_chat_id()
        _, body, _ = call(parts, "GET", f"/api/chats/{chat_id}")
        user, assistant = body["messages"]
        assert user["attachments"] == [{"kind": "image", "size": len(png())},
                                       {"kind": "text", "size": len(f"memo {SECRET}")}]
        assert assistant["attachments"] == []
        assert SECRET not in json.dumps(body)
        snapshot = call(parts, "GET", "/api/poll?rev=-1&after=0")[1]
        assert snapshot["messages"][0]["attachments"] == user["attachments"]

    def test_only_the_message_that_carried_them_gets_the_markers(self, parts):
        _, note, _ = upload(parts, b"some text")
        self._send(parts, [note["id"]], text="first")
        parts.hub.sync()
        self._send(parts, [], text="second")
        parts.hub.sync()
        _, body, _ = call(parts, "GET", f"/api/chats/{parts.store.get_active_chat_id()}")
        assert [len(m["attachments"]) for m in body["messages"]] == [1, 0, 0, 0]


class TestModelsThatCannotTakeImages:
    def test_a_local_model_without_vision_refuses_an_image_and_says_so(self, parts):
        parts.backend.support = {"images": {"ok": False, "reason": "no_vision"},
                                 "screenshot": {"ok": True, "reason": None}}
        _, image, _ = upload(parts, png())
        status, body, _ = call(parts, "POST", "/api/chat", body={"text": "what is this?", "attachments": [image["id"]]})
        assert (status, body["error"]) == (409, "no_vision")
        assert parts.backend.submitted == []

    def test_a_refused_send_keeps_the_upload_for_after_the_model_is_changed(self, parts):
        parts.backend.support = {"images": {"ok": False, "reason": "no_vision"},
                                 "screenshot": {"ok": True, "reason": None}}
        _, image, _ = upload(parts, png())
        body = {"text": "what is this?", "attachments": [image["id"]]}
        assert call(parts, "POST", "/api/chat", body=body)[0] == 409
        parts.backend.support = {"images": {"ok": True, "reason": None}, "screenshot": {"ok": True, "reason": None}}
        assert call(parts, "POST", "/api/chat", body=body)[0] == 202

    def test_a_screenshot_still_goes_to_a_model_without_vision_for_its_text(self, parts):
        parts.backend.support = {"images": {"ok": False, "reason": "no_vision"},
                                 "screenshot": {"ok": True, "reason": None}}
        parts.backend.screenshot_result = screenshot_attachment()
        _, shot, _ = call(parts, "POST", "/api/attachments/screenshot", body={})
        status, _, _ = call(parts, "POST", "/api/chat", body={"text": "read this", "attachments": [shot["id"]]})
        assert status == 202
        assert parts.backend.attachments_received[0][0].text == "<screen text block>"

    def test_text_files_never_need_vision(self, parts):
        parts.backend.support = {"images": {"ok": False, "reason": "no_vision"},
                                 "screenshot": {"ok": True, "reason": None}}
        _, note, _ = upload(parts, b"some text")
        assert call(parts, "POST", "/api/chat", body={"text": "summarise", "attachments": [note["id"]]})[0] == 202

    def test_a_cloud_mode_that_cannot_take_images_refuses_screenshots_too(self, parts):
        parts.backend.support = {"images": {"ok": False, "reason": "cloud_no_images"},
                                 "screenshot": {"ok": True, "reason": None}}
        parts.backend.screenshot_result = screenshot_attachment()
        _, shot, _ = call(parts, "POST", "/api/attachments/screenshot", body={})
        status, body, _ = call(parts, "POST", "/api/chat", body={"text": "look", "attachments": [shot["id"]]})
        assert (status, body["error"]) == (409, "cloud_no_images")
        _, note, _ = upload(parts, b"some text")
        assert call(parts, "POST", "/api/chat", body={"text": "summarise", "attachments": [note["id"]]})[0] == 202


class TestNothingIsKept:
    def test_content_reaches_neither_the_database_nor_any_file_nor_the_logs(self, parts, monkeypatch):
        logged = []
        for module in ("jarvis.webchat.uploads", "jarvis.webchat.hub", "jarvis.webchat.server"):
            monkeypatch.setattr(f"{module}.debug_log", lambda message, *_a, _log=logged: _log.append(message))
        _, note, _ = upload(parts, f"memo {SECRET}".encode(), headers={"X-File-Name": "C:/Users/me/secret-plans.txt"})
        call(parts, "POST", "/api/chat", body={"text": "summarise", "attachments": [note["id"]]})
        parts.hub.sync()
        parts.store.close()
        for path in parts.dir.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert SECRET.encode() not in data and b"secret-plans" not in data, path.name
        assert not any(SECRET in line or "secret-plans" in line for line in logged)
        assert logged  # the upload was logged, as a count and a size only

    def test_clearing_the_history_forgets_waiting_uploads(self, parts):
        upload(parts, b"some text")
        assert len(parts.hub.uploads) == 1
        assert call(parts, "POST", "/api/clear", body={"confirm": True})[0] == 200
        assert len(parts.hub.uploads) == 0

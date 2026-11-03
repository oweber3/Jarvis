"""The page never touches the browser's microphone: Jarvis records, the page only asks it to.

Checked on the page's source with plain file reads (no Node), and on the headers the server sends, so a
browser or the desktop window's embedded Chromium has nothing to ask permission for.
See ``webchat/webchat.spec.md``, Voice input.
"""
import re
from pathlib import Path

import pytest

from test_webchat_server import call, parts  # noqa: F401  (the fixture and request helper)

SOURCE = Path(__file__).resolve().parents[1] / "webchat-ui" / "src"

BROWSER_AUDIO = (
    "getUserMedia", "mediaDevices", "MediaRecorder", "SpeechRecognition", "webkitSpeechRecognition",
    "AudioContext", "AudioWorklet", "MediaStream", "requestPermission", "permissions.query",
)


PHONE = SOURCE / "phone"
STATIC = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "webchat" / "static"


def page_sources():
    """The web chat page's sources: everything except the phone app's own folder, which records with the
    phone's own microphone (``remote/remote.spec.md``, Voice) and is built into a separate bundle."""
    return [p for p in SOURCE.rglob("*")
            if p.suffix in {".ts", ".tsx"} and not p.name.endswith(".test.ts") and PHONE not in p.parents]


@pytest.mark.parametrize("path", page_sources(), ids=lambda p: p.name)
def test_the_page_source_uses_no_browser_microphone_or_speech_api(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    assert [word for word in BROWSER_AUDIO if word in text] == []


def test_the_page_does_not_wire_in_assistant_uis_browser_dictation():
    text = "\n".join(p.read_text(encoding="utf-8") for p in page_sources())
    assert not re.search(r"dictation\s*[:=]|WebSpeech|startDictation", text)


def test_the_page_records_only_by_asking_the_daemon():
    text = (SOURCE / "mic.ts").read_text(encoding="utf-8")
    for route in ("/api/mic/start", "/api/mic/heartbeat", "/api/mic/stop", "/api/mic/cancel"):
        assert route in text


def test_every_response_denies_the_microphone_to_the_page(parts):  # noqa: F811
    for path in ("/", "/api/state", "/api/mic"):
        _status, _body, response = call(parts, "GET", path)
        policy = response.getheader("Permissions-Policy") or ""
        assert "microphone=()" in policy and "camera=()" in policy, path


def test_nothing_shared_with_the_web_chat_imports_the_phone_app():
    importing = [p.name for p in page_sources() if re.search(r"""from\s+["'](@/phone|\./phone|\.\./phone)""", p.read_text(
        encoding="utf-8", errors="replace"))]
    assert importing == []


@pytest.mark.parametrize("path", sorted((STATIC / "assets").glob("*.js")), ids=lambda p: p.name)
def test_the_built_web_chat_has_no_browser_audio_either(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    assert [word for word in BROWSER_AUDIO if word in text] == []

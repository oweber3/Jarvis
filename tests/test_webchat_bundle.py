"""The shipped pages are offline: no CDN, no hosted service, no analytics.

Two builds come from one React project: the web chat (``webchat/static``) and the phone app
(``remote/static``). Both builds and the sources behind them are checked with plain file reads, so Python
tests never need Node. See ``webchat/webchat.spec.md`` and ``remote/remote.spec.md``.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_STATIC = ROOT / "src" / "jarvis" / "webchat" / "static"
PHONE_STATIC = ROOT / "src" / "jarvis" / "remote" / "static"
SOURCE = ROOT / "webchat-ui" / "src"
UI = ROOT / "webchat-ui"
APPS = {"web": WEB_STATIC, "phone": PHONE_STATIC}

URL = re.compile(r"https?://[^\s\"'`)<>\\]+")

# Strings a UI library carries that are never fetched: XML namespace names, error-message help links and a
# licence banner comment.
INERT_URLS = (
    "http://www.w3.org/",
    "https://react.dev/errors/",
    "https://github.com/syntax-tree/",
    "https://tailwindcss.com",
)

HOSTED_OR_TRACKING = (
    "assistant-cloud", "api.assistant-ui", "cloud.assistant", "posthog", "sentry", "google-analytics",
    "googletagmanager", "googleapis", "gstatic", "jsdelivr", "unpkg", "cdnjs", "mixpanel", "segment.io",
    "sendBeacon", "EventSource", "WebSocket", "telemetry",
)

# Files a page may name besides its own assets: the public files the phone build copies to its root.
PHONE_PUBLIC = {"/manifest.webmanifest", "/icon.svg", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"}


def built_files():
    return [p for static in APPS.values() for p in static.rglob("*") if p.is_file() and p.suffix != ".png"]


def source_files():
    """The page's own sources. Tests (``*.test.ts(x)`` and ``src/test/``) are left out: they never reach a build,
    and a test of how a link in a reply is drawn has to name one; the builds themselves are checked above."""
    tests = SOURCE / "test"
    sources = [p for p in SOURCE.rglob("*") if p.suffix in {".ts", ".tsx", ".css"}
               and ".test." not in p.name and tests not in p.parents]
    return sources + [UI / "phone.html", UI / "public-phone" / "pcm-worklet.js", UI / "public-phone" / "manifest.webmanifest",
                      UI / "public-phone" / "icon.svg"]


@pytest.mark.parametrize("app", APPS)
def test_the_build_is_committed_with_a_page_and_its_assets(app):
    static = APPS[app]
    assert (static / "index.html").is_file()
    assets = list((static / "assets").glob("*"))
    assert any(p.suffix == ".js" for p in assets) and any(p.suffix == ".css" for p in assets)


@pytest.mark.parametrize("app", APPS)
def test_the_page_loads_only_its_own_files(app):
    static = APPS[app]
    html = (static / "index.html").read_text(encoding="utf-8")
    assert not URL.findall(html)
    for reference in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert reference.startswith("/assets/") or reference in PHONE_PUBLIC, reference
        assert (static / reference.lstrip("/")).is_file(), reference


@pytest.mark.parametrize("path", built_files(), ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_the_build_names_no_outside_host(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    outside = [u for u in URL.findall(text) if not u.startswith(INERT_URLS)]
    assert outside == []


@pytest.mark.parametrize("path", built_files(), ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_the_build_has_no_hosted_service_or_tracking(path):
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    assert [w for w in HOSTED_OR_TRACKING if w.lower() in text] == []


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_the_source_names_no_url_and_no_hosted_service(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    assert [u for u in URL.findall(text) if not u.startswith(INERT_URLS)] == [], path.name
    assert re.search(r"assistant-cloud|@assistant-ui/cloud|AssistantCloud", text) is None, path.name
    assert [w for w in ("sendBeacon", "EventSource", "WebSocket", "telemetry") if w in text] == [], path.name


def test_the_theme_stylesheet_imports_nothing_remote():
    for sheet in (SOURCE / "index.css", SOURCE / "phone" / "phone.css"):
        for line in re.findall(r"@import\s+[^;]+;", sheet.read_text(encoding="utf-8")):
            assert "http" not in line and "//" not in line, line


def test_the_desktop_build_ships_both_pages():
    spec = (ROOT / "jarvis_desktop.spec").read_text(encoding="utf-8")
    assert "'webchat' / 'static'" in spec and "'jarvis/webchat/static'" in spec
    assert "'remote' / 'static'" in spec and "'jarvis/remote/static'" in spec


def test_the_servers_find_the_pages_where_the_builds_put_them():
    from jarvis.remote.server import STATIC_DIR as phone_dir
    from jarvis.webchat.server import STATIC_DIR as web_dir
    assert web_dir == WEB_STATIC and phone_dir == PHONE_STATIC


# -- the phone build in particular ---------------------------------------------------------------------------


def test_the_old_hand_written_phone_page_is_gone():
    assert not [p.name for p in PHONE_STATIC.iterdir() if p.name in {"app.js", "app.css"}]


def test_the_phone_page_can_be_added_to_a_home_screen():
    html = (PHONE_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in html and 'rel="apple-touch-icon"' in html and 'name="theme-color"' in html
    assert "viewport-fit=cover" in html
    manifest = json.loads((PHONE_STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for icon in manifest["icons"]:
        assert (PHONE_STATIC / icon["src"].lstrip("/")).is_file(), icon["src"]


@pytest.mark.parametrize("name,size", [("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)])
def test_the_home_screen_icons_are_pngs_of_the_right_size(name, size):
    data = (PHONE_STATIC / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(data[16:20], "big") == size and int.from_bytes(data[20:24], "big") == size


def test_the_audio_worklet_is_served_as_a_static_file_and_registers_its_processor():
    from jarvis.remote.server import STATIC_FILES
    assert STATIC_FILES["/pcm-worklet.js"][0] == "pcm-worklet.js"
    text = (PHONE_STATIC / "pcm-worklet.js").read_text(encoding="utf-8")
    assert 'registerProcessor("pcm-capture"' in text
    assert (UI / "src" / "phone" / "micCapture.ts").read_text(encoding="utf-8").count('"pcm-capture"') >= 1


def test_every_static_file_the_server_names_exists_in_the_build():
    from jarvis.remote.server import STATIC_FILES
    for path, (name, _type) in STATIC_FILES.items():
        assert (PHONE_STATIC / name).is_file(), path


def test_the_phone_build_records_with_the_phones_own_microphone_and_the_web_chat_does_not():
    phone = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (PHONE_STATIC / "assets").glob("*.js"))
    web = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (WEB_STATIC / "assets").glob("*.js"))
    assert "getUserMedia" in phone and "AudioWorkletNode" in phone
    assert "getUserMedia" not in web and "AudioWorkletNode" not in web


def test_the_phone_build_uses_no_browser_speech_service():
    phone = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (PHONE_STATIC / "assets").glob("*.js"))
    for word in ("SpeechRecognition", "webkitSpeechRecognition", "speechSynthesis", "SpeechSynthesisUtterance",
                 "MediaRecorder"):
        assert word not in phone, word


def test_the_phone_sources_use_no_browser_speech_service():
    for path in (SOURCE / "phone").rglob("*.ts*"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in ("SpeechRecognition", "speechSynthesis", "SpeechSynthesisUtterance", "MediaRecorder"):
            assert word not in text, f"{word} in {path.name}"


def test_the_phone_app_sends_audio_only_to_its_own_server():
    text = (SOURCE / "phone" / "phoneApi.ts").read_text(encoding="utf-8")
    assert '"/api/voice"' in text and "postBytes" in text
    assert not URL.findall(text)


def test_the_phone_app_keeps_its_token_only_in_the_browsers_storage():
    text = (SOURCE / "phone" / "storage.ts").read_text(encoding="utf-8")
    assert "localStorage" in text
    assert "document.cookie" not in "\n".join(p.read_text(encoding="utf-8", errors="replace")
                                              for p in (SOURCE / "phone").rglob("*.ts*"))

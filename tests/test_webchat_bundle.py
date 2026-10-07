"""The shipped web chat page is offline: no CDN, no hosted service, no analytics.

Checks the committed build and the copied assistant-ui source with plain file reads, so Python tests
never need Node. See ``webchat/webchat.spec.md``.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "jarvis" / "webchat" / "static"
SOURCE = ROOT / "webchat-ui" / "src"

URL = re.compile(r"https?://[^\s\"'`)<>\\]+")

# Strings a UI library carries that are never fetched: XML namespace names and error-message help links.
INERT_URLS = (
    "http://www.w3.org/",
    "https://react.dev/errors/",
    "https://github.com/syntax-tree/",
)

HOSTED_OR_TRACKING = (
    "assistant-cloud", "api.assistant-ui", "cloud.assistant", "posthog", "sentry", "google-analytics",
    "googletagmanager", "googleapis", "gstatic", "jsdelivr", "unpkg", "cdnjs", "mixpanel", "segment.io",
    "sendBeacon", "EventSource", "WebSocket", "telemetry",
)


def built_files():
    return [p for p in STATIC.rglob("*") if p.is_file()]


def source_files():
    return [p for p in SOURCE.rglob("*") if p.suffix in {".ts", ".tsx", ".css"}]


def test_the_build_is_committed_with_a_page_and_its_assets():
    assert (STATIC / "index.html").is_file()
    assets = [p for p in (STATIC / "assets").glob("*")]
    assert any(p.suffix == ".js" for p in assets) and any(p.suffix == ".css" for p in assets)


def test_the_page_loads_only_its_own_files():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not URL.findall(html)
    for reference in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert reference.startswith("/assets/"), reference
        assert (STATIC / reference.lstrip("/")).is_file(), reference


@pytest.mark.parametrize("path", built_files(), ids=lambda p: p.name)
def test_the_build_names_no_outside_host(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    outside = [u for u in URL.findall(text) if not u.startswith(INERT_URLS)]
    assert outside == []


@pytest.mark.parametrize("path", built_files(), ids=lambda p: p.name)
def test_the_build_has_no_hosted_service_or_tracking(path):
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    assert [w for w in HOSTED_OR_TRACKING if w.lower() in text] == []


def test_the_copied_source_names_no_url_and_no_hosted_service():
    for path in source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert URL.findall(text) == [], path.name
        assert re.search(r"assistant-cloud|@assistant-ui/cloud|AssistantCloud", text) is None, path.name


def test_the_theme_stylesheet_imports_nothing_remote():
    css = (SOURCE / "index.css").read_text(encoding="utf-8")
    for line in re.findall(r"@import\s+[^;]+;", css):
        assert "http" not in line and "//" not in line, line

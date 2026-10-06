"""The phone web app: self-contained, and wearing the orb's own palette."""
import re
from pathlib import Path

import pytest

from desktop_app.themes import ORB_PALETTE

STATIC = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "remote" / "static"


def css_palette():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    return {name.replace("-", "_"): value.lower()
            for name, value in re.findall(r"--orb-([a-z-]+):\s*(#[0-9a-fA-F]{6})", css)}


@pytest.mark.unit
class TestStaticApp:
    def test_the_app_uses_the_orb_palette(self):
        assert css_palette() == {k: v.lower() for k, v in ORB_PALETTE.items()}

    def test_nothing_is_loaded_from_another_host(self):
        for path in STATIC.iterdir():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", text), path.name
            assert "@import" not in text and "fonts.googleapis" not in text, path.name
            assert not re.search(r"fetch\(\s*[\"']https?://", text), path.name

    def test_the_orb_knows_every_assistant_state(self):
        from jarvis.assistant_state import AssistantState
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        for state in AssistantState:
            assert f'"{state.value}"' in js or f"{state.value}:" in js, state.value

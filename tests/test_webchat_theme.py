"""The web chat page wears the desktop HUD palette: one source of colour (``desktop_app.themes``).

The page's palette is generated from ``HUD_COLORS`` into ``webchat-ui/src/generated/theme.css`` (committed),
so it needs no Node to check and cannot drift from the windows it sits beside.
"""
import re
from pathlib import Path

import pytest

from desktop_app import web_chat_theme
from desktop_app.themes import FONT_MONO, FONT_UI, HUD_COLORS

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "webchat-ui" / "src" / "generated" / "theme.css"


def declared(css: str) -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", css))


def test_every_hud_colour_becomes_a_variable_with_the_same_value():
    variables = declared(web_chat_theme.theme_css())
    for key, value in HUD_COLORS.items():
        assert variables[f"hud-{key.replace('_', '-')}"] == value, key


def test_the_page_uses_the_desktop_fonts():
    variables = declared(web_chat_theme.theme_css())
    assert variables["hud-font-ui"] == FONT_UI and variables["hud-font-mono"] == FONT_MONO


def test_the_committed_file_is_current():
    assert GENERATED.read_text(encoding="utf-8").replace("\r\n", "\n") == web_chat_theme.theme_css(), (
        "run: python -m desktop_app.web_chat_theme")


def test_the_generator_writes_the_file(tmp_path):
    target = tmp_path / "theme.css"
    assert web_chat_theme.write(target) == target
    assert target.read_text(encoding="utf-8") == web_chat_theme.theme_css()


@pytest.mark.parametrize("fragment", ["http://", "https://", "url(", "@import"])
def test_the_palette_loads_nothing(fragment):
    assert fragment not in web_chat_theme.theme_css()

"""The memory viewer page wears the same HUD palette as the Qt windows and makes no external request."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def page():
    from desktop_app import memory_viewer
    response = memory_viewer.app.test_client().get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _css_variables(page: str) -> dict[str, str]:
    root = re.search(r":root\s*\{(.*?)\n\s*\}", page, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", root))


def test_css_variables_are_generated_from_the_palette(page):
    from desktop_app.themes import HUD_COLORS
    variables = _css_variables(page)
    for css_name, key in {
        "bg-primary": "bg_primary", "bg-secondary": "bg_secondary", "bg-tertiary": "bg_tertiary",
        "bg-card": "bg_card", "bg-hover": "bg_hover", "accent-primary": "accent_primary",
        "accent-secondary": "accent_secondary", "accent-glow": "accent_glow",
        "accent-muted": "accent_muted", "text-primary": "text_primary",
        "text-secondary": "text_secondary", "text-muted": "text_muted",
        "border-color": "border", "border-glow": "border_glow", "success": "success",
        "warning": "warning", "error": "error",
    }.items():
        assert variables[css_name] == HUD_COLORS[key], css_name


def test_no_amber_remains_in_the_page(page):
    assert "245, 158, 11" not in page
    for retired in ("#92400e", "#d97706", "#fef3c7", "139, 92, 246"):
        assert retired not in page


def test_graph_canvas_colours_follow_the_css_variables(page):
    # The graph is drawn on a canvas, so it must read its colours from the palette variables.
    assert "getComputedStyle(document.documentElement)" in page


def test_page_makes_no_external_request(page):
    assert "fonts.googleapis.com" not in page
    assert "fonts.gstatic.com" not in page
    assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", page)
    assert "@import" not in page


def test_fonts_are_system_or_bundled_stacks(page):
    families = re.findall(r"font-family:\s*([^;]+);", page)
    assert families
    assert not any("Outfit" in family for family in families)
    assert any("Segoe UI" in family for family in families)
    assert any("monospace" in family for family in families)


def test_icons_are_the_shared_line_icons(page):
    from desktop_app.themes import LINE_ICONS
    symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', page))
    assert symbols == set(LINE_ICONS)
    used = set(re.findall(r'href="#i-([\w-]+)"', page))
    assert used and used <= symbols


def test_icons_draw_in_the_colour_of_their_text(page):
    sprite = re.search(r'<svg[^>]*style="display: none">(.*?)</svg>', page, re.S).group(1)
    assert 'stroke="currentColor"' in sprite
    assert not re.search(r"#[0-9a-fA-F]{6}", sprite)

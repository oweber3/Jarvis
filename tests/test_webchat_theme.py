"""The web chat page wears the desktop HUD palette, dark and light: one source of colour (``desktop_app.themes``).

The page's palettes are generated from ``HUD_COLORS`` and ``HUD_COLORS_LIGHT`` into
``webchat-ui/src/generated/theme.css`` (committed), so they need no Node to check and cannot drift from
the windows they sit beside. Both must keep text readable (WCAG AA, 4.5:1).
"""
import re
from pathlib import Path

import pytest

from desktop_app import web_chat_theme
from desktop_app.themes import FONT_MONO, FONT_UI, HUD_COLORS, HUD_COLORS_LIGHT

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "webchat-ui" / "src" / "generated" / "theme.css"
GENERATED_ORB = ROOT / "webchat-ui" / "src" / "generated" / "orbPalette.ts"


def block(css: str, selector: str) -> dict:
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, selector
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", found.group(1)))


def luminance(colour: str) -> float:
    red, green, blue = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    channel = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(first: str, second: str) -> float:
    a, b = sorted((luminance(first), luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


PALETTES = {"dark": HUD_COLORS, "light": HUD_COLORS_LIGHT}
SELECTORS = {"dark": ":root", "light": ':root[data-theme="light"]'}


@pytest.mark.parametrize("theme", PALETTES)
def test_every_colour_becomes_a_variable_with_the_same_value(theme):
    variables = block(web_chat_theme.theme_css(), SELECTORS[theme])
    for key, value in PALETTES[theme].items():
        assert variables[f"hud-{key.replace('_', '-')}"] == value, key


def test_the_light_palette_has_exactly_the_keys_of_the_dark_one():
    assert set(HUD_COLORS_LIGHT) == set(HUD_COLORS)


def test_the_page_uses_the_desktop_fonts():
    variables = block(web_chat_theme.theme_css(), ":root")
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


SURFACES = ("bg_primary", "bg_card", "bg_tertiary", "bg_hover", "bg_secondary")


@pytest.mark.parametrize("theme", PALETTES)
@pytest.mark.parametrize("text", ["text_primary", "text_secondary", "text_muted", "accent_primary"])
@pytest.mark.parametrize("surface", SURFACES)
def test_text_and_accents_are_readable_on_every_surface(theme, text, surface):
    palette = PALETTES[theme]
    assert contrast(palette[text], palette[surface]) >= 4.5, (theme, text, surface)


@pytest.mark.parametrize("theme", PALETTES)
@pytest.mark.parametrize("status", ["success", "warning", "error"])
def test_status_colours_are_readable_on_the_page(theme, status):
    palette = PALETTES[theme]
    assert contrast(palette[status], palette["bg_primary"]) >= 4.5, (theme, status)


@pytest.mark.parametrize("theme", PALETTES)
def test_the_send_button_and_the_owners_bubble_are_readable(theme):
    palette = PALETTES[theme]
    ink = web_chat_theme.INK_ON_ACCENT[theme]
    for accent in ("accent_primary", "accent_secondary", "accent_deep"):
        assert contrast(ink if ink.startswith("#") else palette[ink], palette[accent]) >= 4.5, (theme, accent)


# -- the orb's colours and looks, for the phone's canvas orb ------------------------------------------


def orb_constants() -> dict:
    """The generated TypeScript as {name: value}: ``ORB_PALETTE`` and each state's look."""
    import json
    text = web_chat_theme.orb_ts()
    palette = re.search(r"export const ORB_PALETTE = (\{.*?\}) as const", text, re.S)
    looks = re.search(r"export const ORB_LOOKS = (\{.*?\}) as const", text, re.S)
    assert palette and looks
    return {"palette": json.loads(palette.group(1)), "looks": json.loads(looks.group(1))}


def test_the_orb_palette_is_generated_with_every_colour_unchanged():
    from desktop_app.themes import ORB_PALETTE
    assert orb_constants()["palette"] == dict(ORB_PALETTE)


def test_every_orb_state_look_is_generated_from_the_desktop_orb():
    from desktop_app.orb_widget import _LOOKS
    looks = orb_constants()["looks"]
    assert set(looks) == {state.value for state in _LOOKS}
    for state, look in _LOOKS.items():
        generated = looks[state.value]
        assert generated == {"glow": look.glow, "spin": look.spin, "think": look.think, "voiceGain": look.voice_gain,
                             "stir": look.stir, "colour": look.colour, "accent": look.accent, "label": look.label,
                             "swell": look.swell}, state


def test_the_committed_orb_file_is_current():
    committed = GENERATED_ORB.read_text(encoding="utf-8")
    assert committed.replace(chr(13) + chr(10), chr(10)) == web_chat_theme.orb_ts(), (
        "run: python -m desktop_app.web_chat_theme")


def test_the_generator_writes_both_files(tmp_path):
    written = web_chat_theme.write(tmp_path / "theme.css", orb_target=tmp_path / "orbPalette.ts")
    assert written == tmp_path / "theme.css"
    assert (tmp_path / "orbPalette.ts").read_text(encoding="utf-8") == web_chat_theme.orb_ts()

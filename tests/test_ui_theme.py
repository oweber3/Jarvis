"""Behaviour tests for the single orb HUD theme every desktop window wears.

The HUD palette (``HUD_COLORS``, derived from the orb's ``ORB_PALETTE``) is the
only palette. See ``desktop_app.spec.md`` (Theme System). Per-window rendering
checks live in ``test_ui_windows_theme.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parents[1] / "src" / "desktop_app"
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{6}\b|\brgba?\s*\(")

# Surfaces text can sit on, and the text colours that must stay legible on all of them.
SURFACES = ("bg_primary", "bg_secondary", "bg_tertiary", "bg_card", "bg_hover")
TEXT_KEYS = ("text_primary", "text_secondary", "text_muted", "accent_secondary",
             "success_light", "warning_light", "error_light")

def _retired_amber_accents():
    """Amber values of the retired accent palette, less the ones the warning status still uses.

    Amber appears only as the warning status colour (badges and notices that warn); never as chrome.
    """
    from desktop_app.themes import HUD_COLORS
    warning = {HUD_COLORS[key] for key in ("warning", "warning_light", "warning_glow", "warning_border")}
    retired = ("#92400e", "#d97706", "rgba(245, 158, 11, 0.15)",
               "rgba(245, 158, 11, 0.3)", "rgba(245, 158, 11, 0.25)")
    return tuple(value for value in retired if value not in warning)


RETIRED_AMBER_ACCENTS = _retired_amber_accents()


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _contrast_rgb(a, b) -> float:
    return _contrast("#%02x%02x%02x" % tuple(a[:3]), "#%02x%02x%02x" % tuple(b[:3]))


def _load_icon_module():
    import importlib.util
    path = SRC / "desktop_assets" / "generate_icons.py"
    spec = importlib.util.spec_from_file_location("generate_icons_theme_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
class TestOnePaletteSource:
    def test_the_amber_palette_and_its_stylesheets_are_gone(self):
        from desktop_app import themes
        for name in ("COLORS", "JARVIS_THEME_STYLESHEET", "WIZARD_STYLESHEET"):
            assert not hasattr(themes, name), name

    def test_status_colours_come_from_the_orb_palette(self):
        from desktop_app.themes import HUD_COLORS, ORB_PALETTE
        assert HUD_COLORS["success"] == ORB_PALETTE["green"]
        assert HUD_COLORS["success_light"] == ORB_PALETTE["green_light"]
        assert HUD_COLORS["warning"] == ORB_PALETTE["amber"]
        assert HUD_COLORS["warning_light"] == ORB_PALETTE["amber_light"]
        assert HUD_COLORS["error"] == ORB_PALETTE["red"]
        assert HUD_COLORS["error_light"] == ORB_PALETTE["red_light"]

    def test_orb_looks_only_use_orb_palette_colours(self):
        from desktop_app.orb_widget import OrbState, _LOOKS
        from desktop_app.themes import ORB_PALETTE
        palette_values = set(ORB_PALETTE.values())
        for state in OrbState:
            assert _LOOKS[state].colour in palette_values, state
            assert _LOOKS[state].accent in palette_values, state
        assert _LOOKS[OrbState.ERROR].colour == ORB_PALETTE["red"]
        assert _LOOKS[OrbState.DICTATING].colour != _LOOKS[OrbState.LISTENING].colour

    def test_every_window_module_takes_colours_from_the_palette(self):
        offenders = {}
        for path in sorted(SRC.glob("*.py")):
            if path.name in ("themes.py", "memory_viewer.py"):
                continue
            hits = COLOUR_LITERAL.findall(path.read_text(encoding="utf-8"))
            if hits:
                offenders[path.name] = hits[:3]
        assert offenders == {}

    def test_tray_icon_generator_takes_colours_from_the_palette(self):
        script = (SRC / "desktop_assets" / "generate_icons.py").read_text(encoding="utf-8")
        assert COLOUR_LITERAL.findall(script) == []


@pytest.mark.unit
class TestThemeStylesheet:
    def test_stylesheet_is_the_hud_palette_with_no_unfilled_tokens(self):
        from desktop_app.themes import HUD_COLORS, HUD_THEME_STYLESHEET
        assert "$" not in HUD_THEME_STYLESHEET
        assert HUD_COLORS["accent_primary"] in HUD_THEME_STYLESHEET
        assert HUD_COLORS["bg_primary"] in HUD_THEME_STYLESHEET
        for amber in RETIRED_AMBER_ACCENTS:
            assert amber not in HUD_THEME_STYLESHEET

    def test_apply_theme_applies_the_hud_palette_and_indicator_icons(self, qapp):
        from PyQt6.QtWidgets import QWidget
        from desktop_app.themes import HUD_COLORS, apply_theme
        widget = QWidget()
        apply_theme(widget)
        sheet = widget.styleSheet()
        assert HUD_COLORS["accent_primary"] in sheet
        assert "url(" in sheet
        for amber in RETIRED_AMBER_ACCENTS:
            assert amber not in sheet

    def test_indicator_icons_are_drawn_from_the_palette(self):
        from desktop_app import themes
        icons = themes._ensure_icons()
        check = Path(icons["check"]).read_text(encoding="utf-8")
        arrow = Path(icons["arrow_up"]).read_text(encoding="utf-8")
        assert themes.HUD_COLORS["bg_primary"] in check
        assert themes.HUD_COLORS["text_muted"] in arrow

    def test_wizard_rules_are_part_of_the_one_stylesheet(self):
        from desktop_app.themes import HUD_THEME_STYLESHEET
        assert "QPushButton#setupNext" in HUD_THEME_STYLESHEET
        assert "QLabel#setupStage" in HUD_THEME_STYLESHEET


@pytest.mark.unit
class TestPaletteLegibility:
    @pytest.mark.parametrize("surface", SURFACES)
    @pytest.mark.parametrize("text", TEXT_KEYS)
    def test_text_meets_wcag_aa_on_every_surface(self, text, surface):
        from desktop_app.themes import HUD_COLORS
        assert _contrast(HUD_COLORS[text], HUD_COLORS[surface]) >= 4.5

    @pytest.mark.parametrize("foreground, background", [
        ("bg_primary", "accent_primary"), ("bg_primary", "success"),
        ("bg_primary", "error"), ("bg_primary", "warning"),
    ])
    def test_text_on_filled_buttons_meets_wcag_aa(self, foreground, background):
        from desktop_app.themes import HUD_COLORS
        assert _contrast(HUD_COLORS[foreground], HUD_COLORS[background]) >= 4.5


@pytest.mark.unit
class TestTrayIcons:
    STATES = ("idle", "listening", "thinking")

    def test_generator_defines_a_distinct_orb_colour_per_state(self):
        from desktop_app.themes import ORB_PALETTE
        mod = _load_icon_module()
        assert set(mod.ICON_STATES) == set(self.STATES)
        colours = [mod.ICON_STATES[s] for s in self.STATES]
        assert len(set(colours)) == len(colours)
        assert all(c in ORB_PALETTE.values() for c in colours)

    @pytest.mark.parametrize("state", STATES)
    def test_icon_reads_as_its_orb_colour_at_16px(self, state, tmp_path):
        from PIL import Image
        mod = _load_icon_module()
        mod.create_icon(mod.ICON_STATES[state], str(tmp_path / f"icon_{state}.png"))
        img = Image.open(tmp_path / f"icon_{state}_16.png").convert("RGBA")
        assert img.size == (16, 16)
        ring_pixel = img.getpixel((2, 8))     # on the bright outer shell
        assert ring_pixel[3] >= 250            # opaque, give or take resampling
        hexcode = mod.ICON_STATES[state].lstrip("#")
        want = tuple(int(hexcode[i:i + 2], 16) for i in (0, 2, 4))
        assert max(abs(c - w) for c, w in zip(ring_pixel[:3], want)) < 70
        # Legible at 16 px: the bright rim stands out from the dimmer layers inside it.
        assert _contrast_rgb(img.getpixel((8, 8)), ring_pixel) > 1.5

    @pytest.mark.parametrize("state", STATES)
    def test_no_core_the_layers_fill_the_disc_and_the_rim_is_brightest(self, state, tmp_path):
        from PIL import Image
        mod = _load_icon_module()
        mod.create_icon(mod.ICON_STATES[state], str(tmp_path / f"icon_{state}.png"))
        img = Image.open(tmp_path / f"icon_{state}.png").convert("RGBA")
        size = img.size[0]
        c = size // 2

        def band(r0, r1):
            values = [sum(img.getpixel((x, y))[:3]) for x in range(0, size, 2) for y in range(0, size, 2)
                      if r0 * size <= ((x - c) ** 2 + (y - c) ** 2) ** 0.5 < r1 * size]
            return sum(values) / len(values)

        rim, inner, centre = band(0.39, 0.45), band(0.12, 0.3), band(0.0, 0.05)
        assert rim > inner > 40            # inner layers fill the disc, dimmer than the rim
        assert centre < rim                # and there is no bright core
        # Outside the emblem the icon is transparent, inside it sits on a dark disc.
        assert img.getpixel((1, 1))[3] == 0
        assert img.getpixel((c, c))[3] == 255

    def test_committed_icons_match_the_generator(self, tmp_path):
        mod = _load_icon_module()
        for state in self.STATES:
            fresh = tmp_path / f"icon_{state}.png"
            mod.create_icon(mod.ICON_STATES[state], str(fresh))
            committed = SRC / "desktop_assets" / f"icon_{state}.png"
            assert committed.read_bytes() == fresh.read_bytes(), state

    def test_tray_shows_the_thinking_icon_while_jarvis_thinks(self):
        from desktop_app.app import tray_icon_name
        assert tray_icon_name(listening=False, orb_state="idle") == "icon_idle.png"
        assert tray_icon_name(listening=True, orb_state="idle") == "icon_listening.png"
        assert tray_icon_name(listening=True, orb_state="thinking") == "icon_thinking.png"
        assert tray_icon_name(listening=False, orb_state="thinking") == "icon_idle.png"


def _calls(path, name):
    """Every call to ``name`` (a function or a method) in a module, as AST nodes."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called == name:
                yield node


@pytest.mark.unit
class TestStylingGoesThroughTheTheme:
    """Window modules name roles; every rule, colour and icon comes from ``themes.py``."""

    def test_window_modules_only_apply_stylesheets_from_the_theme(self):
        import ast
        from desktop_app import themes
        theme_sheets = {name for name in dir(themes) if name.endswith("_STYLESHEET")}
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            if path.name == "themes.py":
                continue
            for call in _calls(path, "setStyleSheet"):
                arg = call.args[0] if call.args else None
                from_theme = (isinstance(arg, ast.Name) and arg.id in theme_sheets) or (
                    isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "themed_stylesheet")
                if not from_theme:
                    offenders.append(f"{path.name}:{call.lineno}")
        assert offenders == []

    def test_every_role_a_window_sets_is_styled(self):
        import ast
        from desktop_app.themes import CHAT_THEME_STYLESHEET, HUD_THEME_STYLESHEET
        sheets = HUD_THEME_STYLESHEET + CHAT_THEME_STYLESHEET
        missing = []
        for path in sorted(SRC.glob("*.py")):
            for call in _calls(path, "set_role"):
                role = call.args[1]
                if isinstance(role, ast.Constant) and f"#{role.value}" not in sheets:
                    missing.append(f"{path.name}:{call.lineno} {role.value}")
        assert missing == []

    def test_set_role_restyles_a_label_at_once(self, qapp):
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QLabel
        from desktop_app.themes import HUD_COLORS, apply_theme, set_role
        label = QLabel("Saved")
        apply_theme(label)
        label.ensurePolished()
        set_role(label, "status-success")
        assert label.palette().windowText().color().name() == QColor(HUD_COLORS["success_light"]).name()
        set_role(label, "status-error")
        assert label.palette().windowText().color().name() == QColor(HUD_COLORS["error_light"]).name()

    def test_hud_heading_names_the_section_and_hides_an_empty_subtitle(self, qapp):
        from PyQt6.QtGui import QFont
        from desktop_app.themes import hud_heading
        widget, title, subtitle = hud_heading("Settings", "Settings")
        eyebrow = widget.findChild(type(title), "eyebrow")
        assert "Settings" in eyebrow.text() and "Jarvis" in eyebrow.text()
        assert eyebrow.font().capitalization() == QFont.Capitalization.AllUppercase
        assert title.objectName() == "title" and title.text() == "Settings"
        assert subtitle.isHidden()


@pytest.mark.unit
class TestLineIcons:
    """Icons on screen are line drawings in palette colours, never emoji."""

    @pytest.mark.parametrize("name", sorted(__import__("desktop_app.themes", fromlist=["LINE_ICONS"]).LINE_ICONS))
    def test_every_line_icon_renders(self, qapp, name):
        from desktop_app.themes import line_icon
        icon = line_icon(name)
        assert not icon.isNull()
        assert not icon.pixmap(16, 16).isNull()

    def test_icons_are_stroked_in_palette_colours(self):
        from desktop_app.themes import HUD_COLORS, line_icon_svg
        svg = line_icon_svg("chat", HUD_COLORS["accent_secondary"])
        assert f'stroke="{HUD_COLORS["accent_secondary"]}"' in svg
        assert COLOUR_LITERAL.findall(svg.replace(HUD_COLORS["accent_secondary"], "")) == []

    def test_an_icon_lights_up_when_its_menu_item_is_active(self, qapp):
        from PyQt6.QtGui import QColor, QIcon
        from desktop_app.themes import HUD_COLORS, line_icon

        def dominant(mode):
            image = line_icon("logs").pixmap(32, 32, mode).toImage()
            colours = [image.pixelColor(x, y) for x in range(32) for y in range(32)]
            strokes = [c for c in colours if c.alpha() > 200]
            return strokes[len(strokes) // 2].name()

        assert dominant(QIcon.Mode.Normal) == QColor(HUD_COLORS["text_secondary"]).name()
        assert dominant(QIcon.Mode.Active) == QColor(HUD_COLORS["accent_secondary"]).name()

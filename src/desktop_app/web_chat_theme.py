"""The web chat page's palettes, generated from the desktop HUD theme.

``python -m desktop_app.web_chat_theme`` rewrites ``webchat-ui/src/generated/theme.css``. The page's
styles map assistant-ui's colour tokens onto these ``--hud-*`` variables, so the chat and the Qt windows
share one source of colour (``themes.py``): the dark palette is ``:root`` and the light one is
``:root[data-theme="light"]``. See ``jarvis/webchat/webchat.spec.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from desktop_app.themes import FONT_MONO, FONT_UI, HUD_COLORS, HUD_COLORS_LIGHT

GENERATED = Path(__file__).resolve().parents[2] / "webchat-ui" / "src" / "generated" / "theme.css"

# The text colour on the accent fills (the send button, the owner's bubble): a palette key, or a colour.
INK_ON_ACCENT = {"dark": "bg_primary", "light": "#ffffff"}


def _block(selector: str, palette: Dict[str, str], ink: str, fonts: bool) -> list:
    lines = [f"{selector} {{"]
    lines += [f"  --hud-{key.replace('_', '-')}: {value};" for key, value in palette.items()]
    lines.append(f"  --hud-ink-on-accent: {ink if ink.startswith('#') else palette[ink]};")
    if fonts:
        lines += [f"  --hud-font-ui: {FONT_UI};", f"  --hud-font-mono: {FONT_MONO};"]
    lines.append("}")
    return lines


def theme_css() -> str:
    """``:root`` custom properties for the dark palette and the fonts, then the light palette's overrides."""
    lines = ["/* Generated from desktop_app/themes.py by `python -m desktop_app.web_chat_theme`. Do not edit. */"]
    lines += _block(":root", HUD_COLORS, INK_ON_ACCENT["dark"], fonts=True)
    lines += _block(':root[data-theme="light"]', HUD_COLORS_LIGHT, INK_ON_ACCENT["light"], fonts=False)
    return "\n".join(lines) + "\n"


def write(target: Path = GENERATED) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(theme_css(), encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # the console may not be UTF-8 (Windows code pages)
    print(f"🎨 Wrote {write()}")

"""The web chat page's palettes, generated from the desktop HUD theme.

``python -m desktop_app.web_chat_theme`` rewrites ``webchat-ui/src/generated/theme.css``. The page's
styles map assistant-ui's colour tokens onto these ``--hud-*`` variables, so the chat and the Qt windows
share one source of colour (``themes.py``): the dark palette is ``:root`` and the light one is
``:root[data-theme="light"]``. See ``jarvis/webchat/webchat.spec.md``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from desktop_app.themes import FONT_MONO, FONT_UI, HUD_COLORS, HUD_COLORS_LIGHT, ORB_PALETTE

GENERATED = Path(__file__).resolve().parents[2] / "webchat-ui" / "src" / "generated" / "theme.css"
GENERATED_ORB = GENERATED.with_name("orbPalette.ts")

# The text colour on the accent fills (the send button, the owner's bubble): a key of that palette.
INK_ON_ACCENT = {"dark": "bg_primary", "light": "bg_secondary"}


def _block(selector: str, palette: Dict[str, str], ink: str, fonts: bool) -> list:
    lines = [f"{selector} {{"]
    lines += [f"  --hud-{key.replace('_', '-')}: {value};" for key, value in palette.items()]
    lines.append(f"  --hud-ink-on-accent: {palette[ink]};")
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


def orb_ts() -> str:
    """The orb's palette and each state's look as TypeScript constants, for the phone's canvas orb.

    The looks come from the desktop orb's own table, so the phone's states cannot drift from the PC's."""
    from desktop_app.orb_widget import _LOOKS

    looks = {state.value: {"glow": look.glow, "spin": look.spin, "think": look.think, "voiceGain": look.voice_gain,
                           "stir": look.stir, "colour": look.colour, "accent": look.accent, "label": look.label,
                           "swell": look.swell}
             for state, look in _LOOKS.items()}
    return ("// Generated from desktop_app/themes.py and desktop_app/orb_widget.py by "
            "`python -m desktop_app.web_chat_theme`. Do not edit.\n"
            f"export const ORB_PALETTE = {json.dumps(dict(ORB_PALETTE), indent=2)} as const\n\n"
            f"export const ORB_LOOKS = {json.dumps(looks, indent=2)} as const\n")


def write(target: Path = GENERATED, orb_target: Path = GENERATED_ORB) -> Path:
    """Write the theme stylesheet and the orb constants; returns the stylesheet's path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(theme_css(), encoding="utf-8", newline="\n")
    orb_target.parent.mkdir(parents=True, exist_ok=True)
    orb_target.write_text(orb_ts(), encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # the console may not be UTF-8 (Windows code pages)
    print(f"🎨 Wrote {write()}")
    print(f"   🔮 Wrote {GENERATED_ORB}")

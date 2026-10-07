"""The web chat page's palette, generated from the desktop HUD theme.

``python -m desktop_app.web_chat_theme`` rewrites ``webchat-ui/src/generated/theme.css``. The page's
styles map assistant-ui's colour tokens onto these ``--hud-*`` variables, so the chat and the Qt windows
share one source of colour (``themes.py``). See ``jarvis/webchat/webchat.spec.md``.
"""
from __future__ import annotations

from pathlib import Path

from desktop_app.themes import FONT_MONO, FONT_UI, HUD_COLORS

GENERATED = Path(__file__).resolve().parents[2] / "webchat-ui" / "src" / "generated" / "theme.css"


def theme_css() -> str:
    """``:root`` custom properties, one per HUD colour, plus the UI and monospace fonts."""
    lines = ["/* Generated from desktop_app/themes.py by `python -m desktop_app.web_chat_theme`. Do not edit. */",
             ":root {"]
    lines += [f"  --hud-{key.replace('_', '-')}: {value};" for key, value in HUD_COLORS.items()]
    lines += [f"  --hud-font-ui: {FONT_UI};", f"  --hud-font-mono: {FONT_MONO};", "}", ""]
    return "\n".join(lines)


def write(target: Path = GENERATED) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(theme_css(), encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # the console may not be UTF-8 (Windows code pages)
    print(f"🎨 Wrote {write()}")

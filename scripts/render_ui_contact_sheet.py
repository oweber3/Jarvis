"""Render every Jarvis desktop window offscreen into one contact sheet.

Run with the project's Python environment, from the repository root:
    PYTHONPATH=src python scripts/render_ui_contact_sheet.py

Writes one PNG per window to ``.tmp/ui-overhaul/windows/`` and the sheet to
``.tmp/ui-overhaul/contact-sheet.png``. Windows are built from demo data only
(see ``ui_windows.py``) and rendered with ``grab()`` under Qt's offscreen
platform, so nothing appears on the screen, no daemon starts and no real
configuration, credentials or network are touched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import ui_windows  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".tmp" / "ui-overhaul"
FONT = ROOT / "src" / "desktop_app" / "desktop_assets" / "fonts" / "DejaVuSans.ttf"

COLUMNS = 4
CELL = (480, 340)
CAPTION = 26
GAP = 14
BACKDROP = (9, 12, 20)
CAPTION_COLOUR = (203, 213, 225)


def compose(images: dict[str, Path], destination: Path) -> None:
    """Lay the rendered windows out in a grid, each scaled to fit its cell and captioned."""
    font = ImageFont.truetype(str(FONT), 15)
    rows = -(-len(images) // COLUMNS)
    cell_w, cell_h = CELL
    sheet = Image.new("RGB", (
        COLUMNS * (cell_w + GAP) + GAP, rows * (cell_h + CAPTION + GAP) + GAP), BACKDROP)
    draw = ImageDraw.Draw(sheet)
    for index, (name, path) in enumerate(images.items()):
        column, row = index % COLUMNS, index // COLUMNS
        x = GAP + column * (cell_w + GAP)
        y = GAP + row * (cell_h + CAPTION + GAP)
        image = Image.open(path).convert("RGBA")
        scale = min(cell_w / image.width, cell_h / image.height, 1.0)
        thumb = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.Resampling.LANCZOS)
        sheet.paste(thumb, (x + (cell_w - thumb.width) // 2, y + CAPTION), thumb)
        draw.text((x, y + 2), f"{name}  ({image.width}x{image.height})", fill=CAPTION_COLOUR, font=font)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # emoji output on Windows consoles
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    windows_dir = OUTPUT / "windows"
    windows_dir.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, Path] = {}
    with ui_windows.isolated():
        for name in ui_windows.WINDOWS:
            widget = ui_windows.build(name)
            path = windows_dir / f"{name}.png"
            ui_windows.render(widget, path)
            widget.close()
            rendered[name] = path
            print(f"📸 Rendered {name}")
    sheet = OUTPUT / "contact-sheet.png"
    compose(rendered, sheet)
    print(f"🖼️  Contact sheet: {sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

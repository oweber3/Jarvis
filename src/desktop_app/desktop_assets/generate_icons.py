"""
Generate the tray icons for the Jarvis desktop app: the orb's data sphere as
an emblem (nested broken shells of light filaments, brightest at the rim, on a
dark disc) in the colour of each state (idle, listening, thinking), taken from the orb
palette in ``themes.py`` and kept legible at 16 px.
"""

import importlib.util
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def _load_orb_palette() -> dict:
    """The orb palette, loaded from ``themes.py`` by path so no Qt or app import is needed."""
    path = Path(__file__).resolve().parents[1] / "themes.py"
    spec = importlib.util.spec_from_file_location("jarvis_orb_palette", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ORB_PALETTE


_PALETTE = _load_orb_palette()

# Orb colour of each state icon: dim slate while not listening, cyan while listening,
# indigo while a reply is worked out (the orb's own thinking colour).
ICON_STATES = {
    "idle": _PALETTE["slate_mid"],
    "listening": _PALETTE["cyan"],
    "thinking": _PALETTE["indigo"],
}

# Emblem geometry as fractions of the icon size.
_DISC = 0.485          # dark backing disc, so the emblem reads on light and dark taskbars
# Nested bands of filaments from the rim inward, as (inner, outer, brightness): the rim brightest,
# the inner layers dimmer, filling the disc the way the orb's inner layers fill the sphere.
_BANDS = ((0.405, 0.445, 1.0), (0.355, 0.395, 0.95), (0.305, 0.345, 0.78), (0.255, 0.295, 0.6),
          (0.205, 0.245, 0.48), (0.155, 0.195, 0.4), (0.105, 0.145, 0.33), (0.06, 0.095, 0.28))


def _rgb(colour: str) -> tuple:
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _circle(draw: ImageDraw.ImageDraw, size: int, radius: float, **kwargs) -> None:
    c = size / 2
    r = radius * size
    draw.ellipse([(c - r, c - r), (c + r, c + r)], **kwargs)


def _emblem(colour: str, size: int) -> Image.Image:
    """Draw the emblem at ``size`` px: the orb's data sphere as nested broken shells of light filaments."""
    state = _rgb(colour)
    white = _rgb(_PALETTE["white"])
    backdrop = _rgb(_PALETTE["backdrop"])
    light = _mix(state, white, 0.4)
    rng = random.Random(7)   # seeded: the filaments are the same on every run
    c = size / 2

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _circle(draw, size, _DISC, fill=backdrop + (255,))

    # Glow layer: the rim blurred under the sharp strokes, added as light.
    glow = Image.new("RGB", (size, size), (0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    _circle(glow_draw, size, _BANDS[0][1], fill=_mix((0, 0, 0), state, 0.5))
    _circle(glow_draw, size, _BANDS[1][0], fill=(0, 0, 0))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.03))
    lit = ImageChops.add(img.convert("RGB"), glow)
    img = Image.merge("RGBA", (*lit.split(), img.split()[3]))
    draw = ImageDraw.Draw(img)

    # Each band is a run of filament arcs of random length with narrow breaks, lighter towards
    # the top where the light catches it, and dimmer the further in it lies.
    for inner, outer, brightness in _BANDS:
        width = max(1, round((outer - inner) * size))
        radius = (inner + outer) / 2 * size
        box = [(c - radius, c - radius), (c + radius, c + radius)]
        angle = rng.uniform(0, 360)
        end = angle + 360
        while angle < end - 4:
            span = min(rng.uniform(18, 60), end - angle - 3)
            middle = math.radians(angle + span / 2)
            shade = 0.5 - 0.5 * math.sin(middle)   # 1 at the top, 0 at the bottom (screen y runs down)
            tone = _mix(backdrop, _mix(state, light, shade * rng.uniform(0.5, 1.0)), brightness)
            draw.arc(box, angle, angle + span, fill=tone + (255,), width=width)
            angle += span + rng.uniform(3, 7)
    return img


def create_icon(color: str, filename: str, size: int = 256) -> None:
    """Create the data sphere emblem icon in ``color``, with sized PNGs and a multi-size ICO."""
    img = _emblem(color, size)
    img.save(filename)

    # Smaller versions, downsampled from the full-size emblem.
    for icon_size in [16, 32, 48, 64, 128]:
        resized = img.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
        resized.save(filename.replace('.png', f'_{icon_size}.png'))

    # .ico file for Windows (multiple sizes in one file).
    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_images = [img.resize((s, s), Image.Resampling.LANCZOS) for s in ico_sizes]
    ico_filename = filename.replace('.png', '.ico')
    ico_images[-1].save(
        ico_filename,
        format='ICO',
        append_images=ico_images[:-1]
    )


if __name__ == '__main__':
    import sys

    # Fix Windows console encoding for emojis
    if sys.platform == 'win32':
        try:
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        except Exception:
            pass

    script_dir = Path(__file__).parent

    for state, colour in ICON_STATES.items():
        create_icon(colour, str(script_dir / f'icon_{state}.png'))
        print(f"🎨 Created icon_{state}.png")

    print("\n✅ Icon generation complete!")

"""
Generate the tray icons for the Jarvis desktop app: the orb's sphere of light as
an emblem (evenly spread points of light, brightest at the rim, the back showing
through dimmer, on a dark disc) in the colour of each state (idle, listening,
thinking), taken from the orb palette in ``themes.py`` and kept legible at 16 px.
"""

import importlib.util
import math
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
_SPHERE = 0.40         # radius of the sphere of points
_POINTS = 380          # points over the sphere, spread evenly (as the orb spreads its own); fewer, larger ones than the orb's read better as an icon
_DOT = 0.0105          # radius of a point
_SUPERSAMPLE = 4       # drawn this many times larger, then reduced, so the points are smooth
_TILT = math.radians(20)   # seen from a little above, like the orb


def _rgb(colour: str) -> tuple:
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _circle(draw: ImageDraw.ImageDraw, size: int, radius: float, **kwargs) -> None:
    c = size / 2
    r = radius * size
    draw.ellipse([(c - r, c - r), (c + r, c + r)], **kwargs)


def _sphere_points():
    """The emblem's points, projected: (x, y) in sphere radii and how much light each gets, back to front."""
    golden = math.pi * (3.0 - math.sqrt(5.0))
    ct, st = math.cos(_TILT), math.sin(_TILT)
    points = []
    for i in range(_POINTS):
        y = 1.0 - 2.0 * (i + 0.5) / _POINTS
        ring = math.sqrt(1.0 - y * y)
        x, z = math.cos(i * golden) * ring, math.sin(i * golden) * ring
        y, z = y * ct - z * st, y * st + z * ct
        front = (z + 1.0) / 2.0
        limb = math.hypot(x, y)
        # As in the orb: the front brighter than the back, the outline brightest.
        points.append((z, x, y, (0.12 + 0.88 * front ** 1.7) * (0.4 + 0.6 * limb ** 4)))
    points.sort()
    return [(x, y, light) for _, x, y, light in points]


def _emblem(colour: str, size: int) -> Image.Image:
    """Draw the emblem at ``size`` px: the orb's sphere of evenly spread points of light."""
    state = _rgb(colour)
    white = _rgb(_PALETTE["white"])
    backdrop = _rgb(_PALETTE["backdrop"])
    big = size * _SUPERSAMPLE
    c = big / 2

    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _circle(draw, big, _DISC, fill=backdrop + (255,))

    # Glow layer: a soft band of the state colour at the rim, added as light under the points.
    glow = Image.new("RGB", (big, big), (0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    _circle(glow_draw, big, _SPHERE + 0.02, fill=_mix((0, 0, 0), state, 0.75))
    _circle(glow_draw, big, _SPHERE - 0.07, fill=_mix((0, 0, 0), state, 0.12))
    glow = glow.filter(ImageFilter.GaussianBlur(big * 0.02))

    # The points, as light added over the glow: brighter ones whiter, as the orb draws its brightest points.
    points = Image.new("RGB", (big, big), (0, 0, 0))
    points_draw = ImageDraw.Draw(points)
    radius, dot = _SPHERE * big, _DOT * big
    for x, y, light in _sphere_points():
        tone = _mix((0, 0, 0), _mix(state, white, max(0.0, light - 0.5) * 0.9), min(1.0, 0.2 + light * 1.3))
        px, py = c + x * radius, c - y * radius
        r = dot * (0.75 + 0.5 * light)
        points_draw.ellipse([(px - r, py - r), (px + r, py + r)], fill=tone)

    lit = ImageChops.add(ImageChops.add(img.convert("RGB"), glow), points)
    img = Image.merge("RGBA", (*lit.split(), img.split()[3]))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def create_icon(color: str, filename: str, size: int = 256) -> None:
    """Create the sphere emblem icon in ``color``, with sized PNGs and a multi-size ICO."""
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

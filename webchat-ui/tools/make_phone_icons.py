"""Draws the phone app's home-screen icons: the orb's sphere of light on its dark backdrop.

``PYTHONPATH=src python webchat-ui/tools/make_phone_icons.py`` rewrites the PNGs in ``webchat-ui/public-phone``.
The colours are the desktop orb's (``desktop_app.themes.ORB_PALETTE``). Only needed when the icon changes.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from desktop_app.themes import ORB_PALETTE

TARGET = Path(__file__).resolve().parents[1] / "public-phone"
SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}
POINTS = 1400


def _rgb(hex_colour: str):
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def sphere(size: int) -> Image.Image:
    scale = 4  # drawn large and shrunk, for smooth edges
    big = size * scale
    backdrop = _rgb(ORB_PALETTE["backdrop"])
    image = Image.new("RGB", (big, big), backdrop)
    glow = Image.new("RGB", (big, big), (0, 0, 0))
    draw = ImageDraw.Draw(glow)
    centre, radius = big / 2, big * 0.31
    golden = math.pi * (3.0 - math.sqrt(5.0))
    tilt = math.radians(20)
    cyan, sky, white = _rgb(ORB_PALETTE["cyan"]), _rgb(ORB_PALETTE["sky"]), _rgb(ORB_PALETTE["white"])
    for i in range(POINTS):
        y = 1.0 - 2.0 * (i + 0.5) / POINTS
        r = math.sqrt(1.0 - y * y)
        a = i * golden
        x, z = math.cos(a) * r, math.sin(a) * r
        y2, z2 = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
        front = (z2 + 1) / 2
        limb = math.hypot(x, y2)
        light = (0.1 + 0.9 * front ** 1.7) * (0.4 + 0.6 * limb ** 4)
        mix = max(0.0, (light - 0.5) / 0.5) * 0.7
        colour = tuple(int((c1 * (1 - mix) + c2 * mix) * min(1.0, light * 1.4)) for c1, c2 in zip(cyan if i % 5 else sky, white))
        px, py = centre + x * radius, centre - y2 * radius
        dot = big * (0.0035 + 0.004 * light)
        draw.ellipse((px - dot, py - dot, px + dot, py + dot), fill=colour)
    halo = glow.filter(ImageFilter.GaussianBlur(big * 0.03))
    arrays = [np.asarray(layer, dtype=np.float32) for layer in (image, halo, glow)]
    lit = np.clip(arrays[0] + arrays[1] * 0.9 + arrays[2], 0, 255).astype(np.uint8)
    return Image.fromarray(lit).resize((size, size), Image.LANCZOS)


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        sphere(size).save(TARGET / name, optimize=True)
        print(f"wrote {TARGET / name}")


if __name__ == "__main__":
    main()

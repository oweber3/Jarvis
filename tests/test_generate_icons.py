"""The icon generator must be deterministic.

Guards against local builds and run scripts dirtying the committed icons on
every invocation: output that depends on anything platform-specific (such as a
system font) differs per platform, and git then reports the asset files as
modified. The emblem is drawn from shapes only.
"""

import hashlib
import importlib.util
from pathlib import Path

from PIL import ImageFont

ASSETS_DIR = Path(__file__).resolve().parents[1] / "src" / "desktop_app" / "desktop_assets"
SCRIPT = ASSETS_DIR / "generate_icons.py"


def _load_module():
    """Load generate_icons.py without running its ``__main__`` block."""
    spec = importlib.util.spec_from_file_location("generate_icons_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestIconGeneration:
    def test_bundled_font_exists_and_loads(self):
        font = ASSETS_DIR / "fonts" / "DejaVuSans.ttf"
        assert font.is_file()
        ImageFont.truetype(str(font), 64)  # must parse without error

    def test_icon_has_no_text_so_no_system_font_can_change_it(self):
        script = SCRIPT.read_text(encoding="utf-8")
        assert "ImageFont" not in script

    def test_the_emblem_is_a_sphere_of_light_brightest_at_its_rim(self):
        import numpy as np

        mod = _load_module()
        pixels = np.asarray(mod._emblem("#22d3ee", 256).convert("RGB"), dtype=float).sum(axis=2)
        y, x = np.mgrid[0:256, 0:256]
        r = np.hypot(x - 127.5, y - 127.5) / 256
        centre, rim = pixels[r < 0.15].mean(), pixels[(r > 0.36) & (r < 0.44)].mean()
        assert rim > centre * 1.4               # the outline is the brightest band
        assert centre > 40                      # but the middle is filled with light, not empty
        # Made of points of light: the middle has many small bright spots, not one smooth fill.
        middle = pixels[96:160, 96:160]
        peaks = ((middle[1:-1, 1:-1] > middle[:-2, 1:-1]) & (middle[1:-1, 1:-1] > middle[2:, 1:-1])
                 & (middle[1:-1, 1:-1] > middle[1:-1, :-2]) & (middle[1:-1, 1:-1] > middle[1:-1, 2:]))
        assert peaks.sum() > 40

    def test_generation_is_byte_deterministic(self, tmp_path):
        """Two runs in the same environment must produce identical bytes
        for every output file (png, sized pngs, ico)."""
        mod = _load_module()
        out1, out2 = tmp_path / "a", tmp_path / "b"
        out1.mkdir()
        out2.mkdir()

        mod.create_icon("#9E9E9E", str(out1 / "icon_idle.png"))
        mod.create_icon("#9E9E9E", str(out2 / "icon_idle.png"))

        f1, f2 = sorted(out1.iterdir()), sorted(out2.iterdir())
        assert [p.name for p in f1] == [p.name for p in f2]
        for p1, p2 in zip(f1, f2):
            assert p1.name == p2.name
            assert _sha256(p1) == _sha256(p2), f"{p1.name} differs between runs"

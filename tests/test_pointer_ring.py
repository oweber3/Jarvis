"""Behavioural tests for the pointer ring shown while gesture snapping is on (model, geometry, controller, setting)."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop_app.pointer_ring import (  # noqa: E402
    FADE_S,
    RING_SIZE,
    RingModel,
    ring_rect,
)


def _run(model, seconds, fps=60):
    for _ in range(int(seconds * fps)):
        model.step(1.0 / fps)


# --------------------------------------------------------------------- model


class TestWhenItShows:
    def test_hidden_until_gesture_snapping_is_on(self):
        model = RingModel()
        model.observe(False)
        _run(model, 1.0)
        assert not model.visible
        assert model.presence == 0.0

    def test_shows_while_gesture_snapping_is_on(self):
        model = RingModel()
        model.observe(True)
        assert model.visible
        _run(model, FADE_S + 0.05)
        assert model.presence == pytest.approx(1.0)

    def test_switch_off_keeps_it_hidden_even_with_snapping_on(self):
        model = RingModel(enabled=False)
        model.observe(True)
        _run(model, 1.0)
        assert not model.visible

    def test_turning_the_switch_off_fades_it_out(self):
        model = RingModel()
        model.observe(True)
        _run(model, 0.5)
        model.set_enabled(False)
        _run(model, FADE_S + 0.05)
        assert not model.visible

    def test_snapping_stopping_fades_it_out_within_the_fade_time(self):
        model = RingModel()
        model.observe(True)
        _run(model, 0.5)
        model.observe(False)
        assert model.visible  # fading, not cut
        _run(model, FADE_S + 0.05)
        assert not model.visible
        assert model.presence == 0.0

    def test_fades_take_less_than_three_tenths_of_a_second(self):
        assert 0.0 < FADE_S < 0.3
        model = RingModel()
        model.observe(True)
        _run(model, FADE_S / 2)
        assert 0.0 < model.presence < 1.0

    def test_a_stale_flag_reads_as_off(self):
        """The flag reader reports False once the daemon's heartbeat stops; the ring follows it."""
        model = RingModel()
        model.observe(True)
        _run(model, 0.5)
        model.observe(False)
        _run(model, 0.5)
        assert not model.visible


class TestLook:
    def test_glow_breathes_while_shown(self):
        model = RingModel()
        model.observe(True)
        seen = []
        for _ in range(240):
            model.step(1.0 / 60)
            seen.append(model.glow)
        steady = seen[30:]
        assert max(steady) - min(steady) > 0.15   # it visibly breathes
        assert min(steady) > 0.3                   # but never goes dark while on
        assert all(0.0 <= g <= 1.0 for g in seen)

    def test_glow_is_zero_while_hidden(self):
        model = RingModel()
        _run(model, 1.0)
        assert model.glow == 0.0


# ------------------------------------------------------------------ geometry


class TestGeometry:
    @pytest.mark.parametrize("cursor", [(640, 360), (0, 0), (-1500, -200), (-1, 2879), (3839, -1080)])
    def test_ring_is_centred_on_the_pointer_on_any_monitor(self, cursor):
        x, y, w, h = ring_rect(cursor, RING_SIZE)
        assert (w, h) == (RING_SIZE, RING_SIZE)
        assert x + w / 2 == pytest.approx(cursor[0], abs=0.5)
        assert y + h / 2 == pytest.approx(cursor[1], abs=0.5)

    def test_ring_is_small(self):
        assert 40 <= RING_SIZE <= 96


# ---------------------------------------------------------------- controller


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _Flag:
    def __init__(self, on=False):
        self.on = on

    def __call__(self):
        return self.on


class _Cursor:
    def __init__(self, pos=(100, 100)):
        self.pos = pos

    def __call__(self):
        return self.pos


def _ring(flag, cursor=None, **kwargs):
    from desktop_app.pointer_ring import PointerRing

    return PointerRing(flag_reader=flag, cursor=cursor or _Cursor(), **kwargs)


def _settle(ring, seconds=0.5):
    for _ in range(int(seconds * 60)):
        ring.tick(1.0 / 60)


class TestPointerRingController:
    def test_nothing_on_screen_and_no_frame_timer_while_snapping_is_off(self, qapp):
        ring = _ring(_Flag(False))
        ring.start()
        ring.poll()
        assert ring.is_polling()
        assert not ring.is_following()
        assert ring.window() is None or not ring.window().isVisible()
        ring.shutdown()

    def test_shows_around_the_pointer_when_snapping_starts(self, qapp):
        flag, cursor = _Flag(False), _Cursor((-1500, -200))
        ring = _ring(flag, cursor)
        ring.start()
        flag.on = True
        ring.poll()
        _settle(ring)
        window = ring.window()
        assert window is not None and window.isVisible()
        assert ring.is_following()
        g = window.geometry()
        assert g.center().x() == pytest.approx(-1500, abs=1)
        assert g.center().y() == pytest.approx(-200, abs=1)
        ring.shutdown()

    def test_follows_the_pointer_wherever_it_moves(self, qapp):
        flag, cursor = _Flag(True), _Cursor((10, 10))
        ring = _ring(flag, cursor)
        ring.start()
        ring.poll()
        _settle(ring, 0.1)
        for pos in [(2500, 900), (-800, 300), (-1920, -1080)]:
            cursor.pos = pos
            ring.tick(1.0 / 60)
            g = ring.window().geometry()
            assert (g.center().x(), g.center().y()) == pytest.approx(pos, abs=1)
        ring.shutdown()

    def test_it_repaints_far_less_often_than_it_follows_the_pointer(self, qapp):
        from desktop_app.pointer_ring import PAINT_INTERVAL_S

        dt, ticks = 1.0 / 60, 120
        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.5)
        repainted_at = []
        tick = [0]
        ring.window().update = lambda *a: repainted_at.append(tick[0])
        cursor = ring._cursor
        followed = 0
        for i in range(ticks):
            tick[0] = i
            cursor.pos = (100 + i, 100)
            ring.tick(dt)
            followed += abs(ring.window().geometry().center().x() - (100 + i)) <= 1
        assert followed == ticks, "the window follows the pointer on every tick"
        assert 0 < len(repainted_at) <= ticks // 2, "and is repainted on far fewer of them"
        gaps = [(y - x) * dt for x, y in zip(repainted_at, repainted_at[1:])]
        assert min(gaps) >= PAINT_INTERVAL_S - 1e-9, "never more often than the paint interval"
        ring.shutdown()

    def test_window_passes_clicks_through_and_never_takes_focus(self, qapp):
        from PyQt6.QtCore import Qt

        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.1)
        w = ring.window()
        flags = w.windowFlags()
        assert flags & Qt.WindowType.WindowTransparentForInput
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert flags & Qt.WindowType.FramelessWindowHint
        assert (flags & Qt.WindowType.Tool) == Qt.WindowType.Tool
        assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert not w.isActiveWindow()
        ring.shutdown()

    def test_draws_a_glowing_ring_with_a_hollow_centre(self, qapp):
        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.5)
        image = ring.window().grab().toImage()
        c = image.width() // 2
        centre = image.pixelColor(c, c)
        assert centre.alpha() == 0   # the pointer's tip is never covered
        brightest = max(image.pixelColor(x, c).alpha() for x in range(image.width()))
        assert brightest > 120       # a clearly visible ring
        ring.shutdown()

    def test_hides_and_stops_the_frame_timer_when_snapping_stops(self, qapp):
        flag = _Flag(True)
        ring = _ring(flag)
        ring.start()
        ring.poll()
        _settle(ring, 0.3)
        flag.on = False
        ring.poll()
        _settle(ring, 0.5)
        assert not ring.window().isVisible()
        assert not ring.is_following()
        assert ring.is_polling()
        ring.shutdown()

    def test_an_unreadable_flag_counts_as_off(self, qapp):
        def broken():
            raise OSError("flag file gone")

        ring = _ring(broken)
        ring.start()
        ring.poll()
        _settle(ring, 0.3)
        assert not ring.is_following()
        ring.shutdown()

    def test_disabling_removes_it_and_stops_polling(self, qapp):
        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.3)
        ring.set_enabled(False)
        assert not ring.is_polling()
        assert not ring.is_following()
        assert ring.window() is None or not ring.window().isVisible()
        ring.set_enabled(True)
        assert ring.is_polling()
        ring.shutdown()

    def test_disabled_at_start_never_polls(self, qapp):
        ring = _ring(_Flag(True), enabled=False)
        ring.start()
        assert not ring.is_polling()
        ring.poll()
        _settle(ring, 0.3)
        assert ring.window() is None or not ring.window().isVisible()
        ring.shutdown()

    def test_shutdown_removes_everything(self, qapp):
        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.3)
        ring.shutdown()
        assert ring.window() is None
        assert not ring.is_polling()
        assert not ring.is_following()


# ------------------------------------------------------------------- setting


def _config(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))


class TestSetting:
    def test_on_by_default(self, tmp_path, monkeypatch):
        from desktop_app.pointer_ring import configured_enabled
        from jarvis.config import get_default_config, load_settings

        _config(tmp_path, monkeypatch, {})
        assert get_default_config()["gesture_snap_pointer_ring"] is True
        assert load_settings().gesture_snap_pointer_ring is True
        assert configured_enabled() is True

    def test_turned_off_in_config(self, tmp_path, monkeypatch):
        from desktop_app.pointer_ring import configured_enabled
        from jarvis.config import load_settings

        _config(tmp_path, monkeypatch, {"gesture_snap_pointer_ring": False})
        assert configured_enabled() is False
        assert load_settings().gesture_snap_pointer_ring is False

    def test_a_value_of_the_wrong_type_uses_the_default(self, tmp_path, monkeypatch):
        from desktop_app.pointer_ring import configured_enabled
        from jarvis.config import load_settings

        _config(tmp_path, monkeypatch, {"gesture_snap_pointer_ring": "no"})
        assert configured_enabled() is True
        assert load_settings().gesture_snap_pointer_ring is True

    def test_offered_in_gesture_snapping_settings_as_a_switch(self):
        from desktop_app.settings_window import FIELD_METADATA

        field = next(fm for fm in FIELD_METADATA if fm.key == "gesture_snap_pointer_ring")
        assert field.field_type == "bool"
        assert field.category == "gesture_snap"


class TestTrayWiring:
    def _tray(self, ring):
        from unittest.mock import MagicMock

        from desktop_app.app import JarvisSystemTray, OllamaRuntimeOwnership

        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray.is_listening = False
        tray.daemon_process = None
        tray._ollama_runtime_ownership = OllamaRuntimeOwnership()
        tray.memory_viewer = MagicMock()
        tray.pointer_ring = ring
        return tray

    def test_quitting_removes_the_ring(self, qapp):
        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.2)
        self._tray(ring).cleanup_on_exit()
        assert ring.window() is None
        assert not ring.is_polling()

    def test_saving_settings_applies_the_switch_at_once(self, qapp, tmp_path, monkeypatch):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QDialog

        _config(tmp_path, monkeypatch, {"gesture_snap_pointer_ring": False})
        ring = _ring(_Flag(True))
        ring.start()
        ring.poll()
        _settle(ring, 0.2)
        tray = self._tray(ring)

        class _Saved:
            def exec(self):
                return QDialog.DialogCode.Accepted

        with patch("desktop_app.settings_window.SettingsWindow", return_value=_Saved()):
            tray.show_settings()
        assert not ring.is_enabled()
        assert not ring.is_following()
        ring.shutdown()

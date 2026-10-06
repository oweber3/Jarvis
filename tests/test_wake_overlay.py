"""Behavioural tests for the wake screen effect (model, geometry and controller)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop_app.orb_widget import OrbState  # noqa: E402
from desktop_app.wake_overlay import (  # noqa: E402
    MAX_UNCHANGED_S,
    OverlayModel,
    edge_strips,
    frame_thickness,
    screens_to_cover,
)


def _run(model, seconds, level=None, fps=30):
    for _ in range(int(seconds * fps)):
        model.step(1.0 / fps, level)


def _woken():
    model = OverlayModel()
    model.observe(OrbState.IDLE)
    model.observe(OrbState.LISTENING)
    return model


class TestWhenItEngages:
    def test_wake_from_idle_engages(self):
        model = _woken()
        assert model.engaged
        assert model.visible

    def test_staying_idle_never_engages(self):
        model = OverlayModel()
        for _ in range(5):
            model.observe(OrbState.IDLE)
            _run(model, 0.2)
        assert not model.engaged
        assert not model.visible

    def test_listening_already_at_start_does_not_engage(self):
        model = OverlayModel()
        model.observe(OrbState.LISTENING)
        assert not model.engaged

    def test_typed_request_and_dictation_do_not_engage(self):
        for state in (OrbState.THINKING, OrbState.DICTATING, OrbState.SPEAKING):
            model = OverlayModel()
            model.observe(OrbState.IDLE)
            model.observe(state)
            assert not model.engaged, state

    def test_waking_from_asleep_does_not_engage(self):
        model = OverlayModel()
        model.observe(OrbState.OFFLINE)
        model.observe(OrbState.LISTENING)
        assert not model.engaged


class TestFollowingTheConversation:
    def test_stays_through_thinking_speaking_and_the_follow_up_window(self):
        model = _woken()
        for state in (OrbState.THINKING, OrbState.SPEAKING, OrbState.LISTENING, OrbState.THINKING):
            model.observe(state)
            _run(model, 1)
            assert model.engaged, state
            assert model.presence > 0.9

    @pytest.mark.parametrize("state", [OrbState.IDLE, OrbState.OFFLINE, OrbState.DICTATING,
                                       OrbState.MUTED, OrbState.ERROR])
    def test_leaving_the_conversation_fades_out_within_a_second(self, state):
        model = _woken()
        _run(model, 1)
        model.observe(state)
        assert not model.engaged
        _run(model, 0.2)
        assert model.visible  # fading, not cut
        _run(model, 0.8)
        assert not model.visible

    def test_a_stuck_state_fades_out_and_the_next_wake_engages_again(self):
        model = _woken()
        _run(model, MAX_UNCHANGED_S + 2)
        assert not model.visible
        model.observe(OrbState.LISTENING)
        assert not model.engaged
        model.observe(OrbState.IDLE)
        model.observe(OrbState.LISTENING)
        assert model.engaged

    def test_state_changes_keep_the_safety_net_from_firing(self):
        model = _woken()
        for _ in range(4):
            model.observe(OrbState.THINKING)
            _run(model, MAX_UNCHANGED_S * 0.6)
            model.observe(OrbState.SPEAKING)
            _run(model, MAX_UNCHANGED_S * 0.6)
        assert model.engaged

    def test_disengage_fades_out(self):
        model = _woken()
        _run(model, 1)
        model.disengage()
        _run(model, 1)
        assert not model.visible


class TestPulse:
    """The frame breathes: a soft glow that pulses at a rhythm set by the state."""

    def _beats(self, model, seconds, level=None):
        """How many pulses the frame sends in ``seconds``."""
        before = model.pulses_sent
        _run(model, seconds, level)
        return model.pulses_sent - before

    def test_waking_sends_a_heartbeat_double_pulse_and_a_flash(self):
        model = _woken()
        model.step(1.0 / 30, None)
        assert model.flash > 0.9
        peaks = []
        for _ in range(20):
            model.step(1.0 / 30, None)
            peaks.append(model.pulse)
        assert model.pulses_sent >= 2
        assert max(peaks) > 0.8
        # Two beats: the swell falls after the first and rises again for the second.
        assert any(peaks[i] < peaks[i - 1] and max(peaks[i:]) > peaks[i] + 0.1 for i in range(1, len(peaks)))

    def test_the_wake_heartbeat_settles_into_breathing(self):
        model = _woken()
        _run(model, 2.5, 0.0)
        assert model.pulse < 0.05
        assert model.flash < 0.05

    def test_listening_breathes_slowly_and_visibly(self):
        model = _woken()
        _run(model, 3)
        samples = []
        for _ in range(150):
            model.step(1.0 / 30, 0.0)
            samples.append(model.intensity)
        assert max(samples) - min(samples) > 0.2
        assert 1 <= self._beats(model, 6, 0.0) <= 3

    def test_thinking_pulses_faster_than_listening(self):
        thinking, listening = _woken(), _woken()
        thinking.observe(OrbState.THINKING)
        _run(thinking, 2)
        _run(listening, 2)
        assert self._beats(thinking, 6) >= 2 * self._beats(listening, 6, 0.0)

    def test_the_rhythm_changes_smoothly_without_a_jump(self):
        model = _woken()
        _run(model, 2)
        before = model.breath
        model.observe(OrbState.THINKING)
        model.step(1.0 / 30, None)
        assert abs(model.breath - before) < 0.1

    def test_louder_input_glows_brighter(self):
        quiet, loud = _woken(), _woken()
        _run(quiet, 3, level=0.02)
        _run(loud, 3, level=0.9)
        assert loud.intensity > quiet.intensity + 0.15

    def test_speaking_without_a_real_level_still_pulses(self):
        model = _woken()
        model.observe(OrbState.SPEAKING)
        _run(model, 1)
        samples = []
        for _ in range(60):
            model.step(1.0 / 30, None)
            samples.append(model.intensity)
        assert max(samples) - min(samples) > 0.05

    def test_values_stay_in_range_and_huge_gaps_are_safe(self):
        model = _woken()
        model.step(30.0, 5.0)
        for value in (model.presence, model.intensity, model.level, model.breath, model.flash, model.pulse):
            assert 0.0 <= value <= 1.0

    def test_colours_follow_the_conversation_not_the_resting_state(self):
        model = _woken()
        model.observe(OrbState.THINKING)
        model.observe(OrbState.OFFLINE)
        assert model.look_state is OrbState.THINKING


class TestGeometry:
    @pytest.mark.parametrize("size", [(2560, 1440), (1080, 1920), (800, 600), (3840, 2160)])
    def test_strips_frame_the_edges_and_leave_the_middle_clear(self, size):
        w, h = size
        t = frame_thickness(w, h)
        strips = edge_strips(w, h, t)
        assert t < min(w, h) / 6

        def covered(px, py):
            return any(x <= px < x + sw and y <= py < y + sh for x, y, sw, sh in strips)

        assert not covered(w // 2, h // 2)
        assert not covered(w // 3, h // 3)
        for i in range(50):
            assert covered(int(i / 50 * w), 0)
            assert covered(int(i / 50 * w), h - 1)
            assert covered(0, int(i / 50 * h))
            assert covered(w - 1, int(i / 50 * h))
        # No pixel is drawn twice: the strips do not overlap.
        area = sum(sw * sh for _, _, sw, sh in strips)
        assert area == w * h - (w - 2 * t) * (h - 2 * t)


class TestScreensToCover:
    SCREENS = [(0, 0, 2560, 1440, 1.0), (-1080, -162, 1080, 1920, 1.0), (2560, 0, 1280, 720, 1.5)]

    def test_every_screen_is_covered_without_a_fullscreen_app(self):
        assert screens_to_cover(self.SCREENS, None) == [s[:4] for s in self.SCREENS]

    def test_the_screen_with_a_fullscreen_app_is_skipped(self):
        busy = (-1080, -162, 0, 1758)    # native left, top, right, bottom of the second screen
        assert screens_to_cover(self.SCREENS, busy) == [self.SCREENS[0][:4], self.SCREENS[2][:4]]

    def test_scaled_screens_are_matched_in_native_pixels(self):
        busy = (2560, 0, 2560 + 1920, 1080)
        assert screens_to_cover(self.SCREENS, busy) == [s[:4] for s in self.SCREENS[:2]]


class TestPainter:
    W, H = 1920, 1080

    def _frame_over(self, backdrop, model):
        """The top-left corner of the frame composited over a plain ``backdrop`` colour."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QImage, QPainter
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        t = frame_thickness(self.W, self.H)
        layer = QImage(self.W, t, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        p = QPainter(layer)
        colour, accent = state_colours(model.look_state)
        FramePainter(self.W, self.H, (0, 0, self.W, t)).paint(p, model, QColor(colour), QColor(accent))
        p.end()
        image = QImage(self.W, t, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(backdrop))
        p = QPainter(image)
        p.drawImage(0, 0, layer)
        p.end()
        return image

    def _difference(self, image, backdrop, x, y):
        from PyQt6.QtGui import QColor
        c, b = image.pixelColor(x, y), QColor(backdrop)
        return abs(c.red() - b.red()) + abs(c.green() - b.green()) + abs(c.blue() - b.blue())

    @pytest.mark.parametrize("backdrop", ["#ffffff", "#000000", "#808080"])
    def test_the_edge_glow_shows_on_light_and_dark_screens(self, qapp, backdrop):
        model = _woken()
        _run(model, 3, 0.0)
        image = self._frame_over(backdrop, model)
        assert self._difference(image, backdrop, self.W // 2, 2) > 90      # the edge itself clearly lit
        assert self._difference(image, backdrop, self.W // 2, image.height() - 1) < 12   # fades out inward

    def test_the_glow_is_brighter_at_the_crest_of_a_breath(self, qapp):
        model = _woken()
        _run(model, 3, 0.0)
        low = high = None
        for _ in range(150):
            model.step(1.0 / 30, 0.0)
            if low is None or model.intensity < low[0]:
                low = (model.intensity, self._difference(self._frame_over("#000000", model), "#000000", 400, 12))
            if high is None or model.intensity > high[0]:
                high = (model.intensity, self._difference(self._frame_over("#000000", model), "#000000", 400, 12))
        assert high[1] > low[1] + 20

    def test_an_unchanged_frame_is_not_repainted(self, qapp):
        from PyQt6.QtGui import QColor
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 2)
        band = FramePainter(self.W, self.H, (0, 0, self.W, 80))
        colour = QColor("#22d3ee")
        assert band.needs_repaint(model, colour, colour)
        assert not band.needs_repaint(model, colour, colour)
        model.step(1.0 / 30, None)
        assert band.needs_repaint(model, colour, colour)


class TestSetting:
    def _config(self, tmp_path, monkeypatch, values):
        import json

        path = tmp_path / "config.json"
        path.write_text(json.dumps(values))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))

    def test_on_by_default(self, tmp_path, monkeypatch):
        from desktop_app.wake_overlay import configured_enabled
        from jarvis.config import get_default_config

        self._config(tmp_path, monkeypatch, {})
        assert get_default_config()["wake_overlay_enabled"] is True
        assert configured_enabled() is True

    def test_turned_off_in_config(self, tmp_path, monkeypatch):
        from desktop_app.wake_overlay import configured_enabled

        self._config(tmp_path, monkeypatch, {"wake_overlay_enabled": False})
        assert configured_enabled() is False

    def test_offered_in_settings_as_a_switch(self):
        from desktop_app.settings_window import FIELD_METADATA

        field = next(fm for fm in FIELD_METADATA if fm.key == "wake_overlay_enabled")
        assert field.field_type == "bool"


# ---------------------------------------------------------------- controller


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _States:
    def __init__(self, state=OrbState.IDLE):
        self.state = state

    def __call__(self):
        return self.state


SCREENS = [(0, 0, 1280, 720), (1280, 0, 800, 600)]


def _overlay(states, screens=SCREENS, **kwargs):
    from desktop_app.orb_widget import AudioLevelSource
    from desktop_app.wake_overlay import WakeOverlay

    return WakeOverlay(state_reader=states, audio_source=AudioLevelSource(),
                       screens=lambda: list(screens), **kwargs)


def _flush_deletes(qapp):
    from PyQt6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    qapp.processEvents()


def _wake(overlay, states):
    states.state = OrbState.IDLE
    overlay.poll()
    states.state = OrbState.LISTENING
    overlay.poll()


class TestWakeOverlayController:
    def test_nothing_is_on_screen_and_nothing_animates_before_a_wake(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        overlay.poll()
        assert overlay.windows() == []
        assert not overlay.is_animating()
        assert overlay.is_watching()
        overlay.shutdown()

    def test_wake_frames_every_screen_edge_and_animates(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        windows = overlay.windows()
        assert len(windows) == 4 * len(SCREENS)
        assert all(w.isVisible() for w in windows)
        assert overlay.is_animating()
        for sx, sy, sw, sh in SCREENS:
            on_screen = [w.geometry() for w in windows
                         if sx <= w.geometry().x() < sx + sw and sy <= w.geometry().y() < sy + sh]
            assert len(on_screen) == 4
            assert not any(g.contains(sx + sw // 2, sy + sh // 2) for g in on_screen)
        overlay.shutdown()

    def test_edge_windows_pass_clicks_through_and_never_take_focus(self, qapp):
        from PyQt6.QtCore import Qt

        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        for w in overlay.windows():
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
        overlay.shutdown()

    def test_edges_light_up_while_engaged(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        for _ in range(20):
            overlay.tick(1.0 / 30)
        top = next(w for w in overlay.windows() if w.geometry().y() == 0 and w.width() == 1280)
        image = top.grab().toImage()
        lit = sum(1 for x in range(0, image.width(), 8) for y in range(0, image.height(), 2)
                  if image.pixelColor(x, y).alpha() > 20)
        assert lit > 50
        overlay.shutdown()

    def test_conversation_end_fades_out_and_removes_every_window(self, qapp):
        from PyQt6 import sip

        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        windows = overlay.windows()
        for _ in range(15):
            overlay.tick(1.0 / 30)
        states.state = OrbState.IDLE
        overlay.poll()
        overlay.tick(1.0 / 30)
        assert overlay.windows(), "fades out rather than vanishing"
        for _ in range(45):
            overlay.tick(1.0 / 30)
        assert overlay.windows() == []
        assert not overlay.is_animating()
        assert overlay.is_watching()
        _flush_deletes(qapp)
        assert all(sip.isdeleted(w) for w in windows)
        overlay.shutdown()

    def test_disabling_removes_the_effect_and_ignores_wakes(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        assert overlay.windows()
        overlay.set_enabled(False)
        assert overlay.windows() == []
        assert not overlay.is_animating()
        _wake(overlay, states)
        assert overlay.windows() == []
        overlay.set_enabled(True)
        _wake(overlay, states)
        assert overlay.windows()
        overlay.shutdown()

    def test_starting_disabled_does_not_watch(self, qapp):
        overlay = _overlay(_States(), enabled=False)
        overlay.start()
        assert not overlay.is_watching()
        overlay.set_enabled(True)
        assert overlay.is_watching()
        overlay.shutdown()

    def test_shutdown_stops_everything(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        overlay.shutdown()
        assert overlay.windows() == []
        assert not overlay.is_animating()
        assert not overlay.is_watching()

    def test_screens_are_read_again_at_each_wake(self, qapp):
        states = _States()
        screens = [(0, 0, 1280, 720)]
        overlay = _overlay(states, screens=screens)
        overlay.start()
        _wake(overlay, states)
        assert len(overlay.windows()) == 4
        states.state = OrbState.IDLE
        overlay.poll()
        for _ in range(60):
            overlay.tick(1.0 / 30)
        screens.append((1280, 0, 800, 600))
        _wake(overlay, states)
        assert len(overlay.windows()) == 8
        overlay.shutdown()

    def test_no_screens_means_nothing_shown(self, qapp):
        states = _States()
        overlay = _overlay(states, screens=[])
        overlay.start()
        _wake(overlay, states)
        assert overlay.windows() == []
        assert not overlay.is_animating()
        overlay.shutdown()

    def test_unreadable_state_counts_as_offline(self, qapp):
        def broken():
            raise OSError("state file gone")

        overlay = _overlay(broken)
        overlay.start()
        overlay.poll()
        assert overlay.windows() == []
        overlay.shutdown()


class TestTrayWiring:
    def _tray(self, overlay):
        from unittest.mock import MagicMock

        from desktop_app.app import JarvisSystemTray, OllamaRuntimeOwnership

        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray.is_listening = False
        tray.daemon_process = None
        tray._ollama_runtime_ownership = OllamaRuntimeOwnership()
        tray.memory_viewer = MagicMock()
        tray.wake_overlay = overlay
        return tray

    def test_quitting_removes_the_effect_and_stops_watching(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        tray = self._tray(overlay)
        tray.cleanup_on_exit()
        assert overlay.windows() == []
        assert not overlay.is_watching()

    def test_saving_settings_applies_the_switch_at_once(self, qapp, tmp_path, monkeypatch):
        import json
        from unittest.mock import patch

        from PyQt6.QtWidgets import QDialog

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"wake_overlay_enabled": False}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        tray = self._tray(overlay)

        class _Saved:
            def exec(self):
                return QDialog.DialogCode.Accepted

        with patch("desktop_app.settings_window.SettingsWindow", return_value=_Saved()):
            tray.show_settings()
        assert not overlay.is_enabled()
        assert overlay.windows() == []
        overlay.shutdown()

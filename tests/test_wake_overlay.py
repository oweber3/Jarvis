"""Behavioural tests for the wake screen effect (model, geometry and controller)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop_app.orb_widget import OrbState  # noqa: E402
from desktop_app.wake_overlay import (  # noqa: E402
    FRAME_FAST_MS,
    FRAME_STEADY_MS,
    MAX_UNCHANGED_S,
    OverlayModel,
    ToneFade,
    edge_strips,
    frame_interval_ms,
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


class TestFollowsTheClock:
    """The animation runs on elapsed time, so a late frame lands where steady frames would."""

    @pytest.mark.parametrize("late", [0.12, 0.2])
    def test_a_late_frame_lands_where_steady_frames_would(self, late):
        steady, delayed = _woken(), _woken()
        for model in (steady, delayed):
            _run(model, 2.6, 0.0)
        for _ in range(round(late / 0.02)):
            steady.step(0.02, 0.0)
        delayed.step(late, 0.0)
        assert abs(delayed.breath - steady.breath) < 0.05
        assert abs(delayed.intensity - steady.intensity) < 0.05

    def test_the_picture_does_not_depend_on_the_frame_rate(self):
        slow, fast = _woken(), _woken()
        for _ in range(20):
            slow.step(1 / 15, 0.0)
        for _ in range(60):
            fast.step(1 / 45, 0.0)
        assert abs(slow.intensity - fast.intensity) < 0.05
        assert abs(slow.presence - fast.presence) < 0.01

    def test_a_stall_far_beyond_a_frame_never_runs_the_wake_beats_ahead_of_the_screen(self):
        model = _woken()
        model.step(5.0, 0.0)   # the machine slept: not a quarter of the way through the beats
        assert 0.0 <= model.pulse <= 1.0
        assert model.time < 1.0


class TestFrameCadence:
    """Frames come quickly while the light changes fast and gently once it only breathes."""

    def test_the_wake_beats_get_the_fast_cadence_and_breathing_the_steady_one(self):
        model = _woken()
        model.step(1 / 30, None)
        assert frame_interval_ms(model) == FRAME_FAST_MS
        _run(model, 4, 0.0)
        assert frame_interval_ms(model) == FRAME_STEADY_MS

    def test_fading_out_gets_the_fast_cadence(self):
        model = _woken()
        _run(model, 4, 0.0)
        model.disengage()
        model.step(1 / 30, None)
        assert frame_interval_ms(model) == FRAME_FAST_MS

    def test_the_fast_cadence_is_faster(self):
        assert FRAME_FAST_MS < FRAME_STEADY_MS


class TestToneFade:
    """The glow changes colour by cross-fading to a palette that is ready, never by re-tinting mid-frame."""

    A = ("#22d3ee", "#3b82f6")
    B = ("#818cf8", "#38bdf8")

    def _weights(self, fade):
        return {palette: weight for palette, weight in fade.tones}

    def test_it_starts_on_one_palette(self):
        fade = ToneFade(self.A)
        assert fade.tones == [(self.A, 1.0)]
        assert fade.wanted is None

    def test_a_new_palette_is_asked_for_and_nothing_changes_until_it_is_ready(self):
        fade = ToneFade(self.A)
        fade.retarget(self.B)
        assert fade.wanted == self.B
        for _ in range(30):
            fade.step(1 / 30, ready=False)
        assert fade.tones == [(self.A, 1.0)]

    def test_once_ready_it_cross_fades_smoothly_and_completes_quickly(self):
        fade = ToneFade(self.A)
        fade.retarget(self.B)
        seen = []
        for _ in range(60):
            fade.step(1 / 60, ready=True)
            weights = self._weights(fade)
            assert sum(weights.values()) == pytest.approx(1.0)
            seen.append(weights.get(self.B, 0.0))
        assert seen == sorted(seen)                                 # only ever towards the new palette
        assert max(b - a for a, b in zip(seen, seen[1:])) < 0.2     # no jump between frames
        assert fade.tones == [(self.B, 1.0)]
        assert fade.wanted is None

    def test_a_change_during_a_fade_waits_for_it_to_finish(self):
        fade = ToneFade(self.A)
        fade.retarget(self.B)
        fade.step(0.1, ready=True)
        fade.retarget(self.A)
        assert self._weights(fade).get(self.B, 0.0) > 0.0     # mid-fade
        seen = []
        for _ in range(120):
            fade.step(1 / 60, ready=True)
            seen.append(self._weights(fade).get(self.B, 0.0))
        # B is allowed to finish (it reaches 1.0) before the fade back to A, which then completes.
        assert max(seen) == 1.0
        assert seen.index(1.0) < len(seen) - 1 and seen[-1] == 0.0
        assert fade.tones == [(self.A, 1.0)]

    def test_retargeting_the_palette_already_shown_changes_nothing(self):
        fade = ToneFade(self.A)
        fade.retarget(self.A)
        assert fade.wanted is None
        fade.step(0.5, ready=False)
        assert fade.tones == [(self.A, 1.0)]


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
        FramePainter(self.W, self.H, (0, 0, self.W, t)).paint(p, model, [(state_colours(model.look_state), 1.0)])
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
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 2)
        band = FramePainter(self.W, self.H, (0, 0, self.W, 80))
        tones = [(state_colours(OrbState.LISTENING), 1.0)]
        assert band.needs_repaint(model, tones)
        assert not band.needs_repaint(model, tones)
        _run(model, 0.4)
        assert band.needs_repaint(model, tones)

    def test_a_change_of_palette_is_repainted(self, qapp):
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 2)
        band = FramePainter(self.W, self.H, (0, 0, self.W, 80))
        assert band.needs_repaint(model, [(state_colours(OrbState.LISTENING), 1.0)])
        assert band.needs_repaint(model, [(state_colours(OrbState.THINKING), 1.0)])

    def test_a_barely_changing_glow_is_not_repainted_every_frame(self, qapp):
        """Near the top and bottom of a breath the picture holds still: those frames cost nothing."""
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 3, 0.0)
        band = FramePainter(self.W, self.H, (0, 0, self.W, 80))
        tones = [(state_colours(OrbState.LISTENING), 1.0)]
        repainted = 0
        frames = 30 * 12   # four breaths of the listening rhythm
        for _ in range(frames):
            model.step(1.0 / 30, 0.0)
            repainted += band.needs_repaint(model, tones)
        assert repainted < frames * 0.9

    def test_it_is_ready_once_its_glow_is_rendered_and_painting_then_renders_nothing_more(self, qapp):
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        palette = state_colours(OrbState.LISTENING)
        band = FramePainter(self.W, self.H, (0, 0, self.W, 80))
        assert not band.is_ready(palette)
        steps = 0
        while not band.prepare_next(palette):
            steps += 1
            assert steps < 10
        assert band.is_ready(palette)
        assert band.prepare_next(palette)   # nothing left to do

    def test_an_unprepared_band_still_paints(self, qapp):
        model = _woken()
        _run(model, 3, 0.0)
        image = self._frame_over("#000000", model)
        assert self._difference(image, "#000000", self.W // 2, 2) > 90

    def test_scanlines_cut_every_third_row_of_the_glow(self, qapp):
        model = _woken()
        _run(model, 3, 0.0)
        image = self._frame_over("#000000", model)

        def lit(y):
            return self._difference(image, "#000000", 400, y)

        # Rows 0, 3, 6... are the cut ones: the glow there is dimmer than on the rows beside them.
        cut = range(3, 21, 3)   # further in the glow is too faint for single steps to show
        assert all(lit(y) < lit(y - 1) and lit(y) < lit(y + 1) for y in cut)

    def test_scanlines_follow_the_screen_rows_in_a_band_that_starts_off_the_pattern(self, qapp):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage, QPainter
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 3, 0.0)
        top, t = 100, frame_thickness(self.W, self.H)    # a side band starting on a row that is not a multiple of 3
        layer = QImage(t, 60, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        p = QPainter(layer)
        p.translate(0, -top)
        FramePainter(self.W, self.H, (0, top, t, 60)).paint(p, model, [(state_colours(OrbState.LISTENING), 1.0)])
        p.end()
        alpha = [layer.pixelColor(2, y).alpha() for y in range(60)]
        for y in range(1, 59):
            if (top + y) % 3 == 0:
                assert alpha[y] < alpha[y - 1] and alpha[y] < alpha[y + 1]    # the cut rows are the dim ones

    def test_two_tones_alternate_round_the_screen(self, qapp):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QImage, QPainter
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 3, 0.0)
        palette = ("#ff0000", "#0000ff")
        t = frame_thickness(self.W, self.H)
        layer = QImage(self.W, t, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        p = QPainter(layer)
        FramePainter(self.W, self.H, (0, 0, self.W, t)).paint(p, model, [(palette, 1.0)])
        p.end()
        centre, corner = layer.pixelColor(self.W // 2, 2), layer.pixelColor(20, 2)
        assert centre.red() > centre.blue()     # the colour at the middle of the top edge
        assert corner.blue() > corner.red()     # the accent towards the corner

    def test_cross_fading_two_palettes_keeps_the_glow_about_as_strong(self, qapp):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage, QPainter
        from desktop_app.orb_widget import state_colours
        from desktop_app.wake_overlay import FramePainter

        model = _woken()
        _run(model, 3, 0.0)
        a, b = state_colours(OrbState.LISTENING), state_colours(OrbState.THINKING)
        t = frame_thickness(self.W, self.H)

        def alpha_at(tones):
            layer = QImage(self.W, t, QImage.Format.Format_ARGB32_Premultiplied)
            layer.fill(Qt.GlobalColor.transparent)
            p = QPainter(layer)
            FramePainter(self.W, self.H, (0, 0, self.W, t)).paint(p, model, tones)
            p.end()
            return layer.pixelColor(400, 1).alpha()

        steady = alpha_at([(a, 1.0)])
        for weight in (0.25, 0.5, 0.75):
            assert alpha_at([(a, 1.0 - weight), (b, weight)]) >= steady * 0.8


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


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TestSmoothActivation:
    """The wake must not stutter: everything is rendered before the light is shown, and the
    animation starts from the first frame that is actually on screen."""

    def test_every_window_is_fully_prepared_when_it_is_shown(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        assert overlay.windows()
        assert all(w.is_prepared() for w in overlay.windows())
        overlay.shutdown()

    def test_the_animation_waits_for_the_first_painted_frame(self, qapp):
        clock = _Clock()
        states = _States()
        overlay = _overlay(states, clock=clock)
        overlay.start()
        _wake(overlay, states)
        for _ in range(5):          # the windows have been shown but nothing has been painted yet
            clock.now += 0.03
            overlay._on_frame()
        assert overlay.model.time == 0.0
        assert overlay.model.presence == 0.0
        qapp.processEvents()        # the first paint happens
        clock.now += 0.03
        overlay._on_frame()
        assert overlay.model.time == pytest.approx(0.03)   # counted from the painted frame, not from the wake
        overlay.shutdown()

    def test_it_does_not_wait_for_a_paint_that_never_comes(self, qapp):
        clock = _Clock()
        states = _States()
        overlay = _overlay(states, clock=clock)
        overlay.start()
        _wake(overlay, states)
        for _ in range(20):
            clock.now += 0.03
            overlay._on_frame()
        assert overlay.model.time > 0.0
        overlay.shutdown()

    def test_a_late_frame_advances_by_the_time_that_passed(self, qapp):
        clock = _Clock()
        states = _States()
        overlay = _overlay(states, clock=clock)
        overlay.start()
        _wake(overlay, states)
        qapp.processEvents()
        overlay._on_frame()
        before = overlay.model.time
        clock.now += 0.15
        overlay._on_frame()
        assert overlay.model.time - before == pytest.approx(0.15)
        overlay.shutdown()

    def test_frames_are_quick_for_the_wake_beats_then_settle(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        overlay.tick(1.0 / 60)
        assert overlay.frame_interval() == FRAME_FAST_MS
        for _ in range(240):
            overlay.tick(1.0 / 60)
        assert overlay.frame_interval() == FRAME_STEADY_MS
        overlay.shutdown()

    def test_a_change_of_state_cross_fades_the_colour_without_a_jump(self, qapp):
        from desktop_app.orb_widget import state_colours

        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        for _ in range(60):
            overlay.tick(1.0 / 30)
        assert overlay.tones == [(state_colours(OrbState.LISTENING), 1.0)]
        states.state = OrbState.THINKING
        overlay.poll()
        shares = []
        for _ in range(90):
            overlay.tick(1.0 / 30)
            shares.append(dict(overlay.tones).get(state_colours(OrbState.THINKING), 0.0))
            assert sum(weight for _, weight in overlay.tones) == pytest.approx(1.0)
        assert shares[-1] == 1.0
        assert max(b - a for a, b in zip(shares, shares[1:])) < 0.25
        overlay.shutdown()

    def test_a_new_palette_is_rendered_a_little_at_a_time_never_all_in_one_frame(self, qapp, monkeypatch):
        from desktop_app import wake_overlay

        clock = _Clock()

        def advancing():   # every read takes a little time, like work being done
            clock.now += 0.002
            return clock.now

        states = _States()
        overlay = _overlay(states, clock=advancing)
        overlay.start()
        _wake(overlay, states)
        rendered = []
        original = wake_overlay.FramePainter.prepare_next

        def counting(self, palette):
            rendered[-1] += 1
            return original(self, palette)

        monkeypatch.setattr(wake_overlay.FramePainter, "prepare_next", counting)
        states.state = OrbState.THINKING
        overlay.poll()
        for _ in range(200):
            rendered.append(0)
            overlay.tick(1.0 / 30)
            if overlay.tones == [(wake_overlay.state_colours(OrbState.THINKING), 1.0)]:
                break
        assert overlay.tones == [(wake_overlay.state_colours(OrbState.THINKING), 1.0)]
        assert max(rendered) <= 4            # a couple of images a frame, within the frame budget
        assert sum(1 for count in rendered if count) >= len(overlay.windows()) / 4   # spread over several frames
        overlay.shutdown()

    def test_logging_is_warmed_up_when_watching_starts_not_at_the_wake(self, qapp, monkeypatch):
        """Debug logging loads the settings the first time it is used: that must happen at start, not at the wake."""
        from desktop_app import wake_overlay

        calls = []
        monkeypatch.setattr(wake_overlay, "debug_log", lambda *a, **k: calls.append(a[0]))
        overlay = _overlay(_States())
        overlay.start()
        assert calls
        overlay.shutdown()

    def test_the_light_waits_for_every_window_to_paint_before_the_beats_begin(self, qapp):
        clock = _Clock()
        states = _States()
        overlay = _overlay(states, clock=clock)
        overlay.start()
        _wake(overlay, states)
        windows = overlay.windows()
        overlay.note_painted(windows[0])        # one window has painted, the others have not
        clock.now += 0.03
        overlay._on_frame()
        assert overlay.model.time == 0.0
        for window in windows:
            overlay.note_painted(window)
        clock.now += 0.03
        overlay._on_frame()
        assert overlay.model.time == pytest.approx(0.03)
        overlay.shutdown()

    def test_a_failure_while_showing_leaves_nothing_behind_and_the_next_wake_works(self, qapp, monkeypatch):
        from desktop_app import wake_overlay

        states = _States()
        overlay = _overlay(states)
        overlay.start()
        original = wake_overlay.FramePainter.prepare_next

        def failing(self, palette):
            raise RuntimeError("out of memory")

        monkeypatch.setattr(wake_overlay.FramePainter, "prepare_next", failing)
        _wake(overlay, states)
        assert overlay.windows() == []
        assert not overlay.is_animating()
        monkeypatch.setattr(wake_overlay.FramePainter, "prepare_next", original)
        states.state = OrbState.IDLE
        overlay.poll()
        _wake(overlay, states)
        assert overlay.windows()
        overlay.shutdown()

    def test_waking_again_while_fading_out_keeps_the_light_and_brings_it_back(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        for _ in range(30):
            overlay.tick(1.0 / 30)
        windows = overlay.windows()
        states.state = OrbState.IDLE
        overlay.poll()
        for _ in range(6):
            overlay.tick(1.0 / 30)
        assert 0.0 < overlay.model.presence < 1.0
        _wake(overlay, states)
        for _ in range(30):
            overlay.tick(1.0 / 30)
        assert overlay.windows() == windows
        assert overlay.model.presence > 0.9
        overlay.shutdown()

    def test_shutting_down_mid_fade_removes_everything_and_later_wakes_are_ignored(self, qapp):
        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        states.state = OrbState.IDLE
        overlay.poll()
        overlay.tick(1.0 / 30)
        overlay.shutdown()
        assert overlay.windows() == []
        assert not overlay.is_animating()
        _wake(overlay, states)
        assert overlay.windows() == []

    def test_only_the_palettes_in_use_stay_rendered(self, qapp):
        from desktop_app.orb_widget import state_colours

        states = _States()
        overlay = _overlay(states)
        overlay.start()
        _wake(overlay, states)
        listening, thinking = state_colours(OrbState.LISTENING), state_colours(OrbState.THINKING)
        states.state = OrbState.THINKING
        overlay.poll()
        for _ in range(120):
            overlay.tick(1.0 / 30)
        window = overlay.windows()[0]
        assert window.is_ready(thinking)
        assert not window.is_ready(listening)
        overlay.shutdown()


class TestStateWatch:
    def _file_overlay(self, states, path, **kwargs):
        return _overlay(states, state_file=str(path), watch_interval_ms=5000, **kwargs)

    def _arm(self, qapp):
        """Qt starts watching a file on a helper thread: give it a moment, as the real app has had."""
        self._wait_for(qapp, lambda: False, seconds=0.3)

    def _wait_for(self, qapp, condition, seconds=3.0):
        import time

        end = time.monotonic() + seconds
        while time.monotonic() < end and not condition():
            qapp.processEvents()
            time.sleep(0.005)
        return condition()

    def test_a_change_of_the_state_file_shows_the_light_without_waiting_for_the_next_poll(self, qapp, tmp_path):
        path = tmp_path / "jarvis_state"
        path.write_text("idle")
        states = _States()
        overlay = self._file_overlay(states, path)
        overlay.start()
        states.state = OrbState.IDLE
        overlay.poll()
        self._arm(qapp)
        states.state = OrbState.LISTENING
        path.write_text("listening")
        assert self._wait_for(qapp, lambda: overlay.windows())
        overlay.shutdown()

    def test_a_state_file_caught_empty_mid_write_is_not_read_until_it_has_content(self, qapp, tmp_path):
        """The daemon truncates and then writes: reading in between would see nothing and miss the wake."""
        path = tmp_path / "jarvis_state"
        path.write_text("idle")
        states = _States()
        reads = []

        def reader():
            reads.append(states.state)
            return states.state

        overlay = _overlay(reader, state_file=str(path), watch_interval_ms=5000)
        overlay.MAX_EMPTY_READS = 1000            # a writer that stays slow for the whole wait
        overlay.start()
        overlay.poll()
        self._arm(qapp)
        reads.clear()
        path.write_text("")                       # the truncation is reported on its own
        self._wait_for(qapp, lambda: False, seconds=0.25)
        assert reads == []                        # nothing was read from an empty file
        states.state = OrbState.LISTENING
        path.write_text("listening")
        assert self._wait_for(qapp, lambda: overlay.windows())
        overlay.shutdown()

    def test_a_missing_state_file_is_tolerated_and_the_poll_still_works(self, qapp, tmp_path):
        states = _States()
        overlay = self._file_overlay(states, tmp_path / "does_not_exist")
        overlay.start()
        _wake(overlay, states)
        assert overlay.windows()
        overlay.shutdown()

    def test_nothing_is_watched_while_the_effect_is_off_or_shut_down(self, qapp, tmp_path):
        path = tmp_path / "jarvis_state"
        path.write_text("idle")
        states = _States()
        overlay = self._file_overlay(states, path, enabled=False)
        overlay.start()
        assert not overlay.is_watching()
        assert not overlay._file_watch.files()
        overlay.set_enabled(True)
        assert overlay.is_watching()
        assert overlay._file_watch.files()
        overlay.set_enabled(False)
        assert not overlay._file_watch.files()
        overlay.set_enabled(True)
        overlay.shutdown()
        assert not overlay.is_watching()
        assert not overlay._file_watch.files()


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

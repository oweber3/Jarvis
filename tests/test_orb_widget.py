"""Behavioural tests for the reactive orb visual (model + widget)."""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop_app.orb_widget import (  # noqa: E402
    AudioLevelSource,
    OrbModel,
    OrbState,
    orb_state_for,
)


def _run(model, state, seconds, level=None, fps=30):
    for _ in range(int(seconds * fps)):
        model.step(1.0 / fps, state, level)


class TestStateMapping:
    def test_every_jarvis_state_maps_to_an_orb_state(self):
        from desktop_app.face_widget import JarvisState

        for js in JarvisState:
            assert isinstance(orb_state_for(js.value), OrbState)

    def test_asleep_is_offline_and_unknown_is_offline(self):
        assert orb_state_for("asleep") is OrbState.OFFLINE
        assert orb_state_for("not-a-state") is OrbState.OFFLINE

    def test_dictation_processing_reads_as_thinking(self):
        assert orb_state_for("dictation_processing") is OrbState.THINKING


class TestAudioLevelSource:
    def test_reports_none_when_never_fed(self):
        assert AudioLevelSource().current(now=100.0) is None

    def test_fresh_level_is_returned_and_clamped(self):
        src = AudioLevelSource(max_age_s=0.3)
        src.push(5.0, now=10.0)
        assert src.current(now=10.1) == 1.0
        src.push(-1.0, now=10.2)
        assert src.current(now=10.25) == 0.0

    def test_stale_level_expires_to_none(self):
        src = AudioLevelSource(max_age_s=0.3)
        src.push(0.6, now=10.0)
        assert src.current(now=10.5) is None


class TestOrbModelBehaviour:
    def test_idle_is_dimmer_than_listening_and_speaking(self):
        idle, listening, speaking = OrbModel(), OrbModel(), OrbModel()
        _run(idle, OrbState.IDLE, 2)
        _run(listening, OrbState.LISTENING, 2)
        _run(speaking, OrbState.SPEAKING, 2)
        assert idle.glow < listening.glow < speaking.glow

    def test_offline_is_dimmer_than_idle(self):
        idle, offline = OrbModel(), OrbModel()
        _run(idle, OrbState.IDLE, 2)
        _run(offline, OrbState.OFFLINE, 2)
        assert offline.glow < idle.glow

    def test_listening_voice_follows_input_level(self):
        quiet, loud = OrbModel(), OrbModel()
        _run(quiet, OrbState.LISTENING, 1, level=0.02)
        _run(loud, OrbState.LISTENING, 1, level=0.9)
        assert loud.voice > quiet.voice + 0.5

    def test_voice_falls_back_when_level_stops(self):
        model = OrbModel()
        _run(model, OrbState.LISTENING, 1, level=0.9)
        peak = model.voice
        _run(model, OrbState.LISTENING, 1, level=0.0)
        assert model.voice < peak * 0.5

    def test_speaking_without_real_level_still_moves_with_a_voice(self):
        model = OrbModel()
        samples = []
        for _ in range(60):
            model.step(1.0 / 30, OrbState.SPEAKING, None)
            samples.append(model.voice)
        assert max(samples) > 0.3
        assert max(samples) - min(samples[20:]) > 0.1      # it rises and falls like speech

    def test_idle_ignores_sound(self):
        model = OrbModel()
        _run(model, OrbState.IDLE, 2, level=0.9)
        assert model.voice < 0.05

    def test_thinking_is_visually_distinct_from_listening(self):
        thinking, listening = OrbModel(), OrbModel()
        _run(thinking, OrbState.THINKING, 2, level=0.5)
        _run(listening, OrbState.LISTENING, 2, level=0.5)
        assert thinking.think > listening.think + 0.5
        assert thinking.voice < listening.voice

    def test_model_values_stay_in_unit_range(self):
        model = OrbModel()
        for state in OrbState:
            _run(model, state, 1, level=2.0)
            assert 0.0 <= model.glow <= 1.0
            assert 0.0 <= model.voice <= 1.0
            assert 0.0 <= model.think <= 1.0

    def test_huge_frame_gap_does_not_blow_up(self):
        model = OrbModel()
        model.step(30.0, OrbState.SPEAKING, 1.0)
        assert 0.0 <= model.glow <= 1.0
        assert 0.0 <= model.flash <= 1.0


class TestParticleSphere:
    """The orb is a sphere of evenly spread points of light whose whole surface moves with the voice."""

    def _radius(self, frame):
        return np.hypot(frame.x, frame.y)

    def test_the_point_set_is_fixed_and_light(self):
        from desktop_app.orb_widget import SPHERE_POINTS, build_sphere

        a, b = build_sphere(), build_sphere()
        assert np.array_equal(a.position, b.position)
        assert len(a.position) == SPHERE_POINTS <= 5000      # kept light on the CPU
        model = OrbModel()
        first = model.sphere_frame()
        _run(model, OrbState.SPEAKING, 0.5)
        assert len(model.sphere_frame().x) == len(first.x)

    def test_points_are_spread_evenly_over_the_surface(self):
        from desktop_app.orb_widget import build_sphere

        outer = build_sphere().position
        outer = outer[np.linalg.norm(outer, axis=1) > 0.99]
        # Equal-area bands of the sphere hold about the same number of points: no clumps, no gaps.
        counts, _ = np.histogram(outer[:, 1], bins=10, range=(-1, 1))
        assert counts.max() <= counts.min() * 1.05

    def test_a_dimmer_inner_sphere_fills_the_middle(self):
        model = OrbModel()
        _run(model, OrbState.IDLE, 1)
        f = model.sphere_frame()
        r = self._radius(f)
        inner = f.inner.astype(bool)
        assert inner.sum() >= 500
        assert r[inner].max() < 0.75
        assert f.light[inner].mean() < f.light[~inner].mean()

    def test_the_rim_is_brighter_than_the_middle(self):
        model = OrbModel()
        _run(model, OrbState.IDLE, 1)
        f = model.sphere_frame()
        r = self._radius(f)
        outer = ~f.inner.astype(bool)
        assert f.light[outer & (r > 0.85)].mean() > f.light[outer & (r < 0.5)].mean() * 1.5

    def test_the_back_shows_through_dimmer(self):
        model = OrbModel()
        _run(model, OrbState.LISTENING, 1)
        f = model.sphere_frame()
        front, back = f.light[f.depth > 0.3], f.light[f.depth < -0.3]
        assert len(back) and back.mean() > 0.02
        assert front.mean() > back.mean() * 1.5

    def test_turns_slowly_at_rest_and_faster_when_thinking(self):
        resting, thinking = OrbModel(), OrbModel()
        _run(resting, OrbState.IDLE, 2)
        _run(thinking, OrbState.THINKING, 2)
        assert 0 < resting.spin < thinking.spin

    def test_the_whole_surface_swells_and_ripples_with_the_voice(self):
        quiet, loud = OrbModel(), OrbModel()
        _run(quiet, OrbState.LISTENING, 1, level=0.02)
        _run(loud, OrbState.LISTENING, 1, level=0.9)
        fq, fl = quiet.sphere_frame(), loud.sphere_frame()
        outer_q, outer_l = ~fq.inner.astype(bool), ~fl.inner.astype(bool)
        rq, rl = self._radius(fq)[outer_q], self._radius(fl)[outer_l]
        assert np.percentile(rl, 99) > np.percentile(rq, 99) * 1.08      # it bulges outward
        assert fl.light.mean() > fq.light.mean()                         # and brightens
        # The silhouette is reshaped, not just scaled: the rim's radius varies round the outline.
        def rim_spread(f, outer):
            angle = np.arctan2(f.y, f.x)[outer]
            r = self._radius(f)[outer]
            sector = ((angle + np.pi) / (2 * np.pi) * 36).astype(int) % 36
            return np.std([r[sector == s].max() for s in range(36)])
        assert rim_spread(fl, outer_l) > rim_spread(fq, outer_q) * 2

    def test_the_surface_keeps_moving_while_the_voice_holds_steady(self):
        model = OrbModel()
        _run(model, OrbState.LISTENING, 1, level=0.7)
        a = model.sphere_frame()
        _run(model, OrbState.LISTENING, 0.3, level=0.7)
        b = model.sphere_frame()
        assert np.abs(self._radius(a) - self._radius(b)).mean() > 0.005

    def test_thinking_sweeps_a_band_of_light_over_the_sphere(self):
        model = OrbModel()
        _run(model, OrbState.THINKING, 2)
        bright = []
        for _ in range(4):
            f = model.sphere_frame()
            outer = ~f.inner.astype(bool)
            top = np.argsort(f.light[outer])[-200:]
            bright.append(np.median(f.y[outer][top]))
            _run(model, OrbState.THINKING, 0.25)
        assert np.ptp(bright) > 0.3          # the brightest part of the sphere moves


class TestWakeFlash:
    def test_waking_flashes_once_and_fades(self):
        model = OrbModel()
        _run(model, OrbState.IDLE, 2)
        assert model.flash < 0.01
        _run(model, OrbState.LISTENING, 0.1)
        assert model.flash > 0.5
        _run(model, OrbState.LISTENING, 2.5)
        assert model.flash < 0.05

    def test_every_resting_to_active_change_flashes(self):
        for resting in (OrbState.OFFLINE, OrbState.IDLE, OrbState.MUTED, OrbState.ERROR):
            for active in (OrbState.LISTENING, OrbState.THINKING, OrbState.SPEAKING, OrbState.DICTATING):
                model = OrbModel()
                _run(model, resting, 1)
                _run(model, active, 0.1)
                assert model.flash > 0.5, (resting, active)

    def test_moving_between_active_states_never_flashes(self):
        model = OrbModel()
        _run(model, OrbState.IDLE, 1)
        _run(model, OrbState.LISTENING, 3)
        for state in (OrbState.THINKING, OrbState.SPEAKING, OrbState.LISTENING):
            _run(model, state, 0.1)
            assert model.flash < 0.05, state

    def test_staying_at_rest_never_flashes(self):
        model = OrbModel()
        for state in (OrbState.OFFLINE, OrbState.IDLE, OrbState.MUTED, OrbState.ERROR):
            _run(model, state, 0.5)
            assert model.flash < 0.01


class TestSharedLook:
    def test_every_state_has_distinct_enough_colours(self):
        from desktop_app.orb_widget import state_colours
        from desktop_app.themes import ORB_PALETTE
        for state in OrbState:
            colour, accent = state_colours(state)
            assert colour in ORB_PALETTE.values()
            assert accent in ORB_PALETTE.values()
        assert state_colours(OrbState.THINKING)[0] != state_colours(OrbState.LISTENING)[0]

    def test_synthetic_speech_envelope_varies_and_stays_in_range(self):
        from desktop_app.orb_widget import synthetic_speech_level
        samples = [synthetic_speech_level(t / 30) for t in range(90)]
        assert all(0.0 <= s <= 1.0 for s in samples)
        assert max(samples) - min(samples) > 0.2


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pixels_lit(widget):
    image = widget.grab().toImage()
    lit = 0
    for x in range(0, image.width(), 4):
        for y in range(0, image.height(), 4):
            c = image.pixelColor(x, y)
            if c.red() + c.green() + c.blue() > 90:
                lit += 1
    return lit


class TestOrbWidget:
    def test_renders_something_and_resizes(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget(audio_source=AudioLevelSource())
        w.set_state_override(OrbState.LISTENING)
        for _ in range(30):                 # let the listening glow come up
            w.tick(1.0 / 30)
        w.resize(300, 300)
        small = _pixels_lit(w)
        w.resize(640, 640)
        large = _pixels_lit(w)
        assert small > 0
        assert large > small

    def test_non_square_sizes_paint_without_error(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget()
        for size in [(200, 600), (800, 240), (1, 1)]:
            w.resize(*size)
            w.grab()

    def test_animation_only_ticks_while_visible(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget()
        assert not w.is_animating()
        w.show()
        assert w.is_animating()
        w.hide()
        assert not w.is_animating()

    def test_a_resting_orb_animates_at_a_lower_frame_rate_than_an_active_one(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget(audio_source=AudioLevelSource())
        w.set_state_override(OrbState.IDLE)
        w.show()
        for _ in range(90):
            w.tick(1.0 / 30)
        resting = w.frame_interval_ms()
        w.set_state_override(OrbState.LISTENING)
        w.tick(1.0 / 30)
        active = w.frame_interval_ms()
        assert w.is_animating()
        assert resting > active
        assert 1000 / active >= 30          # active states stay smooth
        w.hide()

    def test_override_wins_over_daemon_state(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget()
        w.set_state_override(OrbState.ERROR)
        w.tick(1.0 / 30)
        assert w.orb_state is OrbState.ERROR
        w.set_state_override(None)
        w.tick(1.0 / 30)
        assert w.orb_state is not OrbState.ERROR

    def test_states_render_differently(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        frames = {}
        for state in (OrbState.OFFLINE, OrbState.LISTENING, OrbState.THINKING, OrbState.ERROR):
            w = OrbWidget()
            w.resize(320, 320)
            w.set_state_override(state)
            for _ in range(45):
                w.tick(1.0 / 30)
            frames[state] = _pixels_lit(w)
        assert frames[OrbState.OFFLINE] < frames[OrbState.LISTENING]
        assert len(set(frames.values())) > 1

    def test_caption_replaces_the_state_label_in_the_footer(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget()
        w.resize(320, 320)
        w.set_state_override(OrbState.THINKING)
        w.tick(0.0)

        def footer():
            image = w.grab().toImage()
            return image.copy(0, image.height() - 44, image.width(), 44)

        default = footer()
        w.set_caption("starting up")
        w.tick(0.0)
        assert footer() != default
        w.set_caption(None)
        w.tick(0.0)
        assert footer() == default

    def test_no_hot_core_and_the_rim_is_the_brightest_band(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget(audio_source=AudioLevelSource())
        w.resize(320, 360)
        w.set_state_override(OrbState.SPEAKING)
        for _ in range(60):
            w.tick(1.0 / 30)
        image = w.grab().toImage()
        footer, header = min(40.0, 360 * 0.14), min(34.0, 360 * 0.12)
        cx, cy = 160, header + (360 - header - footer) / 2
        radius = min(320, 360 - header - footer) / 2 * 0.80

        def brightness(x, y):
            c = image.pixelColor(int(x), int(y))
            return c.red() + c.green() + c.blue()

        def ring(r0, r1):
            values = [brightness(x, y) for x in range(0, 320, 2) for y in range(0, 360, 2)
                      if r0 * radius <= ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 < r1 * radius]
            return sum(values) / len(values), max(values)

        centre_mean, centre_peak = ring(0.0, 0.15)
        middle_mean, _ = ring(0.0, 0.45)
        rim_mean, _ = ring(0.8, 1.0)
        assert centre_peak < 600          # no white-hot core
        assert middle_mean > 25           # the middle is filled with light
        assert rim_mean > middle_mean     # but the rim is the brightest band

    def test_a_frame_stays_cheap_to_paint(self, qapp):
        from PyQt6.QtGui import QImage
        from desktop_app.orb_widget import OrbWidget

        w = OrbWidget(audio_source=AudioLevelSource())
        w.resize(380, 440)
        w.set_state_override(OrbState.SPEAKING)
        image = QImage(380, 440, QImage.Format.Format_ARGB32_Premultiplied)
        for _ in range(5):
            w.tick(1.0 / 30)
            w.render(image)
        start = time.perf_counter()
        for _ in range(20):
            w.tick(1.0 / 30)
            w.render(image)
        assert (time.perf_counter() - start) / 20 < 0.015

    def test_wake_flash_brightens_the_orb(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        def lit_after(seconds):
            w = OrbWidget(audio_source=AudioLevelSource())
            w.resize(320, 360)
            w.set_state_override(OrbState.IDLE)
            for _ in range(60):
                w.tick(1.0 / 30)
            w.set_state_override(OrbState.LISTENING)
            for _ in range(int(seconds * 30)):
                w.tick(1.0 / 30)
            return _pixels_lit(w)

        assert lit_after(0.25) > lit_after(3.0)

    def test_injected_audio_source_drives_the_voice(self, qapp):
        from desktop_app.orb_widget import OrbWidget

        src = AudioLevelSource()
        w = OrbWidget(audio_source=src)
        w.set_state_override(OrbState.LISTENING)
        for _ in range(20):
            src.push(0.9, now=time.monotonic())
            w.tick(1.0 / 30)
        assert w.model.voice > 0.5


class TestFaceWindowHostsOrb:
    def test_face_window_contains_orb_and_keeps_launch_api(self, qapp):
        from desktop_app.face_widget import FaceWindow
        from desktop_app.orb_widget import OrbWidget

        win = FaceWindow()
        assert isinstance(win.orb, OrbWidget)
        for name in ("show", "raise_", "activateWindow", "_position_on_right"):
            assert callable(getattr(win, name))

"""Regression hunt on recent develop changes: each test reproduces a defect and fails until it is fixed."""
import json

import pytest


@pytest.fixture
def settings_window(qapp, tmp_path, monkeypatch):
    """Open the Settings window on a config file in a temporary folder (never the user's)."""
    opened = []

    def open_with(values):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"_config_version": 6, **values}))
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
        monkeypatch.setattr("desktop_app.settings_window.get_input_devices", lambda: [("", "System Default")])
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.warning", lambda *a, **k: None)
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        opened.append(win)
        return win, cfg

    yield open_with
    for win in opened:
        win.close()


def _choose(combo, value):
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)


@pytest.mark.unit
def test_claude_effort_model_default_survives_save_and_load(settings_window, monkeypatch):
    """Choosing Claude's "Model default" effort in Settings must make Claude run with the model's default
    effort. The window saves it as JSON null, which the loader turns back into the "low" default, while the
    window keeps showing "Model default"."""
    win, cfg = settings_window({"claude_enabled": True})
    _choose(win._widgets["claude_effort"], "")
    win._on_save()

    from jarvis.config import load_settings
    settings = load_settings()
    assert settings.claude_effort == "", (
        f"Settings shows 'Model default' but Claude runs with effort {settings.claude_effort!r}; "
        f"config.json holds {json.loads(cfg.read_text()).get('claude_effort')!r}")


@pytest.mark.unit
def test_an_emptied_wake_word_does_not_make_every_utterance_wake_jarvis(settings_window):
    """Clearing the Wake Word box and saving writes an empty wake word, which matches any speech, so every
    conversation in the room becomes a request. An empty wake word must keep the default (or be refused)."""
    win, _cfg = settings_window({})
    win._widgets["wake_word"].setText("")
    win._on_save()

    from jarvis.config import load_settings
    from jarvis.listening.wake_detection import is_wake_word_detected
    settings = load_settings()
    assert not is_wake_word_detected("what should we have for dinner tonight", settings.wake_word,
                                     settings.wake_aliases, settings.wake_fuzzy_ratio), (
        f"wake word saved as {settings.wake_word!r}: any speech wakes Jarvis")


@pytest.mark.unit
def test_a_slow_camera_with_the_hand_always_in_view_still_aims_the_pointer():
    """Spec: the mode pauses only when the hand stays out of view for 0.25 s. A webcam in dim light can drop to
    under 4 frames a second; with the hand in every frame, one finger must still move the pointer. Instead the
    watchdog declares the hand lost between frames, so no gesture ever lasts the debounce time."""
    from jarvis.gestures.window_snap import WindowSnap, WindowSnapSettings
    from tests.synthetic_hands import FakeDesktop, frame, hand

    now = [0.0]
    desk = FakeDesktop()
    snap = WindowSnap(desk, WindowSnapSettings(), clock=lambda: now[0])
    period = 0.3  # about 3.3 frames a second
    tick = 0.025  # the watchdog runs every 50 ms on its own clock, as WindowSnapService does
    for index in range(12):
        t = index * period
        while tick < t:
            now[0] = tick
            snap.tick()
            tick += 0.05
        now[0] = t
        snap.on_frame(frame(t, hand(0.4 + index * 0.02, 0.5, gesture="one")))
    assert desk.of("pointer"), f"the pointer never moved; gesture snapping state is {snap.state!r}"


class _FakeTracker:
    """A hand tracker that never opens a camera; it records which camera it was built for."""

    def __init__(self, camera):
        self.camera = camera
        self.subscribers = []

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback) if callback in self.subscribers else None

    def start(self):
        return True

    def stop(self):
        pass


class _FakeWindowsOutput:
    """Stands in for the Windows desktop: one monitor, nothing is ever moved."""

    def __init__(self, *args, **kwargs):
        pass

    def desktop(self):
        return [(0, 0, 1920, 1080)]


@pytest.mark.unit
def test_gesture_settings_apply_after_jarvis_restarts_in_the_same_process(monkeypatch):
    """The installed app runs the daemon in a thread and "Restart Jarvis" runs ``daemon.main`` again in the same
    process. Gesture settings saved before the restart (here the camera) must be the ones the next start uses."""
    from types import SimpleNamespace

    import jarvis.platform.windows.window_snap as windows_output
    from jarvis.gestures import window_snap_service
    from jarvis.tools import registry

    built_for = []

    def fake_snap_tracker(cfg, camera, **kwargs):
        built_for.append(camera)
        return _FakeTracker(camera)

    monkeypatch.setattr(windows_output, "WindowsSnapOutput", _FakeWindowsOutput)
    monkeypatch.setattr(window_snap_service, "snap_tracker", fake_snap_tracker)
    monkeypatch.setattr(window_snap_service, "_service", None)
    monkeypatch.setattr(registry, "BUILTIN_TOOLS", dict(registry.BUILTIN_TOOLS))

    # First daemon start: camera 0. Then the daemon shuts down (daemon.main's cleanup).
    registry.configure_gesture_tool(SimpleNamespace(gesture_snap_enabled=True, gesture_snap_camera=0),
                                    platform="win32")
    window_snap_service.stop_service()

    # The user picks camera 2 in Settings and restarts Jarvis: daemon.main configures the tools again.
    registry.configure_gesture_tool(SimpleNamespace(gesture_snap_enabled=True, gesture_snap_camera=2),
                                    platform="win32")
    service = window_snap_service.get_service()
    try:
        assert service.start() == "started"
    finally:
        service.stop()
    assert built_for == [2], f"gesture snapping after the restart used camera {built_for}, not the saved camera 2"

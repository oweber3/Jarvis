"""DaemonBackend: what phone access sees of the real daemon, confirmation store and assistant state."""
import pytest

from jarvis import assistant_state
from jarvis.remote.backend import DaemonBackend
from jarvis.tools.confirmation import ConfirmationRequest, ConfirmationStore, SafetyTier


def request(tier=SafetyTier.CONFIRM_DIALOG):
    return ConfirmationRequest(tool_name="fileOps", tier=tier, action="delete", target="notes.txt",
                               parameters={"path": "notes.txt"}, consequence="The file is removed.")


class Handle:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def store():
    store = ConfirmationStore()
    yield store
    store.clear_pending()


@pytest.mark.unit
class TestConfirmations:
    def test_a_desktop_dialog_request_is_described(self, store):
        req = request()
        store.begin_dialog(req, {}, lambda r, resolve: Handle())
        pending = DaemonBackend(confirmation_store=store).pending_confirmation()
        assert pending["id"] == req.id
        assert (pending["tool"], pending["action"], pending["target"]) == ("fileOps", "delete", "notes.txt")
        assert pending["consequence"] == "The file is removed."
        assert 0 < pending["expires_in"] <= 60

    def test_a_spoken_yes_or_no_request_is_not_offered(self, store):
        store.set_pending(request(SafetyTier.CONFIRM_VOICE))
        assert DaemonBackend(confirmation_store=store).pending_confirmation() is None

    def test_denying_from_the_phone_closes_the_desktop_dialog(self, store):
        req, handle = request(), Handle()
        store.begin_dialog(req, {}, lambda r, resolve: handle)
        assert DaemonBackend(confirmation_store=store).resolve_confirmation(req.id, False) is True
        assert handle.closed
        assert store.get_pending() is None

    def test_approving_from_the_phone_schedules_the_action(self, store, monkeypatch):
        scheduled = []
        monkeypatch.setattr(store, "schedule", lambda action, **kw: scheduled.append(action.request.id))
        req, handle = request(), Handle()
        store.begin_dialog(req, {}, lambda r, resolve: handle)
        assert DaemonBackend(confirmation_store=store).resolve_confirmation(req.id, True) is True
        assert scheduled == [req.id] and handle.closed

    def test_a_stale_answer_is_ignored(self, store):
        assert DaemonBackend(confirmation_store=store).resolve_confirmation("gone", True) is False


@pytest.mark.unit
class TestDaemonState:
    def test_pc_use_is_in_percent(self):
        stats = DaemonBackend().pc_stats()
        if stats is not None:
            assert 0 <= stats["cpu"] <= 100 and 0 <= stats["ram"] <= 100

    def test_busy_while_a_query_runs(self, monkeypatch):
        from jarvis import daemon
        monkeypatch.setattr(daemon, "is_query_running", lambda: True)
        assistant_state.reset()
        assert DaemonBackend().is_busy() is True

    @pytest.mark.parametrize("state", [assistant_state.AssistantState.LISTENING,
                                       assistant_state.AssistantState.SPEAKING])
    def test_busy_while_jarvis_listens_or_speaks_at_the_pc(self, monkeypatch, state):
        from jarvis import daemon
        monkeypatch.setattr(daemon, "is_query_running", lambda: False)
        assistant_state.set_state(state)
        try:
            assert DaemonBackend().is_busy() is True
        finally:
            assistant_state.reset()

    def test_not_busy_when_idle(self, monkeypatch):
        from jarvis import daemon
        monkeypatch.setattr(daemon, "is_query_running", lambda: False)
        assistant_state.set_state(assistant_state.AssistantState.IDLE)
        try:
            assert DaemonBackend().is_busy() is False
        finally:
            assistant_state.reset()


class Engine:
    """A dictation engine stand-in."""

    def __init__(self, blocker=None, text="hello"):
        self.blocker, self.text, self.calls = blocker, text, []

    def transcribe_blocker(self):
        return self.blocker

    def transcribe_samples(self, samples, rate):
        self.calls.append((len(samples), rate))
        return self.text


@pytest.mark.unit
class TestSpeechIn:
    def settings(self, monkeypatch, **values):
        from types import SimpleNamespace
        from jarvis import daemon
        monkeypatch.setattr(daemon, "get_settings", lambda: SimpleNamespace(**values))

    def test_dictation_off_in_settings_is_disabled(self, monkeypatch):
        from jarvis import daemon
        self.settings(monkeypatch, dictation_enabled=False)
        monkeypatch.setattr(daemon, "get_dictation_engine", lambda: Engine())
        assert DaemonBackend().voice_reason() == "disabled"

    def test_no_dictation_engine_is_unavailable(self, monkeypatch):
        from jarvis import daemon
        self.settings(monkeypatch, dictation_enabled=True)
        monkeypatch.setattr(daemon, "get_dictation_engine", lambda: None)
        assert DaemonBackend().voice_reason() == "unavailable"

    def test_the_engines_own_reason_is_passed_on(self, monkeypatch):
        from jarvis import daemon
        self.settings(monkeypatch, dictation_enabled=True)
        monkeypatch.setattr(daemon, "get_dictation_engine", lambda: Engine("not_ready"))
        assert DaemonBackend().voice_reason() == "not_ready"
        monkeypatch.setattr(daemon, "get_dictation_engine", lambda: Engine())
        assert DaemonBackend().voice_reason() is None

    def test_audio_is_transcribed_at_16k(self, monkeypatch):
        from jarvis import daemon
        engine = Engine(text="what time is it")
        monkeypatch.setattr(daemon, "get_dictation_engine", lambda: engine)
        assert DaemonBackend().transcribe([0.0] * 16000) == "what time is it"
        assert engine.calls == [(16000, 16000)]


class Voice:
    """A voice engine stand-in that renders and records that it never plays."""

    def __init__(self, samples=None, rate=22050):
        import numpy as np
        self.samples = np.array([1, -2, 3], dtype=np.int16) if samples is None else samples
        self.rate, self.rendered, self.spoken = rate, [], []

    def render(self, text):
        self.rendered.append(text)
        return self.samples, self.rate

    def speak(self, text, *a, **k):
        self.spoken.append(text)


@pytest.mark.unit
class TestSpeechOut:
    def test_the_live_engine_renders_and_nothing_is_played_at_the_pc(self, monkeypatch):
        from jarvis import daemon
        import numpy as np
        voice = Voice()
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: voice)
        assert DaemonBackend().render_speech("hello") == (np.array([1, -2, 3], dtype="<i2").tobytes(), 22050)
        assert voice.rendered == ["hello"] and voice.spoken == []

    def test_a_voice_that_made_no_sound_gives_none(self, monkeypatch):
        import numpy as np
        from jarvis import daemon
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: Voice(np.zeros(0, dtype=np.int16)))
        assert DaemonBackend().render_speech("hello") is None

    def test_a_voice_that_could_not_load_gives_none(self, monkeypatch):
        from jarvis import daemon
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: Voice(samples=None))
        voice = Voice()
        voice.render = lambda text: (None, 22050)
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: voice)
        assert DaemonBackend().render_speech("hello") is None

    def test_no_engine_at_all_gives_none(self, monkeypatch):
        from types import SimpleNamespace
        from jarvis import daemon
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: None)
        monkeypatch.setattr(daemon, "get_settings", lambda: None)
        assert DaemonBackend().render_speech("hello") is None

    def test_an_engine_that_cannot_render_is_replaced_by_a_piper_voice_already_on_disk(self, monkeypatch):
        from types import SimpleNamespace
        from jarvis import daemon
        from jarvis.output import piper_voices, tts
        chatterbox = SimpleNamespace(speak=lambda *a, **k: None)   # no render
        cfg = SimpleNamespace(tts_piper_model_path="", tts_piper_voice="en_GB-alan-medium", tts_piper_speaker=None,
                              tts_piper_length_scale=0.65, tts_piper_noise_scale=0.8, tts_piper_noise_w=1.0)
        piper = Voice()
        made = []
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: chatterbox)
        monkeypatch.setattr(daemon, "get_settings", lambda: cfg)
        monkeypatch.setattr(piper_voices, "piper_voice_downloaded", lambda key: True)
        monkeypatch.setattr(tts, "create_tts_engine", lambda **kw: made.append(kw) or piper)
        backend = DaemonBackend()
        assert backend.render_speech("hello") is not None
        assert made and made[0]["engine"] == "piper" and piper.rendered == ["hello"]
        backend.render_speech("again")
        assert len(made) == 1   # built once

    def test_a_missing_piper_voice_is_never_downloaded_for_a_render(self, monkeypatch):
        from types import SimpleNamespace
        from jarvis import daemon
        from jarvis.output import piper_voices, tts
        cfg = SimpleNamespace(tts_piper_model_path="", tts_piper_voice="en_GB-alan-medium")
        monkeypatch.setattr(daemon, "get_tts_engine", lambda: SimpleNamespace())
        monkeypatch.setattr(daemon, "get_settings", lambda: cfg)
        monkeypatch.setattr(piper_voices, "piper_voice_downloaded", lambda key: False)
        monkeypatch.setattr(tts, "create_tts_engine", lambda **kw: pytest.fail("a voice must not be built"))
        assert DaemonBackend().render_speech("hello") is None

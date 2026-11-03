"""Phone voice: a spoken request in, the reply as one-shot audio out, and nothing kept or logged.

The daemon, the speech model and the voice are fakes; the hub and store are real. See ``remote/remote.spec.md``,
Voice and Privacy of audio.
"""
import io
import time
import wave

import numpy as np
import pytest

from fake_remote_backend import FakeRemoteBackend
from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ChatStore
from jarvis.remote import voice as voice_module
from jarvis.remote.voice import (AUDIO_TTL_SEC, MAX_CLIPS_PER_DEVICE, MAX_SPEECH_CHARS, MAX_STORE_BYTES,
                                 MAX_VOICE_BYTES, MIN_VOICE_BYTES, AudioStore, PhoneVoice, wav_bytes)
from jarvis.webchat.hub import ChatHub

ONE_SECOND = b"\x10\x00" * 16000


@pytest.fixture
def chat_backend():
    return FakeWebChatBackend()


@pytest.fixture
def hub(chat_backend, tmp_path):
    store = ChatStore(str(tmp_path / "jarvis.db"))
    h = ChatHub(chat_backend, store, sync_interval_sec=0.05)
    h.sync()
    yield h
    h.stop()
    store.close()


@pytest.fixture
def backend():
    return FakeRemoteBackend()


@pytest.fixture
def voice(backend, hub):
    return PhoneVoice(backend, hub, inline=True)


@pytest.fixture
def logs(monkeypatch):
    lines = []
    monkeypatch.setattr(voice_module, "debug_log", lambda message, *a, **k: lines.append(message))
    return lines


@pytest.mark.unit
class TestWav:
    def test_it_is_a_mono_16_bit_wav_at_the_given_rate(self):
        data = wav_bytes(b"\x01\x00\x02\x00", 22050)
        with wave.open(io.BytesIO(data)) as clip:
            assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), clip.getnframes()) == (1, 2, 22050, 2)
            assert clip.readframes(2) == b"\x01\x00\x02\x00"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.unit
class TestAudioStore:
    def test_clips_are_found_by_a_random_token_once(self):
        store = AudioStore()
        token = store.put("dev", b"WAV")
        assert len(token) == 32 and int(token, 16) >= 0
        assert store.take(token, "dev") == b"WAV"
        assert store.take(token, "dev") is None

    def test_another_device_cannot_take_it(self):
        store = AudioStore()
        token = store.put("dev", b"WAV")
        assert store.take(token, "other") is None
        assert store.take(token, "dev") == b"WAV"  # the failed attempt did not spend it

    def test_an_unknown_token_is_nothing(self):
        assert AudioStore().take("0" * 32, "dev") is None

    def test_a_clip_expires(self):
        clock = Clock()
        store = AudioStore(clock=clock)
        token = store.put("dev", b"WAV")
        clock.now += AUDIO_TTL_SEC + 1
        assert store.take(token, "dev") is None

    def test_a_device_keeps_only_its_newest_clips(self):
        store = AudioStore()
        tokens = [store.put("dev", bytes([i])) for i in range(MAX_CLIPS_PER_DEVICE + 2)]
        assert store.take(tokens[0], "dev") is None and store.take(tokens[1], "dev") is None
        assert store.take(tokens[-1], "dev") == bytes([MAX_CLIPS_PER_DEVICE + 1])

    def test_the_store_is_bounded_in_bytes(self):
        store = AudioStore()
        big = b"x" * (MAX_STORE_BYTES // 3 + 1)
        tokens = [store.put(f"dev{i}", big) for i in range(4)]
        assert store.take(tokens[0], "dev0") is None
        assert store.take(tokens[-1], "dev3") == big

    def test_a_clip_over_the_whole_budget_is_not_kept(self):
        assert AudioStore().put("dev", b"x" * (MAX_STORE_BYTES + 1)) is None


@pytest.mark.unit
class TestAvailability:
    def test_plain_http_has_no_voice(self, voice):
        assert voice.availability(secure=False) == {"available": False, "reason": "no_https"}

    @pytest.mark.parametrize("reason", ["disabled", "unavailable", "not_ready"])
    def test_the_speech_models_reason_is_passed_on(self, voice, backend, reason):
        backend.voice_blocker = reason
        assert voice.availability(secure=True) == {"available": False, "reason": reason}

    def test_https_with_a_ready_model_is_available(self, voice):
        assert voice.availability(secure=True) == {"available": True, "reason": None}


@pytest.mark.unit
class TestSpokenRequest:
    def speak(self, voice, pcm=ONE_SECOND, secure=True, device="dev"):
        return voice.handle_audio(device, pcm, secure=secure)

    def test_the_audio_is_transcribed_and_sent_as_a_spoken_phone_request(self, voice, backend, chat_backend, hub):
        status, payload = self.speak(voice)
        assert status == 202 and payload["text"] == "what time is it" and payload["status"] == "accepted"
        assert chat_backend.submitted == ["what time is it"]
        assert chat_backend.origins == ["phone"]
        stored = hub.store.messages(hub.store.get_active_chat_id())
        assert [(m.role, m.source) for m in stored] == [("user", "voice"), ("assistant", "voice")]

    def test_the_samples_reach_the_model_as_16k_floats(self, voice, backend):
        self.speak(voice, np.array([0, 16384, -16384, 32767], dtype="<i2").tobytes() + ONE_SECOND)
        samples = backend.transcribed[0]
        assert samples.dtype == np.float32 and list(samples[:3]) == [0.0, 0.5, -0.5]

    def test_plain_http_is_refused_before_any_work(self, voice, backend):
        assert self.speak(voice, secure=False) == (503, {"error": "no_https"})
        assert backend.transcribed == []

    def test_a_model_that_cannot_listen_is_refused_with_its_reason(self, voice, backend):
        backend.voice_blocker = "not_ready"
        assert self.speak(voice) == (503, {"error": "not_ready"})
        assert backend.transcribed == []

    def test_a_busy_jarvis_is_refused_before_transcribing(self, voice, backend):
        backend.busy = True
        assert self.speak(voice) == (409, {"error": "busy"})
        assert backend.transcribed == []

    def test_too_short_and_odd_lengths_are_refused(self, voice, backend):
        assert self.speak(voice, b"\x00\x00" * (MIN_VOICE_BYTES // 2 - 1)) == (400, {"error": "too_short"})
        assert self.speak(voice, ONE_SECOND + b"\x00") == (400, {"error": "bad_audio"})
        assert backend.transcribed == []

    def test_too_long_is_refused(self, voice, backend):
        assert self.speak(voice, b"\x00" * (MAX_VOICE_BYTES + 2)) == (413, {"error": "too_large"})
        assert backend.transcribed == []

    def test_nothing_heard_starts_no_request(self, voice, backend, chat_backend):
        backend.transcript = ""
        assert self.speak(voice) == (200, {"status": "nothing_heard", "text": ""})
        assert chat_backend.submitted == []

    def test_a_request_while_jarvis_became_busy_meanwhile_is_busy(self, voice, chat_backend):
        chat_backend.mode = "busy"
        status, payload = self.speak(voice)
        assert (status, payload["error"]) == (409, "busy")

    def test_a_daemon_that_is_not_ready_is_unavailable(self, voice, chat_backend):
        chat_backend.mode = "unavailable"
        status, payload = self.speak(voice)
        assert (status, payload["error"]) == (503, "unavailable")


@pytest.mark.unit
class TestReplyAudio:
    def speak(self, voice):
        status, payload = voice.handle_audio("dev", ONE_SECOND, secure=True)
        assert status == 202
        return payload["query_id"]

    def test_the_reply_is_rendered_and_offered_once_to_the_device(self, voice, backend, chat_backend):
        chat_backend.reply_text = "It is noon."
        query = self.speak(voice)
        entry = voice.audio_status("dev")[str(query)]
        assert entry["status"] == "ready" and entry["url"].startswith("/api/audio/")
        assert backend.rendered == ["It is noon."]
        clip = voice.fetch(entry["url"].rsplit("/", 1)[1], "dev")
        with wave.open(io.BytesIO(clip)) as wav:
            assert wav.getframerate() == 22050
        assert voice.fetch(entry["url"].rsplit("/", 1)[1], "dev") is None
        assert voice.audio_status("dev")[str(query)] == {"status": "none"}

    def test_another_device_sees_nothing_of_it(self, voice):
        self.speak(voice)
        assert voice.audio_status("someone-else") == {}

    def test_a_failed_request_has_no_audio(self, voice, chat_backend, backend):
        chat_backend.mode = "fail"
        status, payload = voice.handle_audio("dev", ONE_SECOND, secure=True)
        assert status == 202
        assert voice.audio_status("dev")[str(payload["query_id"])] == {"status": "none"}
        assert backend.rendered == []

    def test_no_voice_that_could_speak_means_none(self, voice, backend):
        backend.speech = None
        query = self.speak(voice)
        assert voice.audio_status("dev")[str(query)] == {"status": "none"}

    def test_a_voice_that_raises_means_none_and_never_breaks_the_reply(self, voice, backend, hub):
        backend.render_error = RuntimeError("boom")
        query = self.speak(voice)
        assert voice.audio_status("dev")[str(query)] == {"status": "none"}
        assert hub.store.messages(hub.store.get_active_chat_id())[-1].content == "Done."

    def test_a_long_reply_is_cut_at_a_sentence_end(self, voice, backend, chat_backend):
        chat_backend.reply_text = ("A sentence that goes on. " * 200).strip()
        self.speak(voice)
        spoken = backend.rendered[0]
        assert len(spoken) <= MAX_SPEECH_CHARS and spoken.endswith(".")

    def test_a_typed_request_asks_for_audio_the_same_way(self, voice, backend, hub):
        tracker = voice.track("dev")
        result = hub.submit("hello", origin="phone", on_finish=tracker.finished)
        tracker.accepted(result.query_id)
        assert voice.audio_status("dev")[str(result.query_id)]["status"] == "ready"

    def test_a_clip_nobody_fetches_goes_after_the_ttl(self, backend, hub):
        clock = Clock()
        voice = PhoneVoice(backend, hub, inline=True, clock=clock)
        status, payload = voice.handle_audio("dev", ONE_SECOND, secure=True)
        clock.now += AUDIO_TTL_SEC + 1
        assert voice.audio_status("dev") == {}

    def test_the_poll_is_woken_when_audio_is_ready(self, backend, hub):
        woken = []
        voice = PhoneVoice(backend, hub, inline=True, poke=lambda: woken.append(1))
        voice.handle_audio("dev", ONE_SECOND, secure=True)
        assert woken

    def test_synthesis_runs_off_the_replying_thread_by_default(self, backend, hub):
        voice = PhoneVoice(backend, hub)
        try:
            status, payload = voice.handle_audio("dev", ONE_SECOND, secure=True)
            deadline = time.time() + 5
            while voice.audio_status("dev").get(str(payload["query_id"]), {}).get("status") != "ready":
                assert time.time() < deadline
                time.sleep(0.02)
        finally:
            voice.shutdown()


@pytest.mark.unit
class TestPrivacy:
    def test_logs_hold_lengths_and_reasons_only(self, voice, backend, chat_backend, logs):
        backend.transcript = "my secret phrase about the bank"
        chat_backend.reply_text = "A private reply about the bank."
        voice.handle_audio("device-name-xyz", ONE_SECOND, secure=True)
        token = voice.audio_status("device-name-xyz")
        joined = "\n".join(logs)
        assert logs
        for forbidden in ("secret phrase", "private reply", "bank", "device-name-xyz"):
            assert forbidden not in joined
        for entry in token.values():
            if "url" in entry:
                assert entry["url"].rsplit("/", 1)[1] not in joined

    def test_nothing_is_written_to_disk(self, voice, tmp_path, monkeypatch):
        written = []
        real_open = open

        def spy(file, mode="r", *a, **k):
            if any(flag in str(mode) for flag in "wax+"):
                written.append(str(file))
            return real_open(file, mode, *a, **k)

        monkeypatch.setattr("builtins.open", spy)
        voice.handle_audio("dev", ONE_SECOND, secure=True)
        assert [path for path in written if not path.endswith(".db") and "jarvis.db" not in path] == []


@pytest.mark.unit
class TestAudioStatusFollowsTheClip:
    def test_an_entry_lives_as_long_as_its_clip_not_as_long_as_its_request(self, backend, hub):
        clock = Clock()
        voice = PhoneVoice(backend, hub, inline=True, clock=clock)
        original = backend.render_speech

        def slow_render(text):
            clock.now += AUDIO_TTL_SEC - 20   # a long reply: nearly the whole window passes while it is made
            return original(text)

        backend.render_speech = slow_render
        _status, payload = voice.handle_audio("dev", ONE_SECOND, secure=True)
        clock.now += 60   # the clip is 60 s old and still fetchable
        assert voice.audio_status("dev")[str(payload["query_id"])]["status"] == "ready"

    def test_a_clip_the_store_let_go_reads_as_nothing_to_play(self, voice, backend):
        queries = [voice.handle_audio("dev", ONE_SECOND, secure=True)[1]["query_id"]
                   for _ in range(MAX_CLIPS_PER_DEVICE + 2)]
        status = voice.audio_status("dev")
        assert status[str(queries[0])] == {"status": "none"}
        assert status[str(queries[-1])]["status"] == "ready"

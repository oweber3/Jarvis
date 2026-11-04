"""The voice can still be loading when the first reply arrives (it loads while Whisper does). That reply
waits for the load instead of starting the engine a second time."""

import threading
import time

from jarvis.output.tts import PiperTTS


class Output:
    name = "test output"

    def __init__(self, warmed):
        self.warmed = warmed

    def warm(self):
        self.warmed.append(threading.current_thread().name)


def test_a_reply_during_the_voice_load_waits_for_it_instead_of_starting_a_second_engine(monkeypatch):
    warmed, workers, spoken = [], [], []
    tts = PiperTTS(enabled=True, voice_outputs=[Output(warmed)])
    loading, release = threading.Event(), threading.Event()

    def slow_load():
        loading.set()
        release.wait(2)
        return True

    original_run = tts._run
    monkeypatch.setattr(tts, "_ensure_initialized", slow_load)
    monkeypatch.setattr(tts, "_speak_once", spoken.append)
    monkeypatch.setattr(tts, "_run", lambda: (workers.append(1), original_run()))

    starter = threading.Thread(target=tts.start, name="starting thread")
    starter.start()
    assert loading.wait(2)
    reply = threading.Thread(target=tts.speak, args=("hello",), name="reply thread")
    reply.start()
    time.sleep(0.1)
    release.set()
    starter.join(2)
    reply.join(2)
    deadline = time.monotonic() + 2
    while not spoken and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert len(workers) == 1, "one speaking thread, never two draining the same queue"
        assert warmed == ["starting thread"], "voice outputs warm once, on the starting thread"
        assert len(spoken) == 1
    finally:
        tts.stop()

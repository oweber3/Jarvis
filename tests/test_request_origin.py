"""Text requests say where they came from: the desktop chat window or a paired phone.

A phone request must never take the window in front of the PC as what it is about
(``memory/desktop_referents.spec.md``, Foreground window), so the reply engine needs to know."""
import threading

import pytest

from jarvis import daemon
from jarvis.memory.conversation import DialogueMemory


@pytest.fixture
def engine_origins(monkeypatch):
    monkeypatch.setattr(daemon, "_global_dialogue_memory", DialogueMemory())
    monkeypatch.setattr(daemon, "_global_cfg", object())
    monkeypatch.setattr(daemon, "_global_db", object())
    monkeypatch.setattr(daemon, "_global_stop_requested", False)
    seen = []
    done = threading.Event()

    def fake_engine(db, cfg, tts, text, dialogue_memory, language=None, quiet=False, origin=None, **_):
        seen.append((origin, quiet))
        done.set()
        return "ok"

    monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", fake_engine)
    yield seen, done
    for _ in range(100):  # the worker releases the shared query lock just after the engine returns
        if not daemon.is_query_running():
            break
        threading.Event().wait(0.02)


@pytest.mark.unit
def test_the_chat_window_submits_as_chat(engine_origins):
    seen, done = engine_origins
    daemon.submit_text_query("close this")
    assert done.wait(5)
    assert seen == [("chat", True)]


@pytest.mark.unit
def test_a_paired_phone_submits_as_phone(engine_origins):
    from jarvis.remote.backend import DaemonBackend

    seen, done = engine_origins
    DaemonBackend().submit("close this", on_start=None, on_complete=None, on_busy=None)
    assert done.wait(5)
    assert seen == [("phone", True)]

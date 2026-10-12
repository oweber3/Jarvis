"""Announcements reach every output: spoken through the listener's reply path, shown in chat, remembered."""
import json

from jarvis import daemon


class FakeListener:
    def __init__(self):
        self.spoken = []

    def speak_announcement(self, text):
        self.spoken.append(text)


class FakeMemory:
    def __init__(self):
        self.messages = []

    def add_message(self, role, text):
        self.messages.append((role, text))


def test_an_announcement_is_spoken_shown_in_chat_printed_and_remembered(monkeypatch, capsys):
    listener, memory, shown = FakeListener(), FakeMemory(), []
    monkeypatch.setattr(daemon, "_global_voice_listener", listener)
    monkeypatch.setattr(daemon, "_global_dialogue_memory", memory)
    monkeypatch.setattr(daemon, "_stdin_ipc_enabled", lambda: False)
    daemon.add_chat_result_listener(shown.append)
    try:
        daemon.deliver_announcement("Your pasta timer is done.")
    finally:
        daemon.remove_chat_result_listener(shown.append)
    assert listener.spoken == ["Your pasta timer is done."]
    assert shown == ["Your pasta timer is done."]
    assert memory.messages == [("assistant", "Your pasta timer is done.")]
    assert "⏰ Your pasta timer is done." in capsys.readouterr().out


def test_subprocess_chat_gets_a_notice_event(monkeypatch, capsys):
    monkeypatch.setattr(daemon, "_global_voice_listener", None)
    monkeypatch.setattr(daemon, "_global_dialogue_memory", None)
    monkeypatch.setattr(daemon, "_global_tts_engine", None)
    monkeypatch.setattr(daemon, "_stdin_ipc_enabled", lambda: True)
    daemon.deliver_announcement("Reminder: call the dentist.")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(daemon.CHAT_IPC_PREFIX)]
    assert [json.loads(line[len(daemon.CHAT_IPC_PREFIX):]) for line in lines] == [
        {"type": "notice", "data": "Reminder: call the dentist."}]


def test_without_a_listener_the_tts_engine_speaks_it(monkeypatch):
    spoken = []

    class FakeTts:
        enabled = True

        def speak(self, text, **kwargs):
            spoken.append(text)

    monkeypatch.setattr(daemon, "_global_voice_listener", None)
    monkeypatch.setattr(daemon, "_global_dialogue_memory", None)
    monkeypatch.setattr(daemon, "_global_tts_engine", FakeTts())
    monkeypatch.setattr(daemon, "_stdin_ipc_enabled", lambda: False)
    daemon.deliver_announcement("Your timer is done.")
    assert spoken == ["Your timer is done."]


def test_the_listener_speaks_announcements_through_its_reply_path():
    from jarvis.listening.listener import VoiceListener
    calls = []
    fake = type("L", (), {"_is_engaged": lambda self: False, "_pending_replies": (),
                          "_speak_reply": lambda self, text: calls.append(text)})()
    VoiceListener.speak_announcement(fake, "Your timer is done.")
    assert calls == ["Your timer is done."]


def test_the_chat_window_shows_a_notice_line():
    import pytest
    pytest.importorskip("PyQt6")
    from desktop_app.chat_window import ChatWindow
    received = []
    window = ChatWindow.__new__(ChatWindow)
    window.signals = type("S", (), {})()
    window.signals.confirmed_result = type("Sig", (), {"emit": lambda self, text: received.append(text)})()
    assert ChatWindow.process_ipc_line(window, daemon.CHAT_IPC_PREFIX + json.dumps(
        {"type": "notice", "data": "Your timer is done."})) is True
    assert received == ["Your timer is done."]


def test_an_announcement_during_a_request_is_queued_without_ending_it():
    from jarvis.listening.listener import VoiceListener
    spoken, replies = [], []

    class Tts:
        enabled = True

        def speak(self, text, **kwargs):
            spoken.append(text)

    fake = type("L", (), {"_is_engaged": lambda self: True, "_pending_replies": (), "tts": Tts(),
                          "track_tts_start": lambda self, text: None,
                          "_speak_reply": lambda self, text: replies.append(text)})()
    VoiceListener.speak_announcement(fake, "Your timer is done.")
    assert spoken == ["Your timer is done."] and replies == []

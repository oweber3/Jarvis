"""A typed request carries its attachments to the reply engine, for that request only.

See ``webchat/webchat.spec.md`` (Attachments) and ``test_chat_submission.py`` for the rest of the contract.
"""
import pytest

from jarvis import daemon
from jarvis.reply.attachments import RequestAttachment
from test_chat_submission import _install_dialogue_memory, _reset_daemon_globals, _wait_for_complete

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_daemon():
    _reset_daemon_globals()
    yield
    _reset_daemon_globals()


def _submit(monkeypatch, **kw):
    _install_dialogue_memory(cfg=object(), db=object())
    seen = []

    def engine(*args, **kwargs):
        seen.append(kwargs)
        return "a reply"

    monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", engine)
    events = []
    daemon.submit_text_query("what is this", on_complete=lambda r: events.append(("complete", r)), **kw)
    _wait_for_complete(events)
    return seen[0]


def test_the_engine_receives_the_attachments(monkeypatch):
    attached = [RequestAttachment("text", 3, text="body")]
    assert _submit(monkeypatch, attachments=attached)["attachments"] == tuple(attached)


def test_a_plain_request_carries_none(monkeypatch):
    assert _submit(monkeypatch)["attachments"] == ()

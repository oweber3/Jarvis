"""The web chat's microphone button, server side: one recording at a time, kept alive by the page.

Recording happens in the daemon (the dictation engine), never in the browser. The transcript is returned
to the page that stopped the recording and goes nowhere else. The daemon is a fake. See
``webchat/webchat.spec.md``, Voice input.
"""
import json
import re
import time
from types import SimpleNamespace

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.webchat import mic as mic_module
from jarvis.webchat.mic import MAX_RECORDING_SEC, MicSessions
from test_webchat_server import call, parts  # noqa: F401  (the fixture and request helper)


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def sessions():
    backend = FakeWebChatBackend()
    mic = MicSessions(backend, heartbeat_timeout_sec=0.3, watch_interval_sec=0.02)
    yield SimpleNamespace(mic=mic, backend=backend)
    mic.shutdown()


# -- sessions ------------------------------------------------------------------


def test_status_says_a_recording_can_start_and_how_long_it_may_run(sessions):
    assert sessions.mic.status() == {"available": True, "reason": None, "max_seconds": MAX_RECORDING_SEC}


def test_status_gives_the_reason_a_recording_cannot_start(sessions):
    sessions.backend.mic_refusal = "not_ready"
    assert sessions.mic.status() == {"available": False, "reason": "not_ready", "max_seconds": MAX_RECORDING_SEC}


def test_starting_gives_an_unguessable_session_and_asks_for_the_capped_length(sessions):
    session, reason = sessions.mic.start()
    assert reason is None and re.fullmatch(r"[0-9a-f]{32}", session)
    assert sessions.backend.mic_calls == [("start", MAX_RECORDING_SEC)]


@pytest.mark.parametrize("reason", ["unavailable", "disabled", "not_ready", "busy", "microphone"])
def test_a_refused_start_gives_the_reason_and_opens_nothing(sessions, reason):
    sessions.backend.mic_refusal = reason
    assert sessions.mic.start() == (None, reason)
    assert sessions.backend.mic_open is None
    assert sessions.mic.status()["reason"] == reason


def test_a_second_recording_is_busy_and_leaves_the_first_alone(sessions):
    first, _ = sessions.mic.start()
    assert sessions.mic.start() == (None, "busy")
    assert sessions.mic.status() == {"available": False, "reason": "busy", "max_seconds": MAX_RECORDING_SEC}
    assert sessions.mic.beat(first) == "recording"


def test_stopping_returns_the_transcript_and_frees_the_microphone(sessions):
    session, _ = sessions.mic.start()
    assert sessions.mic.stop(session) == "hello from the microphone"
    assert sessions.backend.mic_open is None
    assert sessions.mic.stop(session) is None  # a recording is stopped once
    assert sessions.mic.start()[1] is None  # and the next can begin


def test_another_page_cannot_stop_or_cancel_a_recording(sessions):
    session, _ = sessions.mic.start()
    other = "f" * 32
    assert sessions.mic.stop(other) is None
    assert sessions.mic.cancel(other) is False
    assert sessions.mic.beat(other) is None
    assert sessions.backend.mic_open is not None
    assert sessions.mic.stop(session) == "hello from the microphone"


def test_cancelling_releases_the_microphone_without_a_transcript(sessions):
    session, _ = sessions.mic.start()
    assert sessions.mic.cancel(session) is True
    assert sessions.backend.mic_open is None
    assert not [c for c in sessions.backend.mic_calls if c[0] == "stop"]
    assert sessions.mic.beat(session) is None


def test_a_heartbeat_reports_the_recording_and_when_it_reached_its_cap(sessions):
    session, _ = sessions.mic.start()
    assert sessions.mic.beat(session) == "recording"
    sessions.backend.mic_capped = True
    assert sessions.mic.beat(session) == "full"
    assert sessions.mic.stop(session) == "hello from the microphone"


def test_a_page_that_stops_beating_loses_its_recording(sessions):
    session, _ = sessions.mic.start()
    assert wait_until(lambda: sessions.backend.mic_open is None), "the microphone stayed open"
    assert sessions.mic.beat(session) is None
    assert sessions.mic.stop(session) is None
    assert ("cancel", 1) in sessions.backend.mic_calls
    assert not [c for c in sessions.backend.mic_calls if c[0] == "stop"], "nothing is transcribed for a page that left"


def test_a_page_that_keeps_beating_keeps_its_recording(sessions):
    session, _ = sessions.mic.start()
    end = time.time() + 0.8  # well past the 0.3 s timeout
    while time.time() < end:
        assert sessions.mic.beat(session) == "recording"
        time.sleep(0.05)
    assert sessions.backend.mic_open is not None


def test_a_recording_the_daemon_lost_is_forgotten(sessions):
    session, _ = sessions.mic.start()
    sessions.backend.mic_open = None  # the engine stopped (daemon shutdown, say)
    assert sessions.mic.beat(session) is None
    assert sessions.mic.start()[1] is None


def test_shutting_down_releases_the_microphone(sessions):
    sessions.mic.start()
    sessions.mic.shutdown()
    assert sessions.backend.mic_open is None


def test_the_hub_releases_the_microphone_when_the_web_chat_stops(parts):  # noqa: F811
    parts.hub.mic.start()
    parts.hub.stop()
    assert parts.backend.mic_open is None


def test_the_transcript_never_reaches_the_log(sessions, monkeypatch):
    logged = []
    monkeypatch.setattr(mic_module, "debug_log", lambda message, *_a, **_k: logged.append(str(message)))
    sessions.backend.mic_text = "the quarterly figures are private"
    session, _ = sessions.mic.start()
    sessions.mic.stop(session)
    session, _ = sessions.mic.start()
    sessions.mic.cancel(session)
    assert logged, "key points are still logged"
    assert not [m for m in logged if "quarterly" in m]


# -- HTTP API --------------------------------------------------------------------


def start(parts):  # noqa: F811
    status, body, _ = call(parts, "POST", "/api/mic/start", {})
    return status, body


def test_the_api_reports_whether_the_microphone_is_available(parts):  # noqa: F811
    status, body, _ = call(parts, "GET", "/api/mic")
    assert status == 200
    assert body == {"available": True, "reason": None, "max_seconds": MAX_RECORDING_SEC}
    parts.backend.mic_refusal = "disabled"
    assert call(parts, "GET", "/api/mic")[1]["reason"] == "disabled"


def test_the_api_records_and_returns_the_transcript_to_the_page_that_stops(parts):  # noqa: F811
    status, body = start(parts)
    assert status == 200 and body["max_seconds"] == MAX_RECORDING_SEC
    session = body["session"]

    status, body, response = call(parts, "POST", "/api/mic/stop", {"session": session})
    assert status == 200 and body == {"text": "hello from the microphone"}
    assert response.getheader("Cache-Control") == "no-store"


def test_the_transcript_is_not_stored_or_shown_to_other_requests(parts):  # noqa: F811
    parts.backend.mic_text = "a secret transcript"
    session = start(parts)[1]["session"]
    call(parts, "POST", "/api/mic/stop", {"session": session})

    for path in ("/api/state", "/api/library", "/api/poll?rev=-1&after=0", "/api/mic"):
        body = call(parts, "GET", path)[1]
        assert "secret" not in json.dumps(body), path
    chat_id = parts.store.get_active_chat_id()
    assert chat_id is None or parts.store.messages(chat_id) == []
    assert parts.backend.submitted == []  # nothing is sent until the owner sends it


@pytest.mark.parametrize("reason,status", [("busy", 409), ("unavailable", 503), ("disabled", 503),
                                           ("not_ready", 503), ("microphone", 503)])
def test_the_api_refuses_with_the_reason(parts, reason, status):  # noqa: F811
    parts.backend.mic_refusal = reason
    assert start(parts) == (status, {"error": reason})


def test_the_api_heartbeat_answers_for_the_recording_and_not_for_a_stranger(parts):  # noqa: F811
    session = start(parts)[1]["session"]
    assert call(parts, "POST", "/api/mic/heartbeat", {"session": session})[:2] == (200, {"state": "recording"})
    parts.backend.mic_capped = True
    assert call(parts, "POST", "/api/mic/heartbeat", {"session": session})[1] == {"state": "full"}
    assert call(parts, "POST", "/api/mic/heartbeat", {"session": "a" * 32})[:2] == (404, {"error": "unknown"})


def test_the_api_stop_of_an_unknown_recording_is_not_found(parts):  # noqa: F811
    assert call(parts, "POST", "/api/mic/stop", {"session": "a" * 32})[:2] == (404, {"error": "unknown"})


def test_the_api_cancel_releases_the_microphone_and_is_safe_to_repeat(parts):  # noqa: F811
    session = start(parts)[1]["session"]
    assert call(parts, "POST", "/api/mic/cancel", {"session": session})[:2] == (200, {"ok": True})
    assert parts.backend.mic_open is None
    assert call(parts, "POST", "/api/mic/cancel", {"session": session})[:2] == (200, {"ok": True})


@pytest.mark.parametrize("path", ["/api/mic/heartbeat", "/api/mic/stop", "/api/mic/cancel"])
@pytest.mark.parametrize("body", [{}, {"session": 5}, {"session": "not hex"}, {"session": "A" * 32}, {"session": "a" * 33}])
def test_the_api_rejects_a_malformed_session(parts, path, body):  # noqa: F811
    assert call(parts, "POST", path, body)[:2] == (400, {"error": "bad_session"})


def test_the_api_start_from_a_foreign_origin_is_refused(parts):  # noqa: F811
    status, _body, _ = call(parts, "POST", "/api/mic/start", {}, headers={"Origin": "https://evil.example"})
    assert status == 403
    assert parts.backend.mic_calls == []
    status, _body, _ = call(parts, "POST", "/api/mic/start", {}, headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert parts.backend.mic_calls == []


def test_the_api_stopping_the_server_releases_the_microphone(parts):  # noqa: F811
    start(parts)
    parts.server.stop()
    assert parts.backend.mic_open is None

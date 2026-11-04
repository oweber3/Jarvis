"""The desktop face follows the assistant state: in-process when the daemon runs inside the app, over the
daemon's stdout when it runs as a subprocess. See ``desktop_app.spec.md`` (Face state)."""
import pytest

from jarvis import assistant_state, daemon
from jarvis.assistant_state import AssistantState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_state():
    assistant_state.reset()
    yield
    assistant_state.reset()


class FakeFace:
    def __init__(self):
        self.shown = []

    def set_state(self, state):
        self.shown.append(state)


@pytest.fixture
def face(monkeypatch):
    import desktop_app.face_widget as face_widget
    fake = FakeFace()
    monkeypatch.setattr(face_widget, "get_jarvis_state", lambda: fake)
    return fake


def test_in_process_the_face_shows_each_state_change_until_it_stops_following(face):
    from desktop_app.face_widget import JarvisState, follow_assistant_state
    stop = follow_assistant_state()
    assistant_state.set_state(AssistantState.LISTENING)
    assistant_state.set_state(AssistantState.THINKING)
    stop()
    assistant_state.set_state(AssistantState.SPEAKING)
    assert face.shown == [JarvisState.LISTENING, JarvisState.THINKING]


def test_a_change_already_being_announced_never_reaches_the_face_after_it_stops_following(face, monkeypatch):
    # Another thread copied the subscriber list just before the tray stopped following, so the
    # face's callback still runs once afterwards; it must not overwrite the asleep face.
    from desktop_app.face_widget import follow_assistant_state
    captured = []
    real_subscribe = assistant_state.subscribe
    monkeypatch.setattr(assistant_state, "subscribe",
                        lambda callback: captured.append(callback) or real_subscribe(callback))
    stop = follow_assistant_state()
    stop()
    captured[0](AssistantState.THINKING)
    assert face.shown == []


def test_a_broken_face_never_blocks_other_subscribers(monkeypatch):
    import desktop_app.face_widget as face_widget

    def unavailable():
        raise RuntimeError("no Qt here")
    monkeypatch.setattr(face_widget, "get_jarvis_state", unavailable)
    face_widget.follow_assistant_state()
    heard = []
    assistant_state.subscribe(heard.append)
    assistant_state.set_state(AssistantState.LISTENING)
    assert heard == [AssistantState.LISTENING]


def _run_channel(monkeypatch, owns_stdio):
    lines = []
    monkeypatch.setattr(daemon, "_desktop_owns_stdio", lambda: owns_stdio)
    monkeypatch.setattr(daemon, "_write_stdout_line", lines.append)
    close = daemon.open_assistant_state_channel()
    return lines, close


def test_a_subprocess_daemon_sends_each_state_change_to_the_desktop_app(monkeypatch, face):
    from desktop_app.app import _face_state_from_line
    from desktop_app.face_widget import JarvisState
    lines, close = _run_channel(monkeypatch, owns_stdio=True)
    assistant_state.set_state(AssistantState.IDLE)
    assistant_state.set_state(AssistantState.SPEAKING)
    close()
    assistant_state.set_state(AssistantState.THINKING)
    assert [_face_state_from_line(line) for line in lines] == [JarvisState.IDLE, JarvisState.SPEAKING,
                                                               JarvisState.ASLEEP]


def test_a_daemon_nobody_reads_prints_no_state_lines(monkeypatch):
    lines, close = _run_channel(monkeypatch, owns_stdio=False)
    assistant_state.set_state(AssistantState.IDLE)
    close()
    assert lines == []


def test_a_new_run_starts_asleep_so_its_first_state_reaches_the_face(monkeypatch, face):
    """A bundled app restarts the daemon in the same process: the state left by the last run must not
    swallow the new run's first change while the face shows asleep."""
    from desktop_app.face_widget import JarvisState, follow_assistant_state
    assistant_state.set_state(AssistantState.IDLE)   # left over from the previous run
    follow_assistant_state()
    _lines, close = _run_channel(monkeypatch, owns_stdio=False)
    assistant_state.set_state(AssistantState.IDLE)
    close()
    assert face.shown[-1] == JarvisState.ASLEEP and JarvisState.IDLE in face.shown


def test_closing_the_channel_leaves_the_state_asleep(monkeypatch):
    _lines, close = _run_channel(monkeypatch, owns_stdio=False)
    assistant_state.set_state(AssistantState.LISTENING)
    close()
    assert assistant_state.current() is AssistantState.ASLEEP


@pytest.mark.parametrize("line", ["", "plain log line\n", "__ASSISTANT_STATE__:bogus\n"])
def test_other_lines_are_not_face_states(line):
    from desktop_app.app import _face_state_from_line
    assert _face_state_from_line(line) is None


def test_a_state_line_printed_behind_other_output_is_still_read():
    from desktop_app.app import _face_state_from_line, _should_emit_as_log
    from desktop_app.face_widget import JarvisState
    line = f"partial output{daemon.ASSISTANT_STATE_IPC_PREFIX}listening\n"
    assert _face_state_from_line(line) is JarvisState.LISTENING
    assert not _should_emit_as_log(line)

"""Tests must never drive the live orb: its state file is shared with a running Jarvis."""
import os
import tempfile

import pytest

face_widget = pytest.importorskip("desktop_app.face_widget")


def test_tests_never_write_the_live_orb_state_file():
    live_file = os.path.join(tempfile.gettempdir(), "jarvis_state")
    before = os.stat(live_file).st_mtime_ns if os.path.exists(live_file) else None
    face_widget.get_jarvis_state().set_state(face_widget.JarvisState.SPEAKING)
    after = os.stat(live_file).st_mtime_ns if os.path.exists(live_file) else None
    assert after == before

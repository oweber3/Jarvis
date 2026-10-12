"""The shared on flag: another process sees gesture snapping running, and a dead daemon reads as off."""
from jarvis.gestures.snap_state import MAX_AGE_S, SnapStateChannel


def channel(tmp_path):
    return SnapStateChannel(str(tmp_path / "flag"))


def test_off_until_published(tmp_path):
    assert channel(tmp_path).is_on() is False


def test_another_reader_of_the_same_file_sees_the_flag(tmp_path):
    writer, reader = channel(tmp_path), channel(tmp_path)
    writer.publish(True, now=100.0)
    assert reader.is_on(now=100.2) is True
    writer.publish(False, now=100.3)
    assert reader.is_on(now=100.4) is False


def test_a_flag_without_a_recent_heartbeat_reads_as_off(tmp_path):
    ch = channel(tmp_path)
    ch.publish(True, now=100.0)
    assert ch.is_on(now=100.0 + MAX_AGE_S - 0.1) is True
    assert ch.is_on(now=100.0 + MAX_AGE_S + 0.1) is False


def test_an_unusable_file_reads_as_off_and_never_raises(tmp_path):
    ch = SnapStateChannel(str(tmp_path / "missing-folder" / "flag"))
    ch.publish(True)
    assert ch.is_on() is False

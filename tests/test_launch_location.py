"""A Mac app opened outside Applications runs from a read-only translocated
copy that cannot update and has proven unstable, so launch warns first."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TRANSLOCATED_APP = Path(
    "/private/var/folders/dl/abc/T/AppTranslocation/5BC1ADE4-B30B-483B-B767-C3AC9DD10730/d/Jarvis.app"
)


def _on_mac(app_path: Path, frozen: bool = True):
    return (
        patch("desktop_app.updater.sys.platform", "darwin"),
        patch("desktop_app.updater.is_frozen", return_value=frozen),
        patch("desktop_app.updater.get_app_path", return_value=app_path),
    )


def _run(app_path: Path, ask, frozen: bool = True) -> bool:
    from desktop_app.app import confirm_launch_location

    a, b, c = _on_mac(app_path, frozen)
    with a, b, c:
        return confirm_launch_location(ask=ask)


@pytest.mark.unit
def test_translocated_launch_asks_and_quits_when_user_chooses_quit():
    ask = MagicMock(return_value=False)
    assert _run(TRANSLOCATED_APP, ask) is False
    ask.assert_called_once()
    assert "Applications" in ask.call_args.args[0]


@pytest.mark.unit
def test_translocated_launch_continues_when_user_chooses_continue():
    assert _run(TRANSLOCATED_APP, MagicMock(return_value=True)) is True


@pytest.mark.unit
def test_launch_from_applications_does_not_ask():
    ask = MagicMock()
    assert _run(Path("/Applications/Jarvis.app"), ask) is True
    assert not ask.called


@pytest.mark.unit
def test_source_run_does_not_ask():
    ask = MagicMock()
    assert _run(TRANSLOCATED_APP, ask, frozen=False) is True
    assert not ask.called


@pytest.mark.unit
def test_other_platforms_do_not_ask():
    from desktop_app.app import confirm_launch_location

    ask = MagicMock()
    with patch("desktop_app.updater.sys.platform", "win32"), \
         patch("desktop_app.updater.is_frozen", return_value=True), \
         patch("desktop_app.updater.get_app_path", return_value=TRANSLOCATED_APP):
        assert confirm_launch_location(ask=ask) is True
    assert not ask.called

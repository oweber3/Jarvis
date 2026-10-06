"""The app points at this fork's repository, never at upstream, and a fork without releases is not an error."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[1] / "src" / "desktop_app"


def test_issue_reports_open_on_the_repository_the_updater_checks():
    from desktop_app.repository import GITHUB_REPO, issue_report_url
    from desktop_app.updater import GITHUB_API_URL
    url = issue_report_url("Crash Report", "body text", "bug,crash")
    parsed = urllib.parse.urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"https://github.com/{GITHUB_REPO}/issues/new"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["title"] == ["Crash Report"]
    assert query["body"] == ["body text"]
    assert query["labels"] == ["bug,crash"]
    assert GITHUB_API_URL == f"https://api.github.com/repos/{GITHUB_REPO}/releases"


def test_repository_is_the_fork_not_upstream():
    from desktop_app.repository import GITHUB_REPO
    assert GITHUB_REPO != "isair/jarvis"
    owner, _, name = GITHUB_REPO.partition("/")
    assert owner and name and "/" not in name


def test_no_module_hardcodes_a_repository_url():
    for path in SRC.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "github.com/isair" not in text, path.name
        if path.name != "repository.py":
            assert "github.com/isair/jarvis" not in text, path.name


def test_a_repository_without_releases_reads_as_no_update_without_error():
    from desktop_app.updater import check_for_updates
    import requests
    response = MagicMock(status_code=404)
    response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    with patch("desktop_app.updater.get_version", return_value=("1.0.0", "stable")):
        with patch("requests.get", return_value=response):
            status = check_for_updates()
    assert status.update_available is False
    assert status.error is None
    assert status.latest_release is None


def test_other_http_errors_are_still_reported():
    from desktop_app.updater import check_for_updates
    import requests
    response = MagicMock(status_code=500)
    response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    with patch("desktop_app.updater.get_version", return_value=("1.0.0", "stable")):
        with patch("requests.get", return_value=response):
            status = check_for_updates()
    assert status.error is not None

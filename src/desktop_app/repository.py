"""Where this build's releases and issue reports live."""

from __future__ import annotations

import urllib.parse

GITHUB_REPO = "oweber3/jarvis"


def issue_report_url(title: str, body: str, labels: str) -> str:
    """A pre-filled new-issue page on the repository, for the user to review before submitting."""
    params = urllib.parse.urlencode({"title": title, "body": body, "labels": labels})
    return f"https://github.com/{GITHUB_REPO}/issues/new?{params}"

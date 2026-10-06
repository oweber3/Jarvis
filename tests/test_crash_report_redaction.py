"""The crash report is redacted the same way Report Issue is, in the preview and in the URL."""

from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.unit

CRASH_LOG = (
    "Traceback (most recent call last):\n"
    "  File \"daemon.py\", line 9, in run\n"
    "RuntimeError: boom\n"
    "contact owner@example.com token=private-value\n"
)


def _run_dialog(qapp, monkeypatch, crash_log):
    """Show the crash dialog without blocking, capture its preview text and the URL it opens."""
    from PyQt6.QtWidgets import QDialog, QPushButton, QTextEdit
    from desktop_app.app import show_crash_report_dialog

    opened, previews = [], []

    def fake_exec(self):
        previews.append(self.findChild(QTextEdit).toPlainText())
        button = next(b for b in self.findChildren(QPushButton) if "GitHub" in b.text())
        button.click()
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    monkeypatch.setattr("desktop_app.app.webbrowser.open", opened.append, raising=False)
    monkeypatch.setattr("webbrowser.open", opened.append)
    show_crash_report_dialog(crash_log)
    return previews[0], parse_qs(urlparse(opened[0]).query)["body"][0]


def test_crash_report_url_carries_no_secrets_or_emails(qapp, monkeypatch):
    _, body = _run_dialog(qapp, monkeypatch, CRASH_LOG)

    assert "private-value" not in body
    assert "owner@example.com" not in body
    assert "[REDACTED" in body
    assert "RuntimeError: boom" in body  # the useful part of the crash survives


def test_crash_report_preview_shows_what_will_be_sent(qapp, monkeypatch):
    preview, _ = _run_dialog(qapp, monkeypatch, CRASH_LOG)

    assert "private-value" not in preview
    assert "owner@example.com" not in preview
    assert "RuntimeError: boom" in preview

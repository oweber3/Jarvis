"""Issue reports leave the PC in a URL, so they never carry the conversation.

The activity log window shows what Jarvis heard, its replies, recalled diary
entries and tool arguments, and the diary IPC stream. Report Issue and the
crash report keep the start-up section (where setup problems show) and only
the shape of what came after it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.unit

START_UP = (
    "🚀 Starting Jarvis daemon\n"
    "✅ Whisper model 'small' loaded (cuda)\n"
    "📍 Location: Springfield, Illinois, US\n"
    "🧠 Ollama model qwen3.5:0.8b ready\n"
)
LISTENING = "──────────────────────────────────────────────────\n🎙️  Listening! Try:\n"
CONVERSATION = (
    '📝 Heard: "remind me about my appointment with Dr Quinn"\n'
    '  🧠 Intent (wake word): directed → "appointment with Dr Quinn"\n'
    "  📖 Diary: recalled 2 entries\n"
    "     · Talked about the biopsy results and the divorce lawyer\n"
    '    🛠️ Agent → webSearch {"query": "Dr Quinn oncology clinic"}\n'
    "    ❌ webSearch error: no results for Dr Quinn oncology clinic\n"
    "🤖 Jarvis\n"
    "  Your appointment with Dr Quinn is on Friday.\n"
    "  📅 Biopsy follow-up: bring the referral letter\n"
)
DIARY_IPC = (
    '__DIARY__:{"type": "chunks", "data": ["User: my bank PIN is in the drawer"]}\n'
    '__DIARY__:{"type": "token", "data": "the user mentioned the divorce"}\n'
)
PRIVATE_WORDS = ("Dr Quinn", "biopsy", "divorce", "Biopsy", "PIN", "drawer", "oncology", "Springfield")


def _assert_private_words_absent(text: str) -> None:
    for word in PRIVATE_WORDS:
        assert word not in text, word


class TestReportSafeLog:
    def test_conversation_and_diary_stream_are_left_out(self):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log(START_UP + LISTENING + CONVERSATION + DIARY_IPC)

        _assert_private_words_absent(report)
        assert "__DIARY__" not in report

    def test_any_ipc_channel_is_left_out(self):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log(START_UP + '__SOMETHING_NEW__:{"x": "private words"}\n')

        assert "__SOMETHING_NEW__" not in report
        assert "private words" not in report

    def test_start_up_diagnostics_are_kept(self):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log(START_UP + LISTENING + CONVERSATION)

        assert "✅ Whisper model 'small' loaded (cuda)" in report
        assert "🧠 Ollama model qwen3.5:0.8b ready" in report

    def test_the_shape_of_the_conversation_remains(self):
        """Icons show which stages ran and where an error happened."""
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log(START_UP + LISTENING + CONVERSATION)

        for icon in ("📝", "🛠️", "❌", "🤖"):
            assert icon in report

    def test_a_turn_before_listening_still_ends_the_start_up_section(self):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log(START_UP + CONVERSATION)

        _assert_private_words_absent(report)

    def test_a_failed_start_up_keeps_its_error_text(self):
        """Most reports are start-up failures, and their messages are the useful part."""
        from desktop_app.report_filter import report_safe_log

        log = "🚀 Starting Jarvis daemon\n❌ Failed to initialise Whisper: CUDA driver too old\n"

        assert "CUDA driver too old" in report_safe_log(log)

    def test_the_home_folder_is_not_named(self):
        from desktop_app.report_filter import report_safe_log

        home = str(Path.home())
        report = report_safe_log(f"📁 Config: {home}\\.config\\jarvis\\config.json\n")

        assert home not in report
        assert "config.json" in report

    def test_tracebacks_keep_code_locations_and_exception_types_only(self):
        from desktop_app.report_filter import report_safe_log

        log = START_UP + LISTENING + (
            "Traceback (most recent call last):\n"
            '  File "src/jarvis/reply/engine.py", line 120, in run_reply\n'
            "    result = tool.run(args)\n"
            "ValueError: could not parse 'appointment with Dr Quinn'\n"
        )
        report = report_safe_log(log)

        assert 'File "src/jarvis/reply/engine.py", line 120, in run_reply' in report
        assert "ValueError" in report
        _assert_private_words_absent(report)

    def test_a_crash_dump_keeps_its_fatal_section(self):
        from desktop_app.report_filter import report_safe_log

        log = START_UP + LISTENING + CONVERSATION + (
            "Fatal Python error: Aborted\n\n"
            "Current thread 0x00001234 (most recent call first):\n"
            '  File "src/desktop_app/app.py", line 3000 in main\n'
        )
        report = report_safe_log(log)

        assert "Fatal Python error: Aborted" in report
        assert "Current thread 0x00001234" in report
        assert 'File "src/desktop_app/app.py", line 3000 in main' in report
        _assert_private_words_absent(report)

    def test_secrets_are_still_scrubbed(self):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log("🔑 Using token=private-value for the provider\n")

        assert "private-value" not in report


class TestReportIssueButton:
    def test_the_opened_issue_carries_no_conversation(self, qapp, monkeypatch):
        from desktop_app.app import LogViewerWindow

        window = LogViewerWindow()
        for line in (START_UP + LISTENING + CONVERSATION + DIARY_IPC).splitlines(keepends=True):
            window.append_log(line)
        opened = []
        monkeypatch.setattr("desktop_app.app.webbrowser.open", opened.append)

        window._report_issue()

        body = parse_qs(urlparse(opened[0]).query)["body"][0]
        _assert_private_words_absent(body)
        assert "__DIARY__" not in body
        assert "Whisper model 'small' loaded" in body
        window.close()


class TestCrashReport:
    @pytest.fixture(autouse=True)
    def _sandbox_crash_paths(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "jarvis_logs"
        log_dir.mkdir()
        monkeypatch.setattr("desktop_app.paths.get_log_dir", lambda: log_dir)

    def test_a_crash_report_carries_no_conversation(self):
        from desktop_app import check_previous_crash, get_crash_paths

        crash_log, crash_marker, _ = get_crash_paths()
        crash_marker.touch()
        crash_log.write_text(
            START_UP + LISTENING + CONVERSATION + "Fatal Python error: Aborted\n", encoding="utf-8")

        report = check_previous_crash()

        assert "Fatal Python error: Aborted" in report
        _assert_private_words_absent(report)

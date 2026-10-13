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


    def test_a_typed_request_before_listening_ends_the_start_up_section(self):
        """Text chat, phone and web chat print no Heard line; their turn line marks where they start."""
        from desktop_app.report_filter import report_safe_log
        from jarvis.daemon import TURN_LINE_PREFIX

        typed = (
            f"{TURN_LINE_PREFIX}chat\n"
            "  🔍 Memory search: biopsy, Quinn\n"
            "  📖 Diary: recalled 1 entries\n"
            "     · Talked about the divorce lawyer\n"
            "    🌐 Searching the web for 'Dr Quinn oncology clinic'…\n"
        )
        report = report_safe_log(START_UP + typed)

        _assert_private_words_absent(report)
        assert "✅ Whisper model 'small' loaded (cuda)" in report

    def test_an_announcement_ends_the_start_up_section(self):
        from desktop_app.report_filter import report_safe_log
        from jarvis.daemon import TURN_LINE_PREFIX

        report = report_safe_log(START_UP + f"{TURN_LINE_PREFIX}announcement\n⏰ Take the biopsy forms to Dr Quinn\n")

        _assert_private_words_absent(report)

    def test_a_log_cleared_after_start_up_is_all_conversation(self):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log("    🌐 Searching the web for 'Dr Quinn oncology clinic'…\n", start_up=False)

        _assert_private_words_absent(report)

    def test_names_printed_at_start_up_are_left_out(self):
        """Phone links carry the PC's name and the microphone often carries the owner's."""
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log("📱 Phone access is on (port 8765)\n   🔗 http://OWENS-LAPTOP:8765/\n"
                                 "  🎤 Using audio device: Owen's AirPods\n")

        assert "OWENS-LAPTOP" not in report and "Owen" not in report
        assert "📱 Phone access is on (port 8765)" in report

    @pytest.mark.parametrize("line", ["+447700900123 is Sarah's number", "$5000 rent to Sarah", "£40 for Sarah",
                                      "<Sarah> said hi", "~Sarah"])
    def test_a_later_line_keeps_only_a_leading_icon(self, line):
        from desktop_app.report_filter import report_safe_log

        report = report_safe_log(START_UP + LISTENING + "  " + line + "\n")

        assert "Sarah" not in report and "447700900123" not in report and "5000" not in report


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

    def test_a_cleared_log_is_reported_as_conversation(self, qapp, monkeypatch):
        from desktop_app.app import LogViewerWindow

        window = LogViewerWindow()
        window.append_log(START_UP + LISTENING)
        window.clear_logs()
        window.append_log("    🌐 Searching the web for 'Dr Quinn oncology clinic'…\n")
        opened = []
        monkeypatch.setattr("desktop_app.app.webbrowser.open", opened.append)

        window._report_issue()

        body = parse_qs(urlparse(opened[0]).query)["body"][0]
        _assert_private_words_absent(body)
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


class TestOpeningAFolder:
    def test_the_debug_log_does_not_name_the_folder(self, tmp_path, monkeypatch):
        """Folder paths carry the Windows account name."""
        from types import SimpleNamespace
        from unittest.mock import Mock
        import desktop_app.app as app_module

        logged = []
        monkeypatch.setattr(app_module, "debug_log", lambda message, *a, **k: logged.append(message))
        monkeypatch.setattr(app_module.os, "startfile", lambda path: None, raising=False)
        monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: None)
        tray = SimpleNamespace(log_signals=SimpleNamespace(new_log=Mock()), tray_icon=Mock())
        folder = tmp_path / "someone" / "logs"

        app_module.JarvisSystemTray.open_directory(tray, folder, "logs")

        assert logged
        assert all("someone" not in message for message in logged)

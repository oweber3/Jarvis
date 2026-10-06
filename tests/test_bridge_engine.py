"""The background bridge reply path inside the reply engine, for every cloud reply mode."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.bridge import modes, runtime
from jarvis.bridge.adapter import build_dialogue_context
from jarvis.bridge.settings import bridge_settings
from jarvis.bridge.tools import BridgeOutcome
from jarvis.tools.confirmation import ConfirmationRequest, SafetyTier, get_confirmation_store


class LocalPathReached(Exception):
    pass


class FakeService:
    def __init__(self, outcome=None):
        self.outcome = outcome or BridgeOutcome("reply", "Done, sir.")
        self.requests = []

    def prepare(self):
        return None

    def is_busy(self):
        return False

    def cancel_active(self, reason):
        return False

    def close(self):
        pass

    def run_request(self, utterance, context, origin, language, db, quiet, desktop=None):
        desktop = dict(desktop or {})
        self.requests.append(SimpleNamespace(utterance=utterance, context=context, origin=origin,
                                             language=language, quiet=quiet, desktop=desktop,
                                             referents=list(desktop.get("desktop_referents", []))))
        return self.outcome


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    get_confirmation_store().clear_pending()
    modes.reset()
    yield
    modes.reset()
    get_confirmation_store().clear_pending()


@pytest.fixture(params=["codex", "claude"])
def mode(request):
    return request.param


@pytest.fixture
def codex_cfg(mock_config, mode):
    """A configuration in a cloud reply mode (``mode``), with that mode allowed."""
    mock_config.reply_mode = mode
    mock_config.codex_enabled = mode == "codex"
    mock_config.claude_enabled = mode == "claude"
    setattr(mock_config, f"{mode}_share_recent_dialogue", True)
    setattr(mock_config, f"{mode}_recent_dialogue_messages", 6)
    setattr(mock_config, f"{mode}_share_desktop_referents", True)
    return mock_config


@pytest.fixture
def activate(codex_cfg, mode):
    """Enter the cloud mode with ``service`` as its running bridge."""
    def start(service):
        modes.start(codex_cfg, factories={mode: lambda cfg: service}, save=lambda values: True)
        assert modes.wait_for_warm_up(5)  # its start-up report is printed before the test goes on
        return service
    return start


@pytest.fixture
def forbid_local(monkeypatch):
    from jarvis.reply import engine

    def boom(*args, **kwargs):
        raise LocalPathReached()

    for name in ("select_tools", "plan_query", "chat_with_messages", "extract_search_params_for_memory"):
        monkeypatch.setattr(engine, name, boom)


def tts_double():
    spoken = []
    return SimpleNamespace(enabled=True, speak=spoken.append), spoken


@pytest.mark.unit
class TestRouting:
    def test_local_mode_never_touches_the_bridge(self, mode, mock_config, db, dialogue_memory, forbid_local, monkeypatch):
        from jarvis.reply import engine

        def no_bridge():
            pytest.fail("local mode must not look for the bridge")

        monkeypatch.setattr(runtime, "get_service", no_bridge)
        with pytest.raises(LocalPathReached):
            engine.run_reply_engine(db, mock_config, None, "tell me a long story please", dialogue_memory, quiet=True)

    def test_non_fast_request_skips_all_local_inference(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        reply = engine.run_reply_engine(db, codex_cfg, None, "open word and then tell me the time",
                                        dialogue_memory, quiet=True)
        assert reply == "Done, sir."
        assert len(service.requests) == 1

    def test_deterministic_command_stays_local_with_no_submission(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        reply = engine.run_reply_engine(db, codex_cfg, None, "What time is it?", dialogue_memory, quiet=True)
        assert reply and "Current time:" in reply
        assert service.requests == []

    def test_pending_confirmation_answer_is_handled_before_the_bridge(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        get_confirmation_store().set_pending(ConfirmationRequest(
            tool_name="t", tier=SafetyTier.CONFIRM_VOICE, action="delete", target="a.pdf", parameters={}))
        reply = engine.run_reply_engine(db, codex_cfg, None, "no", dialogue_memory, quiet=True)
        assert reply == "Action cancelled."
        assert service.requests == []

    def test_missing_bridge_is_an_explicit_error_never_a_local_fallback(self, codex_cfg, mode, db, dialogue_memory,
                                                                          forbid_local):
        from jarvis.reply import engine

        modes.start(codex_cfg, factories={mode: lambda cfg: FakeService()}, save=lambda values: True)
        runtime.set_service(None)
        reply = engine.run_reply_engine(db, codex_cfg, None, "tell me about black holes", dialogue_memory, quiet=True)
        assert mode in reply.lower() and "local mode" in reply.lower()


@pytest.mark.unit
class TestDelivery:
    def test_voice_speaks_the_accepted_answer_once_and_records_dialogue(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        activate(FakeService())
        tts, spoken = tts_double()
        reply = engine.run_reply_engine(db, codex_cfg, tts, "tell me about black holes", dialogue_memory)
        assert spoken == [reply] == ["Done, sir."]
        assert [(m["role"], m["content"]) for m in dialogue_memory.get_recent_messages()] == [
            ("user", "tell me about black holes"), ("assistant", "Done, sir.")]

    def test_text_is_silent_and_shares_the_same_dialogue(self, codex_cfg, activate, db, dialogue_memory, forbid_local, capsys):
        from jarvis.reply import engine

        activate(FakeService())
        assert modes.wait_for_warm_up(5)
        capsys.readouterr()  # the mode's start-up announcement is not part of the reply
        tts, spoken = tts_double()
        engine.run_reply_engine(db, codex_cfg, tts, "tell me about black holes", dialogue_memory, quiet=True)
        assert capsys.readouterr().out == ""
        assert [m["role"] for m in dialogue_memory.get_recent_messages()] == ["user", "assistant"]

    @pytest.mark.parametrize("kind", ["question", "awaiting_confirmation", "error"])
    def test_questions_confirmations_and_errors_are_spoken_for_voice(self, kind, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        activate(FakeService(BridgeOutcome(kind, "Which one, sir?", "x")))
        tts, spoken = tts_double()
        engine.run_reply_engine(db, codex_cfg, tts, "tell me about black holes", dialogue_memory)
        assert spoken == ["Which one, sir?"]

    def test_cancelled_requests_deliver_nothing(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        activate(FakeService(BridgeOutcome("cancelled", "Cancelled.", "cancelled")))
        tts, spoken = tts_double()
        reply = engine.run_reply_engine(db, codex_cfg, tts, "tell me about black holes", dialogue_memory)
        assert reply is None and spoken == []
        assert dialogue_memory.get_recent_messages() == []


@pytest.mark.unit
class TestRequestContext:
    def test_origin_and_language_follow_the_entry_point(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "tell me about black holes", dialogue_memory,
                                language="tr", quiet=True)
        engine.run_reply_engine(db, codex_cfg, None, "and what about stars", dialogue_memory)
        assert [(r.origin, r.language, r.quiet) for r in service.requests] == [
            ("chat", "tr", True), ("voice", None, False)]

    def test_a_fast_turn_is_part_of_the_next_request_context(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        fast = engine.run_reply_engine(db, codex_cfg, None, "What time is it?", dialogue_memory, quiet=True)
        engine.run_reply_engine(db, codex_cfg, None, "and what about stars", dialogue_memory, quiet=True)
        ctx = service.requests[0].context
        assert ctx == [{"role": "user", "content": "What time is it?"},
                       {"role": "assistant", "content": fast}]

    def test_a_new_chat_leaves_nothing_for_the_next_request(self, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "my locker code is 4721, tell me a joke", dialogue_memory,
                                quiet=True)
        dialogue_memory.clear()
        engine.run_reply_engine(db, codex_cfg, None, "what did I just tell you about lockers", dialogue_memory,
                                quiet=True)
        assert service.requests[1].context == []
        assert "4721" not in str(service.requests[1])

    def test_turning_sharing_off_takes_effect_on_the_next_request(self, mode, codex_cfg, activate, db, dialogue_memory,
                                                                    forbid_local):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "my locker code is 4721, tell me a joke", dialogue_memory,
                                quiet=True)
        setattr(codex_cfg, f"{mode}_share_recent_dialogue", False)
        engine.run_reply_engine(db, codex_cfg, None, "what did I just tell you about lockers", dialogue_memory,
                                quiet=True)
        assert service.requests[1].context == []

    def test_sharing_can_be_disabled(self, mode, codex_cfg, activate, dialogue_memory):
        dialogue_memory.add_message("user", "hello")
        dialogue_memory.add_message("assistant", "hi")
        setattr(codex_cfg, f"{mode}_share_recent_dialogue", False)
        assert build_dialogue_context(dialogue_memory, bridge_settings(codex_cfg, mode)) == []

    def test_context_is_bounded_redacted_and_limited_to_dialogue_roles(self, mode, codex_cfg, activate, dialogue_memory):
        for i in range(10):
            dialogue_memory.add_message("user" if i % 2 == 0 else "assistant", f"message {i}")
        dialogue_memory.add_message("user", "mail alice@example.com")
        ctx = build_dialogue_context(dialogue_memory, bridge_settings(codex_cfg, mode))
        assert len(ctx) == 6
        assert all(set(m) == {"role", "content"} and m["role"] in ("user", "assistant") for m in ctx)
        assert "alice@example.com" not in str(ctx)
        assert ctx[-1]["content"].startswith("mail ")

    def test_message_count_follows_the_setting(self, mode, codex_cfg, activate, dialogue_memory):
        for i in range(8):
            dialogue_memory.add_message("user" if i % 2 == 0 else "assistant", f"m{i}")
        setattr(codex_cfg, f"{mode}_recent_dialogue_messages", 2)
        assert [m["content"] for m in build_dialogue_context(dialogue_memory, bridge_settings(codex_cfg, mode))] == ["m6", "m7"]

    def test_turns_that_quote_the_activity_log_stay_local_by_default(self, mode, codex_cfg, activate,
                                                                       dialogue_memory):
        dialogue_memory.add_message("user", "what was I doing this afternoon", diary=False)
        dialogue_memory.add_message("assistant", "Proposal.docx - Word for two hours", diary=False)
        dialogue_memory.add_message("user", "thanks")
        ctx = build_dialogue_context(dialogue_memory, bridge_settings(codex_cfg, mode))
        assert [m["content"] for m in ctx] == ["thanks"]

    @pytest.mark.parametrize("share", ["true", 1, None])
    def test_only_a_real_true_shares_activity_turns(self, mode, codex_cfg, activate, dialogue_memory, share):
        dialogue_memory.add_message("assistant", "Proposal.docx - Word for two hours", diary=False)
        codex_cfg.activity_log_share_with_cloud = share
        assert build_dialogue_context(dialogue_memory, bridge_settings(codex_cfg, mode)) == []

    def test_activity_turns_are_shared_when_the_owner_allows_it(self, mode, codex_cfg, activate, dialogue_memory):
        dialogue_memory.add_message("assistant", "Proposal.docx - Word for two hours", diary=False)
        codex_cfg.activity_log_share_with_cloud = True
        ctx = build_dialogue_context(dialogue_memory, bridge_settings(codex_cfg, mode))
        assert [m["content"] for m in ctx] == ["Proposal.docx - Word for two hours"]

    def test_no_dialogue_memory_gives_empty_context(self, mode, codex_cfg):
        assert build_dialogue_context(None, bridge_settings(codex_cfg, mode)) == []


@pytest.mark.unit
class TestDesktopRecords:
    @pytest.fixture(autouse=True)
    def referents(self):
        from jarvis.memory.desktop_referents import DesktopReferent, get_desktop_referents
        store = get_desktop_referents()
        store.clear()
        store.record(DesktopReferent(application="Word", process="WINWORD", hwnd=131338,
                                     monitor=r"\\.\DISPLAY2", state="normal", last_action="place"))
        yield store
        store.clear()

    def test_records_reach_codex_even_with_no_dialogue_shared(self, mode, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        setattr(codex_cfg, f"{mode}_recent_dialogue_messages", 0)
        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "maximise it", dialogue_memory, quiet=True)
        (request,) = service.requests
        assert request.context == []
        assert [(r["application"], r["hwnd"], r["monitor"]) for r in request.referents] == [
            ("Word", 131338, r"\\.\DISPLAY2")]

    def test_records_are_withheld_when_sharing_is_off(self, mode, codex_cfg, activate, db, dialogue_memory, forbid_local):
        from jarvis.reply import engine

        setattr(codex_cfg, f"{mode}_share_desktop_referents", False)
        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "maximise it", dialogue_memory, quiet=True)
        assert service.requests[0].referents == []

    def test_records_older_than_the_conversation_are_not_shared(self, codex_cfg, activate, db, dialogue_memory,
                                                                forbid_local, referents, monkeypatch):
        from jarvis.memory import desktop_referents
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        later = desktop_referents.time.monotonic() + codex_cfg.dialogue_memory_timeout + 1
        monkeypatch.setattr(referents, "_clock", lambda: later)
        engine.run_reply_engine(db, codex_cfg, None, "maximise it", dialogue_memory, quiet=True)
        assert service.requests[0].referents == []

    def test_device_media_and_clipboard_records_ride_with_the_window_records(
            self, mode, codex_cfg, activate, db, dialogue_memory, forbid_local, referents):
        from jarvis.reply import engine

        referents.record_device("tv", "tvControl", "key PowerOn")
        referents.record_media("Apple Music", "playing")
        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "turn it off", dialogue_memory, quiet=True)
        desktop = service.requests[0].desktop
        assert [(o["kind"], o["tool"]) for o in desktop["other_referents"]] == [
            ("media", "mediaControl"), ("device", "tvControl")]
        assert "reference" in desktop["desktop_referents_note"].lower()

    def test_device_records_are_withheld_when_sharing_is_off(
            self, mode, codex_cfg, activate, db, dialogue_memory, forbid_local, referents):
        from jarvis.reply import engine

        referents.record_device("tv", "tvControl", "key PowerOn")
        setattr(codex_cfg, f"{mode}_share_desktop_referents", False)
        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, codex_cfg, None, "turn it off", dialogue_memory, quiet=True)
        assert "other_referents" not in service.requests[0].desktop


@pytest.mark.unit
class TestForegroundWindow:
    """The window the user is looking at, attached per request under ``<mode>_share_foreground_window``."""

    @pytest.fixture(autouse=True)
    def looking_at_chrome(self, monkeypatch):
        import sys

        from jarvis.memory.desktop_referents import get_desktop_referents
        from jarvis.platform.windows import ui_automation
        reads = []

        def foreground_target():
            reads.append(1)
            return {"hwnd": 4242, "process": "chrome", "application": "Google Chrome",
                    "monitor": r"\\.\DISPLAY1", "state": "maximised"}

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ui_automation, "foreground_target", foreground_target)
        get_desktop_referents().clear()
        yield reads
        get_desktop_referents().clear()

    @pytest.fixture
    def sharing(self, codex_cfg, mode):
        codex_cfg.windows_tools_enabled = True
        setattr(codex_cfg, f"{mode}_share_foreground_window", True)
        return codex_cfg

    @pytest.mark.parametrize("quiet", [True, False])
    def test_requests_made_at_the_pc_carry_the_foreground_window(self, sharing, activate, db, dialogue_memory,
                                                                 forbid_local, quiet):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, sharing, None, "close this", dialogue_memory, quiet=quiet)
        desktop = service.requests[0].desktop
        assert desktop["foreground_window"] == {"application": "Google Chrome", "process": "chrome", "hwnd": 4242,
                                                "monitor": r"\\.\DISPLAY1", "state": "maximised"}
        assert "foreground_window" in desktop["desktop_referents_note"]
        assert "desktop_referents" not in desktop  # Jarvis has acted on nothing yet

    def test_a_phone_request_reads_and_shares_no_foreground(self, sharing, activate, db, dialogue_memory,
                                                            forbid_local, looking_at_chrome):
        from jarvis.reply import engine

        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, sharing, None, "close this", dialogue_memory, quiet=True, origin="phone")
        (request,) = service.requests
        assert request.origin == "phone"
        assert "foreground_window" not in request.desktop and looking_at_chrome == []

    def test_the_foreground_is_withheld_when_its_sharing_is_off(self, sharing, mode, activate, db,
                                                               dialogue_memory, forbid_local, looking_at_chrome):
        from jarvis.reply import engine

        setattr(sharing, f"{mode}_share_foreground_window", False)
        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, sharing, None, "close this", dialogue_memory, quiet=True)
        assert service.requests[0].desktop == {} and looking_at_chrome == []

    def test_window_record_sharing_off_does_not_hide_the_foreground(self, sharing, mode, activate, db,
                                                                   dialogue_memory, forbid_local):
        from jarvis.reply import engine

        setattr(sharing, f"{mode}_share_desktop_referents", False)
        service = FakeService()
        activate(service)
        engine.run_reply_engine(db, sharing, None, "close this", dialogue_memory, quiet=True)
        assert service.requests[0].desktop["foreground_window"]["hwnd"] == 4242

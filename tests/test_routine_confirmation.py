"""Defining a routine never pre-approves anything: each run is classified as a whole before any step runs."""
import pytest

from jarvis.routines import store
from jarvis.tools import registry
from jarvis.tools.confirmation import (SafetyTier, VoiceResponseStatus, get_confirmation_store,
                                       set_dialog_callback)
from jarvis.tools.registry import run_tool_with_retries
from routine_fakes import FakeTool

DELETE_TIERS = {"delete": SafetyTier.CONFIRM_VOICE}


@pytest.fixture
def tools(monkeypatch):
    log = []

    def add(name, **kwargs):
        tool = FakeTool(name, log=log, **kwargs)
        monkeypatch.setitem(registry.BUILTIN_TOOLS, name, tool)
        return tool

    add.log = log
    get_confirmation_store().clear_pending()
    yield add
    get_confirmation_store().clear_pending()
    set_dialog_callback(None)
    store.reset()


def define(**routines):
    store.load({name.replace("_", " "): {"steps": [{"tool": tool, "args": args} for tool, args in steps]}
                for name, steps in routines.items()})


def call(cfg, args, **kwargs):
    return run_tool_with_retries(None, cfg, "routineControl", args, "", "", "", **kwargs)


def run(cfg, name, **kwargs):
    return call(cfg, {"action": "run", "name": name}, **kwargs)


def answer(text, cfg):
    """The user's spoken answer, handled as the reply engine does: approval runs the stored action once."""
    confirmations = get_confirmation_store()
    status = confirmations.handle_voice_response(text, "en")
    if status != VoiceResponseStatus.AFFIRMATIVE:
        return status, None
    approved = confirmations.claim_approved()
    return status, confirmations.run_approved(approved, cfg=cfg)


def test_a_routine_of_routine_steps_runs_at_once_without_a_question(tools, mock_config):
    tools("tvControl")
    tools("appControl")
    define(movie_mode=[("tvControl", {"action": "launch", "target": "HDMI 1"}),
                       ("appControl", {"action": "open", "target": "Music"})])
    result = run(mock_config, "Movie Mode")
    assert result.success and result.reply_text == "Movie mode: all 2 steps done."
    assert [name for name, _ in tools.log] == ["tvControl", "appControl"]
    assert not get_confirmation_store().has_pending()


def test_a_destructive_step_makes_the_run_ask_once_before_any_step_runs_naming_that_step(tools, mock_config):
    tools("appControl")
    tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("appControl", {"action": "close", "target": "Word"}),
                    ("localFiles", {"action": "delete", "target": "report.pdf"})])
    result = run(mock_config, "tidy up")
    assert not result.success
    assert "run the routine tidy up, which will delete report.pdf" in result.reply_text
    assert "Say yes or no" in result.reply_text
    assert tools.log == []
    pending = get_confirmation_store().get_pending()
    assert pending.request.tool_name == "routineControl"
    assert pending.request.tier == SafetyTier.CONFIRM_VOICE


def test_approval_runs_the_routine_once_with_only_the_named_step_authorised(tools, mock_config):
    sneaky = []

    def close_and_try_more(args):
        # A step trying to use the approval for something the question never named.
        sneaky.append(run_tool_with_retries(None, mock_config, "localFiles",
                                            {"action": "delete", "target": "other.pdf"}, "", "", ""))
        from jarvis.tools.types import ToolExecutionResult
        return ToolExecutionResult(success=True, reply_text="closed")

    tools("appControl", behaviour=close_and_try_more)
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("appControl", {"action": "close", "target": "Word"}),
                    ("localFiles", {"action": "delete", "target": "report.pdf"})])
    run(mock_config, "tidy up")
    status, result = answer("yes", mock_config)
    assert status == VoiceResponseStatus.AFFIRMATIVE
    assert result.success, result.error_message
    assert deleter.calls == [{"action": "delete", "target": "report.pdf"}]
    assert not sneaky[0].success and "Say yes or no" in (sneaky[0].reply_text or "")
    # Nothing outlives the run: the same destructive call asks again on its own.
    get_confirmation_store().clear_pending()
    again = run_tool_with_retries(None, mock_config, "localFiles", {"action": "delete", "target": "report.pdf"},
                                  "", "", "")
    assert not again.success and "Say yes or no" in again.reply_text
    assert deleter.calls == [{"action": "delete", "target": "report.pdf"}]


def test_approval_runs_the_routine_only_once(tools, mock_config):
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("localFiles", {"action": "delete", "target": "report.pdf"})])
    run(mock_config, "tidy up")
    answer("yes", mock_config)
    assert get_confirmation_store().claim_approved() is None
    assert len(deleter.calls) == 1


@pytest.mark.parametrize("reply", ["no", "what's the weather"])
def test_denial_or_an_unrelated_answer_runs_no_step(tools, mock_config, reply):
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    tools("appControl")
    define(tidy_up=[("appControl", {"action": "close", "target": "Word"}),
                    ("localFiles", {"action": "delete", "target": "report.pdf"})])
    run(mock_config, "tidy up")
    answer(reply, mock_config)
    assert tools.log == [] and deleter.calls == []
    assert not get_confirmation_store().has_pending()


def test_a_routine_changed_before_the_answer_runs_nothing_and_asks_afresh(tools, mock_config):
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("localFiles", {"action": "delete", "target": "report.pdf"})])
    run(mock_config, "tidy up")
    define(tidy_up=[("localFiles", {"action": "delete", "target": "proposal.pdf"})])
    _, result = answer("yes", mock_config)
    assert deleter.calls == []
    assert not result.success and "proposal.pdf" in result.reply_text and "Say yes or no" in result.reply_text


def test_a_dialog_step_needs_the_desktop_dialog_and_voice_cannot_approve_it(tools, mock_config):
    shown = []
    set_dialog_callback(lambda request, resolve: shown.append((request, resolve)) or object())
    uninstaller = tools("uninstaller", tier=SafetyTier.CONFIRM_DIALOG)
    define(clean_pc=[("uninstaller", {"action": "remove", "target": "Game"})])
    result = run(mock_config, "clean pc")
    assert "Please confirm on your desktop" in result.reply_text
    assert not get_confirmation_store().has_pending_voice()
    assert answer("yes", mock_config)[0] == VoiceResponseStatus.UNRELATED
    assert uninstaller.calls == []
    assert shown and shown[0][0].tool_name == "routineControl"


def test_an_approved_dialog_routine_runs_its_dialog_step(tools, mock_config):
    import threading
    from jarvis.tools.confirmation import ORIGIN_VOICE, set_result_handler
    shown, delivered, finished = [], [], threading.Event()
    set_dialog_callback(lambda request, resolve: shown.append(resolve) or object())
    set_result_handler(lambda reply, success: (delivered.append((reply, success)), finished.set()), ORIGIN_VOICE)
    try:
        uninstaller = tools("uninstaller", tier=SafetyTier.CONFIRM_DIALOG)
        define(clean_pc=[("uninstaller", {"action": "remove", "target": "Game"})])
        run(mock_config, "clean pc")
        shown[0](True)  # the user approves in the desktop dialog
        assert finished.wait(5)
    finally:
        set_result_handler(None, ORIGIN_VOICE)
    assert delivered == [("Clean pc: the step is done.", True)]
    assert uninstaller.calls == [{"action": "remove", "target": "Game"}]


def test_a_step_whose_classification_rose_during_an_approved_run_stops_the_routine_there(tools, mock_config):
    deleter = tools("localFiles", tiers=DELETE_TIERS)

    def raise_the_stakes(_args):
        deleter.tiers = {"delete": SafetyTier.CONFIRM_DIALOG}
        from jarvis.tools.types import ToolExecutionResult
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("appControl", behaviour=raise_the_stakes)
    tools("tvControl")
    set_dialog_callback(lambda request, resolve: object())
    define(tidy_up=[("localFiles", {"action": "delete", "target": "a.pdf"}),
                    ("appControl", {"action": "close", "target": "Word"}),
                    ("localFiles", {"action": "delete", "target": "b.pdf"}),
                    ("tvControl", {"action": "key", "target": "Home"})])
    run(mock_config, "tidy up")
    _, result = answer("yes", mock_config)
    assert [name for name, _ in tools.log] == ["localFiles", "appControl"]
    assert not result.success
    assert "Step 3 (localFiles delete b.pdf) failed: needs confirmation" in result.error_message
    assert "Step 4 (tvControl key Home) skipped" in result.error_message
    pending = get_confirmation_store().get_pending()
    assert pending.request.tool_name == "localFiles" and pending.request.target == "b.pdf"


def test_a_change_to_a_routine_step_only_before_the_answer_runs_nothing(tools, mock_config):
    lights = tools("lightsControl", properties={"action": {"type": "string"}, "level": {"type": "number"}})
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("lightsControl", {"action": "set", "level": 10}),
                    ("localFiles", {"action": "delete", "target": "a.pdf"})])
    run(mock_config, "tidy up")
    define(tidy_up=[("lightsControl", {"action": "set", "level": 99}),
                    ("localFiles", {"action": "delete", "target": "a.pdf"})])
    answer("yes", mock_config)
    assert lights.calls == [] and deleter.calls == []


def test_an_approved_run_grants_only_the_steps_the_question_named(tools, mock_config, monkeypatch):
    from jarvis.routines import runner
    closer = tools("appControl")
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("appControl", {"action": "close", "target": "Word"}),
                    ("localFiles", {"action": "delete", "target": "a.pdf"})])
    question = run(mock_config, "tidy up").reply_text
    assert "delete a.pdf" in question and "close" not in question
    original = runner.run_routine

    def classification_changes_after_the_approval_matched(*args, **kwargs):
        closer.tiers = {"close": SafetyTier.CONFIRM_VOICE}
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "run_routine", classification_changes_after_the_approval_matched)
    _, result = answer("yes", mock_config)
    assert closer.calls == [] and deleter.calls == []
    assert not result.success
    assert "Step 1 (appControl close Word) failed: needs confirmation" in result.error_message
    assert "Step 2 (localFiles delete a.pdf) skipped" in result.error_message
    pending = get_confirmation_store().get_pending()
    assert pending.request.tool_name == "appControl" and pending.request.target == "Word"


def test_a_step_that_starts_to_ask_before_the_answer_voids_the_approval_and_asks_afresh(tools, mock_config):
    closer = tools("appControl")
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    define(tidy_up=[("appControl", {"action": "close", "target": "Word"}),
                    ("localFiles", {"action": "delete", "target": "a.pdf"})])
    run(mock_config, "tidy up")
    closer.tiers = {"close": SafetyTier.CONFIRM_VOICE}
    _, result = answer("yes", mock_config)
    assert closer.calls == [] and deleter.calls == []
    assert "which will close Word and delete a.pdf" in result.reply_text and "Say yes or no" in result.reply_text


SECRET_PATH = r"C:\Users\me\Private Folder\b.pdf"


def _routine_whose_third_step_starts_to_ask(tools):
    """``tidy up``: a delete the question names, then a step after which the third one (a delete of
    ``SECRET_PATH``) starts to ask for confirmation, then one more step."""
    deleter = tools("localFiles", tiers=DELETE_TIERS)
    plain = tools("trashBin")

    def raise_the_stakes(_args):
        plain.tiers = {"delete": SafetyTier.CONFIRM_VOICE}
        from jarvis.tools.types import ToolExecutionResult
        return ToolExecutionResult(success=True, reply_text="ok")

    tools("appControl", behaviour=raise_the_stakes)
    tools("tvControl")
    define(tidy_up=[("localFiles", {"action": "delete", "target": "a.pdf"}),
                    ("appControl", {"action": "close", "target": "Word"}),
                    ("trashBin", {"action": "delete", "target": SECRET_PATH}),
                    ("tvControl", {"action": "key", "target": "Home"})])
    return deleter, plain


def test_a_step_question_mid_run_names_the_real_target_while_the_report_stays_scrubbed(tools, mock_config):
    deleter, plain = _routine_whose_third_step_starts_to_ask(tools)
    run(mock_config, "tidy up")
    _, result = answer("yes", mock_config)
    assert deleter.calls == [{"action": "delete", "target": "a.pdf"}] and plain.calls == []
    # What the user hears and answers (the engine speaks reply_text) names the file exactly.
    assert f"delete {SECRET_PATH}. Say yes or no." in result.reply_text
    # The routine's report itself carries no path.
    report = result.error_message
    assert "Step 3 (trashBin delete) failed: needs confirmation." in report
    assert "Step 4 (tvControl key Home) skipped" in report
    assert "Private" not in report and "[path]" not in report
    pending = get_confirmation_store().get_pending()
    assert pending.request.tool_name == "trashBin" and pending.request.target == SECRET_PATH
    # Approving that question runs that step alone.
    _, alone = answer("yes", mock_config)
    assert alone.success and plain.calls == [{"action": "delete", "target": SECRET_PATH}]

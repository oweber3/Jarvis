"""Fast replies retain central execution, dialogue, quiet output and voice gates."""
from types import SimpleNamespace
import time

import pytest

from jarvis.fastpath.dispatcher import dispatch
from jarvis.fastpath.matcher import FastMatch
from jarvis.tools.confirmation import ConfirmationRequest, SafetyTier, get_confirmation_store, set_dialog_callback
from jarvis.tools.types import ToolExecutionResult


@pytest.fixture(autouse=True)
def reset_confirmation():
    get_confirmation_store().clear_pending()
    set_dialog_callback(None)
    yield
    get_confirmation_store().clear_pending()
    set_dialog_callback(None)


def test_engine_returns_real_time_without_llm_and_records_dialogue(mock_config, db, dialogue_memory, monkeypatch, capsys):
    from jarvis.reply import engine
    from jarvis.tools.builtin import time_tool
    def forbidden(*args, **kwargs):
        pytest.fail('A fast time query must not call a model or location service')
    for name in ('select_tools', 'plan_query', 'chat_with_messages', 'extract_search_params_for_memory'):
        monkeypatch.setattr(engine, name, forbidden)
    monkeypatch.setattr(time_tool, 'get_location_context_with_timezone', forbidden)
    reply = engine.run_reply_engine(db, mock_config, None, 'What time is it?', dialogue_memory, quiet=True)
    assert reply and 'Current time:' in reply
    messages = dialogue_memory.get_recent_messages()
    assert [(m['role'], m['content']) for m in messages] == [('user', 'What time is it?'), ('assistant', reply)]
    assert capsys.readouterr().out == ''


def test_local_time_tool_can_use_os_clock_without_location_lookup(mock_config, monkeypatch):
    from jarvis.tools.registry import run_tool_with_retries
    from jarvis.tools.builtin import time_tool
    def forbidden(*args, **kwargs):
        pytest.fail('Local-only time must not resolve an IP or place')
    monkeypatch.setattr(time_tool, 'get_location_context_with_timezone', forbidden)
    result = run_tool_with_retries(None, mock_config, 'getTime', {'local_only': True}, '', '', '')
    assert result.success and 'Current time:' in result.reply_text


def test_local_only_time_does_not_geocode_an_accidental_location(mock_config, monkeypatch):
    from jarvis.tools.registry import run_tool_with_retries
    from jarvis.tools.builtin import time_tool
    def forbidden(*args, **kwargs):
        pytest.fail('Local-only time must not geocode a location')
    monkeypatch.setattr(time_tool.requests, 'get', forbidden)
    result = run_tool_with_retries(None, mock_config, 'getTime',
                                   {'local_only': True, 'location': 'Tokyo'}, '', '', '')
    assert result.success and result.reply_text.startswith('Current time:')


def test_fast_reply_uses_supplied_tts(mock_config, db, dialogue_memory, monkeypatch):
    from jarvis.reply import engine
    spoken = []
    tts = SimpleNamespace(enabled=True, speak=spoken.append)
    reply = engine.run_reply_engine(db, mock_config, tts, 'What day is it?', dialogue_memory, quiet=True)
    assert spoken == [reply]


@pytest.mark.parametrize('tier', [SafetyTier.CONFIRM_VOICE, SafetyTier.CONFIRM_DIALOG, SafetyTier.DENY])
def test_dispatch_cannot_bypass_reclassified_action(tier, mock_config, monkeypatch):
    from jarvis.tools.base import Tool
    from jarvis.tools.registry import BUILTIN_TOOLS
    executed = []
    class ProtectedTool(Tool):
        name = 'appControl'
        description = 'Test protected action'
        inputSchema = {'type': 'object'}
        def classify_safety(self, args, cfg):
            return ConfirmationRequest(self.name, tier, 'close', 'test app', args)
        def run(self, args, context):
            executed.append(args)
            return ToolExecutionResult(True, 'closed')
    monkeypatch.setitem(BUILTIN_TOOLS, 'appControl', ProtectedTool())
    route = FastMatch('app.close', 'apps', 'appControl', {'action': 'close', 'target': 'test app'}, 'Closed.')
    reply = dispatch(route, None, mock_config, 'Close test app', 'en')
    assert not executed
    assert 'Closed.' not in reply
    if tier == SafetyTier.CONFIRM_VOICE:
        assert get_confirmation_store().has_pending()
        assert 'confirmation' in reply
    elif tier == SafetyTier.CONFIRM_DIALOG:
        assert 'desktop confirmation' in reply
    else:
        assert 'prohibited' in reply


def test_dispatch_keeps_tool_argument_validation(mock_config):
    route = FastMatch('volume.set', 'volume', 'systemVolume', {'action': 'set', 'percent': 'invalid'})
    reply = dispatch(route, None, mock_config, 'test', 'en')
    assert 'percentage' in reply.lower()


def make_listener(cfg, judge):
    from jarvis.listening.listener import VoiceListener
    from jarvis.listening.state_manager import StateManager
    from jarvis.listening.transcript_buffer import TranscriptBuffer
    obj = VoiceListener.__new__(VoiceListener)
    obj.cfg = cfg
    obj.tts = None
    obj.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0,
                                        _last_tts_text='', echo_tolerance=.3)
    obj.state_manager = StateManager(voice_collect_seconds=.1)
    obj._transcript_buffer = TranscriptBuffer()
    obj._buffer_duration = 120
    obj._last_detected_language = 'en'
    obj._intent_judge = SimpleNamespace(available=True, judge=judge)
    obj._is_engaged = lambda: False
    obj._start_engagement = lambda: None
    obj._end_engagement = lambda: None
    obj._set_face_state_listening = lambda: None
    obj._clear_audio_buffers = lambda: None
    obj.dispatched = []
    obj._dispatch_query = obj.dispatched.append
    return obj


def process(obj, text):
    stamp = time.time()
    obj._transcript_buffer.add(text, stamp - .2, stamp, .1)
    obj._process_transcript(text, .1, stamp - .2, stamp,
                            captured_during_tts=False, captured_tts_start_time=0)


def test_confident_voice_command_dispatches_without_judge_or_collection(mock_config):
    def forbidden(**kwargs):
        pytest.fail('Confident voice command reached intent judge')
    obj = make_listener(mock_config, forbidden)
    process(obj, 'jarvis what time is it?')
    assert obj.dispatched == ['what time is it?']
    assert not obj.state_manager.is_collecting()
    assert all(seg.processed for seg in obj._transcript_buffer.get_last_seconds(120))
    obj.state_manager.stop()


def test_unmatched_voice_command_keeps_judge_and_collection(mock_config):
    from jarvis.listening.intent_judge import IntentJudgment
    def judge(**kwargs):
        return IntentJudgment(True, 'help me choose an app', False, 'high', 'directed')
    obj = make_listener(mock_config, judge)
    process(obj, 'jarvis help me choose an app')
    assert not obj.dispatched
    assert obj.state_manager.get_pending_query() == 'help me choose an app'
    assert obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_ambient_command_is_not_fast_dispatched(mock_config):
    def forbidden(**kwargs):
        pytest.fail('Ambient command reached judge')
    obj = make_listener(mock_config, forbidden)
    process(obj, 'what time is it?')
    assert not obj.dispatched and not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_collection_cannot_be_interrupted_by_a_fragment_fast_match(mock_config):
    from jarvis.listening.intent_judge import IntentJudgment
    obj = make_listener(mock_config, lambda **kw: IntentJudgment(True, 'what time is it', False, 'high', 'directed'))
    obj.state_manager.start_collection('help me decide')
    process(obj, 'jarvis what time is it')
    assert not obj.dispatched
    obj.state_manager.stop()


def test_hot_window_command_uses_same_immediate_dispatch(mock_config):
    def forbidden(**kwargs):
        pytest.fail('Hot-window fast command reached judge')
    obj = make_listener(mock_config, forbidden)
    enter_hot_window(obj)
    process(obj, 'what time is it?')
    assert obj.dispatched == ['what time is it?']
    assert not obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_hot_window_tts_echo_cannot_execute_a_command(mock_config):
    def forbidden(**kwargs):
        pytest.fail('Pure echo reached judge')
    obj = make_listener(mock_config, forbidden)
    enter_hot_window(obj)
    obj.echo_detector._last_tts_text = 'what time is it?'
    obj.echo_detector.cleanup_leading_echo = lambda text: text
    obj.echo_detector.salvage_after_echo_tail = lambda text: None
    obj.echo_detector.min_salvage_words = 3
    process(obj, 'what time is it?')
    assert not obj.dispatched
    obj.state_manager.stop()


def test_unsupported_voice_language_retains_judge(mock_config):
    from jarvis.listening.intent_judge import IntentJudgment
    obj = make_listener(mock_config, lambda **kw: IntentJudgment(True, 'what time is it', False, 'high', 'directed'))
    obj._last_detected_language = 'fr'
    process(obj, 'jarvis what time is it?')
    assert not obj.dispatched
    assert obj.state_manager.is_collecting()
    obj.state_manager.stop()


def test_nonroutine_classification_is_ineligible_for_matching(mock_config, monkeypatch):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.tools.registry import BUILTIN_TOOLS
    monkeypatch.setattr(BUILTIN_TOOLS['getTime'], 'classify_safety', lambda args, cfg:
                        ConfirmationRequest('getTime', SafetyTier.CONFIRM_VOICE, 'test', 'clock', args))
    assert match_command('What time is it?', mock_config) is None
    assert not get_confirmation_store().has_pending()


def test_time_route_does_not_need_application_catalogue(mock_config, monkeypatch):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.tools.registry import BUILTIN_TOOLS
    def forbidden(cfg):
        pytest.fail('A time query must not build application routing candidates')
    monkeypatch.setattr(BUILTIN_TOOLS['appControl'], 'fast_targets', forbidden)
    assert match_command('What time is it?', mock_config).tool_name == 'getTime'


def test_failed_application_action_returns_error_not_success_template(mock_config, monkeypatch):
    from jarvis.tools.registry import BUILTIN_TOOLS
    def failure(*args):
        raise OSError('Application launch was refused.')
    monkeypatch.setattr(BUILTIN_TOOLS['appControl'], '_operate', failure)
    route = FastMatch('app.open', 'apps', 'appControl', {'action': 'open', 'target': 'Word'},
                      'Opening {app}.', {'app': 'Word'})
    assert dispatch(route, None, mock_config, 'Open Word') == 'Application launch was refused.'


def test_dialog_confirmation_can_authorise_central_execution(mock_config, monkeypatch):
    from jarvis.tools.registry import BUILTIN_TOOLS
    tool = BUILTIN_TOOLS['appControl']
    monkeypatch.setattr(tool, 'classify_safety', lambda args, cfg:
                        ConfirmationRequest(tool.name, SafetyTier.CONFIRM_DIALOG, 'close', 'Word', args))
    monkeypatch.setattr(tool, '_operate', lambda action, target, cfg: {'action': 'close_requested'})
    import threading
    from jarvis.tools.confirmation import set_result_handler
    shown, done, delivered = [], threading.Event(), []
    set_dialog_callback(lambda request, resolve: shown.append(resolve) or object())
    set_result_handler(lambda reply, ok: (delivered.append(reply), done.set()))
    route = FastMatch('app.close', 'apps', 'appControl', {'action': 'close', 'target': 'Word'})
    try:
        # The request returns immediately; Confirm runs the action on a worker.
        assert 'confirm' in dispatch(route, None, mock_config, 'Close Word').lower()
        shown[0](True)
        assert done.wait(5)
        assert 'close_requested' in delivered[0]
    finally:
        set_result_handler(None)


def enter_hot_window(obj):
    from jarvis.listening.state_manager import ListeningState
    obj.state_manager._state = ListeningState.HOT_WINDOW
    obj.state_manager._hot_window_start_time = time.time()
    obj.state_manager._hot_window_span_start = time.time() - 1


def test_normal_voice_dispatch_speaks_reply_and_schedules_hot_window(mock_config, db, dialogue_memory, monkeypatch):
    from collections import deque
    from jarvis.listening.listener import VoiceListener
    from jarvis.listening.echo_detection import EchoDetector
    mock_config.hot_window_enabled = True
    def forbidden(**kwargs):
        pytest.fail('Fast voice command reached judge')
    obj = make_listener(mock_config, forbidden)
    obj.db, obj.dialogue_memory = db, dialogue_memory
    obj.echo_detector = EchoDetector()
    obj._recent_audio_energy = deque([.1])
    obj._dispatch_query = VoiceListener._dispatch_query.__get__(obj)
    spoken = []
    def speak(reply, completion_callback=None, duration_callback=None, first_audio_callback=None):
        spoken.append(reply)
        duration_callback(.5)
        completion_callback()
    obj.tts = SimpleNamespace(enabled=True, speak=speak, is_speaking=lambda: False)
    process(obj, 'jarvis what time is it?')
    deadline = time.monotonic() + 5  # the reply is produced on the reply worker
    while not spoken and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(spoken) == 1 and spoken[0].startswith('Current time:')
    assert obj.echo_detector._tts_exact_duration == .5
    assert obj.state_manager._hot_window_activation_timer is not None
    assert dialogue_memory.get_recent_messages()[-1]['content'] == spoken[0]
    obj.state_manager.stop()


def test_short_hotkey_phrases_need_the_wake_word(mock_config, monkeypatch):
    """A bare "paste" in the hot window acts on whatever has focus, so it must not fast-dispatch."""
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.tools import registry
    original = dict(registry.BUILTIN_TOOLS)
    try:
        registry.configure_windows_tools(mock_config, platform='win32', start_index=False)
        for phrase in ('paste', 'copy that', 'save that', 'undo'):
            assert match_command(phrase, mock_config, 'en') is not None, phrase
            assert match_command(phrase, mock_config, 'en', addressed=False) is None, phrase
        assert match_command('press ctrl v', mock_config, 'en', addressed=False) is not None
        assert match_command('what time is it', mock_config, 'en', addressed=False) is not None
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def _with_windows_tools(mock_config):
    from jarvis.tools import registry
    original = dict(registry.BUILTIN_TOOLS)
    registry.configure_windows_tools(mock_config, platform='win32', start_index=False)
    return lambda: (registry.BUILTIN_TOOLS.clear(), registry.BUILTIN_TOOLS.update(original))


def test_hot_window_short_hotkey_goes_to_the_judge_not_the_keyboard(mock_config):
    from jarvis.listening.intent_judge import IntentJudgment
    restore = _with_windows_tools(mock_config)
    judged = []
    try:
        obj = make_listener(mock_config, lambda **kw: judged.append(kw) or IntentJudgment(
            True, 'paste', False, 'high', 'directed'))
        enter_hot_window(obj)
        process(obj, 'paste')
        assert judged and not obj.dispatched
        assert obj._request_addressed is False
        obj.state_manager.stop()
        obj = make_listener(mock_config, lambda **kw: pytest.fail('wake-worded hotkey reached the judge'))
        process(obj, 'jarvis paste')
        assert obj.dispatched == ['paste'] and obj._request_addressed is True
        obj.state_manager.stop()
    finally:
        restore()


def test_engine_runs_short_hotkeys_only_for_addressed_requests(mock_config, db, dialogue_memory, monkeypatch):
    from jarvis.reply import engine
    from jarvis.tools import registry
    restore = _with_windows_tools(mock_config)
    pressed = []
    monkeypatch.setattr(registry.BUILTIN_TOOLS['inputControl'], 'run',
                        lambda args, context: pressed.append(args) or ToolExecutionResult(True, 'Pressed.'))
    routed = []
    from jarvis.bridge import modes
    monkeypatch.setattr(modes, 'active_mode', lambda: modes.LOCAL)
    monkeypatch.setattr(engine, 'debug_log', lambda msg, *a: routed.append(msg))
    monkeypatch.setattr(engine, 'select_tools', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('model path')))
    try:
        engine.run_reply_engine(db, mock_config, None, 'paste', dialogue_memory, quiet=True)
        assert pressed == [{'action': 'hotkey', 'keys': 'ctrl+v'}]
        try:
            engine.run_reply_engine(db, mock_config, None, 'paste', dialogue_memory, quiet=True, addressed=False)
        except RuntimeError:
            pass
        assert len(pressed) == 1 and 'LLM_ROUTE' in routed
    finally:
        restore()

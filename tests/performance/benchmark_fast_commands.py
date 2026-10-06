"""Measure finalised-transcript routing with local models and real routine tools.

Run with the project Python, --output PATH. Capture and TTS are excluded.
Only the requested routine tool can execute; other model-selected tools are
reported and blocked. Memory is temporary and location/MCP enrichment is off.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from jarvis.config import load_settings
from jarvis.listening.intent_judge import create_intent_judge
from jarvis.listening.listener import VoiceListener
from jarvis.listening.state_manager import StateManager
from jarvis.listening.transcript_buffer import TranscriptBuffer
from jarvis.memory.conversation import DialogueMemory
from jarvis.memory.db import Database
from jarvis.reply import engine
from jarvis.tools.registry import configure_windows_tools
from jarvis.tools.types import ToolExecutionResult

CASES = [
    ('What time is it?', 'getTime', {}),
    ('Open Word', 'appControl', {'action': 'open', 'target': 'word'}),
    ('Set volume to 30%', 'systemVolume', {'action': 'set', 'percent': 30}),
    ('Pause the music', 'mediaControl', {'action': 'pause'}),
    ("What's using the most RAM?", 'systemInfo', {'action': 'top_memory'}),
]


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--model', default='qwen3.5:0.8b')
    opts = parser.parse_args()
    Path(opts.output).parent.mkdir(parents=True, exist_ok=True)
    cfg = replace(load_settings(), llm_chat_model=opts.model, fast_model=opts.model,
                  mcps={}, location_enabled=False,
                  location_auto_detect=False, location_cgnat_resolve_public_ip=False)
    assert cfg.llm_provider == 'ollama' and any(
        host in cfg.llm_base_url for host in ('localhost', '127.0.0.1'))
    configure_windows_tools(cfg)
    # Wait for discovery outside the measured path, like a ready daemon.
    from jarvis.platform.windows.apps import APP_INDEX
    APP_INDEX.applications()
    results = []
    for query, expected, expected_args in CASES:
        row = {'query': query, 'model': cfg.llm_chat_model, 'llm_called': False,
               'judge_called': False, 'collection_skipped': True, 'tools': [],
               'first_tool_ms': None, 'action_ms': None}
        db = Database(':memory:')
        memory = DialogueMemory()
        listener = VoiceListener.__new__(VoiceListener)
        listener.cfg, listener.db, listener.dialogue_memory = cfg, db, memory
        listener.tts = None
        listener.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0,
                                                _last_tts_text='', echo_tolerance=.3)
        listener.state_manager = StateManager(voice_collect_seconds=cfg.voice_collect_seconds)
        listener._transcript_buffer = TranscriptBuffer()
        listener._buffer_duration = 120
        listener._last_detected_language = 'en'
        listener._is_engaged = lambda: False
        listener._start_engagement = lambda: None
        listener._end_engagement = lambda: None
        listener._set_face_state_listening = lambda: None
        listener._clear_audio_buffers = lambda: None
        listener._intent_judge = create_intent_judge(cfg)
        real_judge = listener._intent_judge.judge
        def judge(*args, **kwargs):
            row['judge_called'] = row['llm_called'] = True
            return real_judge(*args, **kwargs)
        listener._intent_judge.judge = judge
        real_tool = engine.run_tool_with_retries
        def tool(*args, **kwargs):
            name = kwargs.get('tool_name', args[2] if len(args) > 2 else '')
            arguments = kwargs.get('tool_args', args[3] if len(args) > 3 else {}) or {}
            row['tools'].append({'name': name, 'args': arguments})
            if row['first_tool_ms'] is None:
                row['first_tool_ms'] = round((time.perf_counter() - started) * 1000, 1)
            permitted = name == expected and all(
                arguments.get(k) == v
                or str(arguments.get(k, '')).casefold() == str(v).casefold()
                or (k == 'target' and name == 'appControl' and str(arguments.get(k, '')).casefold() == 'word')
                for k, v in expected_args.items())
            # Discovery can supply the full canonical application name.
            if name == expected == 'appControl' and arguments.get('action') == 'open':
                permitted = str(arguments.get('target', '')).casefold() in ('word', 'winword', 'microsoft word')
            if not permitted:
                return ToolExecutionResult(False, None, 'Benchmark blocks unrelated actions.')
            result = real_tool(*args, **kwargs)
            row['action_success'] = result.success
            row['action_ms'] = round((time.perf_counter() - started) * 1000, 1)
            return result
        def record_llm(fn):
            def call(*args, **kwargs):
                row['llm_called'] = True
                return fn(*args, **kwargs)
            return call
        def dispatch(text):
            row['reply'] = engine.run_reply_engine(db, cfg, None, text, memory, language='en', quiet=True)
        listener._dispatch_query = dispatch
        stamp = time.time()
        transcript = cfg.wake_word + ' ' + query
        listener._transcript_buffer.add(transcript, stamp - 1, stamp, .1)
        started = time.perf_counter()
        with patch.object(engine, 'run_tool_with_retries', tool), \
             patch.object(engine, 'select_tools', record_llm(engine.select_tools)), \
             patch.object(engine, 'plan_query', record_llm(engine.plan_query)), \
             patch.object(engine, 'chat_with_messages', record_llm(engine.chat_with_messages)):
            listener._process_transcript(transcript, .1, stamp - 1, stamp,
                                         captured_during_tts=False, captured_tts_start_time=0)
            if listener.state_manager.is_collecting():
                row['collection_skipped'] = False
                while not listener.state_manager.check_collection_timeout():
                    time.sleep(.02)
                dispatch(listener.state_manager.clear_collection())
        row['total_ms'] = round((time.perf_counter() - started) * 1000, 1)
        listener.state_manager.stop()
        db.close()
        results.append(row)
        Path(opts.output).write_text(json.dumps(results, indent=2), encoding='utf-8')
        print('⏱️ ' + json.dumps({k: v for k, v in row.items() if k != 'reply'}), flush=True)


if __name__ == '__main__':
    main()

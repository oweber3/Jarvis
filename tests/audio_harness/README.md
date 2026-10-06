# WAV harness

Feeds audio arrays through the real `VoiceListener` frame loop
(`_audio_frames` → VAD → utterance finalisation → transcription queue →
transcriber → transcript handling) with no microphone, no speakers and a
simulated clock. Use it for anything that needs speech going into the
listener: segmentation, wake handling, barge-in, speaker checks.

## API

```python
from tests.audio_harness import ListenerHarness, fixture, silence, music_bed

clip = fixture("wake_command__jarvis_open_word__alan.wav")
with ListenerHarness() as harness:
    harness.play(clip.audio, text=clip.text, bed=music_bed(3.0), snr_db=10)
    harness.play(silence(1.5))          # let VAD close the utterance

harness.whisper_calls   # [WhisperCall(audio, start_time, end_time, text)]
harness.collections     # queries passed to _begin_collection ("" = bare wake)
harness.dispatched      # queries handed to the reply engine
harness.face_states     # orb states the listener set ("listening", "idle", ...)
harness.played          # [Played(start, end, text)] on the simulated clock
harness.listener        # the VoiceListener under test
```

- `ListenerHarness(cfg_overrides, transcriber=..., capture_rate=16000,
  block_ms=20, intent_judge=None)`
  - `cfg_overrides` replace fields of the default settings.
  - `transcriber(audio, start, end) -> str`. The default oracle returns
    the `text` of every clip played inside the utterance span, so tests
    measure segmentation and listener logic, not speech recognition.
    `WhisperTranscriber("small")` runs real faster-whisper on the CPU.
  - `capture_rate` simulates a 44.1 or 48 kHz microphone; audio is
    resampled before it reaches the listener.
  - `intent_judge` defaults to `None` (the text fallback); pass a stub to
    exercise judge paths without Ollama.
- `play(audio, text="", bed=None, snr_db=None)` feeds 16 kHz audio in
  real-time-sized blocks and advances the clock by its duration.
- `harness.now` is the simulated time (seconds since the epoch).

Inside the `with` block the harness:
- patches the listener's clock (`time` in the listener, state manager and
  latency modules) with `SimClock`;
- loads settings from defaults in a throwaway config
  (`JARVIS_CONFIG_PATH` is restored on exit), never the user's file;
- records face states instead of writing the state file a running
  Jarvis's orb reads.

It is safe to use from scripts as well as tests. Hot-window timers still
run on real `threading.Timer`s; drive TTS-dependent paths explicitly.

Audio helpers in `audio.py`: `load_wav`, `save_wav`, `silence`,
`white_noise`, `pink_noise`, `music_bed`, `mix(fg, bg, snr_db, offset)`,
`concat`, `pad`, `fit`, `resample`, `rms`, `to_int16`. All are seeded and
deterministic.

## Fixtures

`fixtures/` holds short Piper clips (16 kHz, 16-bit) described by
`fixtures/manifest.json`:

| kind | content |
|------|---------|
| `wake` | "Jarvis." in six voices |
| `wake_command` | "Jarvis, open Word." and "Jarvis, what time is it?" in one breath |
| `near_miss` | "Travis", "Service", "A jar of this", "Davis" |
| `speech` | ordinary sentences without the wake word |

`fixture(name)`, `fixtures_of_kind(kind)` and `all_fixtures()` load them.

Regenerate them (Piper voices in `.tmp/wake-word/voices`, or set
`JARVIS_HARNESS_VOICES`):

```bash
PYTHONPATH=src .mamba_env/python.exe -m tests.audio_harness.generate_fixtures
```

Voices used: `en_GB-alan-medium`, `en_GB-alba-medium`,
`en_US-lessac-medium`, `en_US-ryan-medium` and speakers 12 and 305 of
`en_US-libritts_r-medium`, from `rhasspy/piper-voices` on Hugging Face.
### Speaker verification and barge-in

- `ListenerHarness(speaker_verifier=..., with_tts=True)` injects a verifier and a `FakeTTS` that records `duck`, `unduck` and `interrupt` with the simulated time (`harness.tts.time_of("duck")`). `stubs.StubVerifier` answers from a script (verdicts and a score) for tests that do not need a model.
- `speakers.py` builds the corpus from Piper voices: one voice is the owner, others are other people and Jarvis's own TTS. `speakers.room_with_jarvis_talking(tts, user, user_over_tts_db=...)` is what the microphone hears while Jarvis speaks and someone talks over it. `speakers.available()` is false without the speaker model (`JARVIS_SPEAKER_MODEL`) and the Piper voices, and the model-backed tests skip.
- `python scripts/measure_voice_id.py all` reports verification accuracy, barge-in latency and the echo-cancellation evaluation on that corpus.

`piper_voices.synthesise(text, Voice(model, speaker))` renders any other
text to an array for larger corpora.

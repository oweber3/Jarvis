# Dictation Engine Specification

## Overview

WisprFlow-like dictation: hold a hotkey to record speech, release to type the
transcription into the focused application. Completely independent from the
assistant pipeline (no wake words, intent judge, profiles, or TTS).

## Configuration

| Key                            | Type   | Default (per-platform)                         | Description                                     |
|--------------------------------|--------|------------------------------------------------|-------------------------------------------------|
| `dictation_enabled`           | bool   | `true`                                         | Master switch for the feature                   |
| `dictation_hotkey`            | string | Win: `"ctrl+cmd"`, macOS/Linux: `"ctrl+alt"`   | Hold-to-record hotkey combination               |
| `dictation_filler_removal`    | bool   | `false`                                        | LLM-based filler word removal via Ollama        |
| `dictation_custom_dictionary` | list   | `[]`                                           | Custom replacements in `"wrong -> right"` format|

Defaults are aligned with WisprFlow. Modifier-only combos are supported
(e.g. `"ctrl+cmd"` activates when both keys are held, with no extra trigger
key required).

The hotkey is configurable as a dropdown in both the setup wizard and settings
window, with four preset options: `ctrl+alt`, `ctrl+cmd`, `ctrl+shift+d`,
`ctrl+shift`.

## Core Flow

### Hold-to-Dictate (Standard Mode)

1. **Press hotkey** → start recording audio into buffer, play start beep,
   set face to `DICTATING`, pause main voice listener.
2. **Hold hotkey** → audio frames accumulate in a dedicated
   `sounddevice.InputStream`.
3. **Release hotkey** → stop recording, play stop beep, set face to
   `DICTATION_PROCESSING`, transcribe via shared Whisper model, apply
   post-processing pipeline, paste result into focused app via clipboard,
   restore face to `IDLE`, resume main voice listener.

A tap released while recording is still starting can have its session
finish, end notice included, before its start notice is given. Once the
start notice has been given, the engine checks whether the press was
released meanwhile; if so it opens no microphone, and if its session has
already finished it gives the end notice again (or, while another dictation
is open, leaves it owed to that one, see Overlapping Dictations), so the
voice listener is always resumed.

The face therefore moves through three distinct states across a dictation
cycle: `DICTATING` while recording, `DICTATION_PROCESSING` while the captured
audio is being transcribed / post-processed / pasted, and back to `IDLE` once
the cycle completes. This gives the user visual confirmation that their voice
input has been accepted and is being processed.

### Overlapping Dictations

The hotkey can be held again while an earlier dictation is still being
transcribed or pasted. Each press is a session, and a session stays open
until it has been transcribed and pasted, or given up.

- The end notice (listener resumed, face `IDLE`) fires only when the last
  open session finishes. An earlier dictation that finishes while a newer
  one is open never resumes the listener or idles the face; its end is
  held back and fires when the newer one finishes, even if that newer press
  gives up without any notices of its own (Whisper not loaded).
- An earlier dictation does not switch the face to `DICTATION_PROCESSING`
  while a newer recording is open; the newer one keeps `DICTATING`.

### Hands-Free Mode (Double-Tap)

1. **Quick press-and-release** (hold < 0.4 s) followed by a **second tap**
   within 0.4 s → enters hands-free mode. Recording continues until
   explicitly stopped.
2. **Stop triggers** — re-press the hotkey *or* press Escape.
3. Same post-processing pipeline as standard mode.
4. If the hands-free recording is given up before it captures anything
   (the microphone fails to open or start), hands-free mode ends with it:
   the next press is an ordinary hold-to-dictate.

### Capture for a Caller

A caller that shows the text itself (the web chat's microphone button,
`webchat/webchat.spec.md`, Voice input) records through the engine without
the hotkey: `start_capture(max_seconds)` returns a token, then
`stop_capture(token)` returns the transcript, or `cancel_capture(token)`
discards the audio. `capture_blocker()` says why a capture could not start
now (`unavailable`, `not_ready`, `busy`), and `start_capture` raises
`CaptureRefused` with that reason, or `microphone` when the input device
cannot be opened.

- A capture is a dictation session: the same start, processing and end
  notices pause and resume the listener and move the face, the same beeps
  play, and the same input device, resampling and loudness levels apply.
- At most one capture is open, and never beside a hotkey recording or an
  earlier dictation still being transcribed (`busy`). A hotkey press during a
  capture starts nothing.
- The stream opens on the calling thread, which must not be the `pynput`
  hook thread.
- After `max_seconds` the microphone is released and the audio is kept for
  `stop_capture`. Cancelling, stopping and `DictationEngine.stop()` release
  the microphone and give the end notice.
- The transcript goes only to the caller: it is not pasted, not added to the
  history, not passed to `on_dictation_result` and never logged, including
  by filler removal. The custom dictionary and filler removal apply as in the
  post-processing pipeline. Audio shorter than 0.3 s or empty gives an empty
  transcript without calling Whisper.

## Silence and Whisper's invented phrases

Whisper turns silence and room noise into stock phrases ("Thanks for watching!"). Every
transcription, hold-to-dictate and a capture for a caller alike, applies two gates before any
post-processing:

1. **Silence gate.** Audio in which no 30 ms window reaches `voice_min_energy` (the level the
   voice listener counts as speech) gives an empty transcript without calling Whisper.
2. **No-speech gate.** A segment whose `no_speech_prob` is at or above
   `whisper_no_speech_threshold` is dropped, with the voice listener's own check
   (`is_whisper_hallucination`), on faster-whisper and MLX. Segments without a score are kept.

Dropped text is never logged; only the count is.

## Post-Processing Pipeline

After transcription, text passes through these stages in order:

1. **Custom dictionary** — case-insensitive whole-word regex replacements
   from `dictation_custom_dictionary`. Each entry is `"wrong -> right"`.
2. **LLM filler removal** (optional) — when `dictation_filler_removal` is
   enabled, sends the text to the local model on the background tier (the
   chat model in local reply mode, the fast model while Codex or Claude
   writes replies; see `llm/llm.spec.md`) with a prompt to remove filler words (um, uh, like, you know,
   etc.) while preserving meaning. Uses a 5-second timeout; falls back to the
   unprocessed text on failure.

## Architecture

- **`pynput`** for global hotkey detection (cross-platform).
- **Clipboard-based paste** (`Ctrl+V` / `Cmd+V`) for text insertion — more
  reliable than character-by-character typing, handles Unicode.
- **Shared Whisper model** via lazy reference (`lambda: voice_thread.model`)
  and backend info — no double memory usage.
- **Separate `sounddevice.InputStream`** for dictation audio — avoids
  modifying the complex listener code.
- **Pause flag** on the main listener to prevent dictation speech being
  interpreted as commands.
- **No PortAudio work on the pynput hook thread.** The hotkey callbacks only
  flip state; stream open/start runs on a worker thread (`_begin_recording`)
  and stop/transcribe already ran on one. Windows silently unhooks slow hook
  callbacks, and opening streams there raced other audio threads into a
  native abort (#462). If recording is stopped before the worker finishes
  opening the stream, the worker closes it rather than leaking a live capture.
- **All PortAudio stream lifecycle calls** (open/start/stop/close, and
  `sd.play` for beeps) run under the process-wide
  `jarvis.utils.audio_lock.portaudio_lock` — PortAudio documents stream
  open/close as not thread safe, and unserialised calls abort the whole app
  on Windows.

### Optional Dependencies

- `sounddevice`, `numpy`, and `pynput` are optional at import time: the
  module must always import cleanly even when they are broken or missing
  (e.g. `import sounddevice` raises **OSError**, not ImportError, when the
  PortAudio shared library is absent; `pynput` can raise display errors on
  headless Linux). Importers such as the setup wizard rely on this — a
  missing audio backend must never crash the app.

### Audio Device Handling

- The engine accepts an optional `voice_device` parameter, passed through from
  the daemon's configured device. Numeric indices and input-device names select
  that device; a missing named input fails instead of recording another device.
- The stream tries the selected device's native sample rate and mono input first.
  Unsupported formats trigger bounded channel-count and rate retries on the
  same input, including the Whisper target rate. Access and unavailable-device
  errors fail immediately.
- Multichannel samples are averaged to mono before transcription. The accepted
  sample rate is stored in `_stream_sample_rate`.
- If the stream rate differs from the Whisper target rate, audio is resampled
  via linear interpolation before transcription.
- While recording, each block's loudness is published as a single 0..1 number
  for the orb (`jarvis.voice_levels`, see `src/desktop_app/orb_widget.spec.md`);
  no audio leaves the engine this way.

## Edge Cases

| Case                      | Behaviour                                         |
|---------------------------|----------------------------------------------------|
| Whisper not yet loaded    | Play "not ready" beep (on the worker, never the hook thread), skip; no start notice, nothing recorded |
| Max recording duration    | Hold-to-dictate: no cap — the user controls when to stop by releasing the hotkey. A cap would paste prematurely mid-dictation and restart recording. A capture for a caller is capped at the caller's `max_seconds` |
| Empty transcription       | No paste occurs                                    |
| Silence or noise only     | Empty transcript (see Silence and Whisper's invented phrases); no paste |
| Concurrent with assistant | Dictation works independently; pauses listener     |
| macOS permissions         | `pynput` requires Accessibility permissions        |
| macOS 26+ (Tahoe)         | `pynput` disabled — TSM main-thread assertion crash |
| Linux / Wayland           | `pynput` requires X11 (limited Wayland support)    |
| Audio rate mismatch       | Resample via linear interpolation to Whisper rate  |
| LLM filler removal fails  | Falls back to raw transcription (5 s timeout)     |
| Custom dictionary empty   | No-op, text passes through unchanged               |

## Thread Safety

- `threading.Lock` around shared Whisper model transcription calls.
- Dedicated audio stream; never touches the listener's stream.
- The `pynput` key handlers (`_on_key_press` / `_on_key_release`) must return
  quickly — Windows silently removes low-level keyboard hooks that take more
  than ~5 s to return, leaving pynput in an inconsistent state that can
  segfault on the next `Controller` call from the paste thread. `_stop_recording`
  therefore only flips state under the lock and dispatches audio-stream
  teardown, beep playback, transcription, and paste to a background thread.
  The `discard=True` path keeps the synchronous teardown so shutdown can
  reliably wait for everything to finish.

## Beeps

Three short beeps generated the same way as the existing `TunePlayer` sonar ping:
- **Start beep** — higher pitch (700 Hz), signals recording started.
- **Stop beep** — lower pitch (440 Hz), signals recording stopped.
- **Not ready beep**: lowest and slightly longer (260 Hz), signals the
  hotkey was pressed before Whisper finished loading.

## Setup Wizard

The setup wizard includes a dedicated Dictation page (between Whisper and
Location steps) that allows users to:
- Enable/disable dictation
- Choose the hotkey from a dropdown of presets
- View tips about hold-to-dictate and double-tap hands-free mode

## Dependencies

- `pynput>=1.7.6` — global hotkey detection and keyboard simulation.

# Voice Output (Text-to-Speech)

Jarvis speaks replies with one of three local engines. Every engine runs on this PC: there is no cloud voice, not even an optional one. Voice files are downloaded once from pinned addresses, checked, and then used offline.

| File | Holds |
|------|-------|
| `tts.py` | `PiperTTS` (the streaming player), `KokoroTTS`, `ChatterboxTTS`, `create_tts_engine`, speech text preprocessing |
| `kokoro.py` | Kokoro model files, voices, ONNX session and synthesis |
| `piper_voices.py`, `piper_voices.json` | The pinned catalogue of English Piper voices |
| `model_download.py` | `download_verified`, shared by both engines |
| `voice_preview.py` | `render_preview` and `play` for the Settings preview |

## Configuration

The voice is plain config, so anything that names a voice (Settings, a personality) uses the same keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `tts_engine` | `piper` | `piper`, `kokoro` or `chatterbox`; any other value loads as `piper` |
| `tts_piper_voice` | `en_GB-alan-medium` | A catalogued Piper voice id; an unknown id loads as the default |
| `tts_piper_length_scale` | `0.65` | Piper speed (below 1.0 faster) |
| `tts_piper_model_path` | none | A Piper voice file of the user's own; wins over `tts_piper_voice` |
| `tts_kokoro_voice` | `bm_george` | One of `KOKORO_VOICES`; an unknown id loads as the default |
| `tts_kokoro_speed` | `1.0` | Kokoro speed, kept within 0.5..2.0 |
| `tts_kokoro_device` | `gpu` | `gpu` (graphics card) or `cpu` (processor); any other value loads as `gpu` |

Piper is the default for everyone. Nothing changes the engine or a default because of the hardware Jarvis runs on.

## Engines

- **Piper** (`PiperTTS`): synthesis yields one chunk per sentence and playback starts with the first chunk while the rest are synthesised. The player owns ducking, interruption, duration reporting, voice levels and extra voice outputs (`extensions/extensions.spec.md`, Voice outputs).
- **Kokoro** (`KokoroTTS`, a `PiperTTS` with a different voice loader): Kokoro-82M v1.0 on ONNX Runtime, using Piper's bundled espeak-ng for phonemes. It adds no Python package and no PyTorch. `KokoroVoice.synthesize` has the shape of `PiperVoice.synthesize`, so the same player streams it, with the same ducking, interruption and voice outputs.
- **Chatterbox** (`ChatterboxTTS`): voice cloning on PyTorch, synthesising the whole reply before playing. It feeds no voice outputs. The Settings row for the recording says to clone only your own voice or someone's who has consented.

### Kokoro

- Only English voices are offered (`a` American and `b` British; espeak voices `en-us` and `en`), matching the reply engine's English-only instruction, which applies to every engine.
- The first sentence of a reply is synthesised clause by clause, so speech starts once its first clause is ready; later sentences are synthesised whole while earlier ones play. A window longer than the model's 509 tokens is split at clause, then word, boundaries without dropping text.
- The model's padding silence is cut to 30 ms before and 50 ms after the speech, then a pause is added: 0.15 s after a sentence, 0.06 s after a clause spoken on its own.
- With `tts_kokoro_device` `gpu` the ONNX session uses the graphics card when the installed ONNX Runtime offers `CUDAExecutionProvider` (the `onnxruntime-gpu` build with the CUDA 12 libraries), otherwise the processor; `cpu` always uses the processor; a CUDA session that fails to start falls back to the processor. A short warm-up run at load keeps the first reply from paying the first-run set-up.
- **Never mute.** When Kokoro cannot speak (its files are not downloaded, ONNX Runtime or the model fails to load), `KokoroTTS` loads Piper instead, prints the reason and that Piper is speaking, and records it in `fallback_reason`; `engine_in_use` says which engine is loaded. Synthesis failing while speaking (for example the graphics card running out of memory) does the same: before any audio of the reply, Piper speaks the whole reply; after some audio the reply ends there; later replies use Piper. A model that fails to load is checked against its SHA-256 and a damaged file is removed, so it downloads again on the next start.
- **Downloads only when chosen.** A Piper user never fetches Kokoro files. When Kokoro is chosen and its files are missing, `start` downloads them on a background thread while Piper speaks; once all are verified, the next reply loads Kokoro on the speaking thread and switches, so a reply never changes voice half-way. An interrupt while Kokoro loads cancels that reply. A failed download leaves Piper speaking and says so. Restarting the engine never starts a second download while one runs, and `download_kokoro` downloads one at a time within a process.

## Voice files

- `download_verified(url, target, size, digest, algorithm, label, progress)` fetches to a `.part` file named for its process and thread (so two downloads of one file never share it), hashes while writing, and moves the file into place only when its size and digest match. The partial file is deleted whatever happens, and a failing progress callback (a closed Settings window) never stops the download. It returns whether a good copy is in place, which another download may have put there. An existing matching file is kept without a request. HTTP 429 is retried four times (2, 4, 8, 16 s). Progress lines use the `📥 <label>: NN%|| ...` form the desktop log viewer shows as a progress bar.
- **Piper**: `piper_voices.json` is a pinned copy of the English entries of the Piper voice catalogue at release v1.0.0, with each voice's `.onnx` and `.onnx.json` size and MD5 digest. Files come from `https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/`. A voice outside the catalogue is never fetched; the message says to place its files and choose them as a custom voice file. Voices live in `~/.local/share/jarvis/models/piper`.
- **Kokoro**: `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the kokoro-onnx `model-files-v1.0` release and `config.json` (the vocabulary) from `hexgrad/Kokoro-82M` at a fixed revision, each with its SHA-256 (about 354 MB in all, Apache-2.0 weights), in `~/.local/share/jarvis/models/kokoro`. `kokoro_files_present` checks every file exists with its pinned size.
- Nothing but the file requests is sent; there is no telemetry, and ONNX Runtime's telemetry is switched off before any session is created.

## Preview

`render_preview(values, text, progress)` builds the engine the daemon would build from the config `values` and returns the whole sample as samples, rate, the engine that spoke and the fallback reason. It first downloads what the choice needs (Kokoro's files, or a catalogued Piper voice not yet on disk), reporting progress. Chatterbox has no preview (`PreviewUnavailable`); a voice that cannot load raises `PreviewUnavailable` with the reason. `play` plays the samples on the default output and waits. The preview never writes `config.json`.

espeak-ng's voice is process-wide. Kokoro's phonemising holds a lock, but Piper's own does not, so in the packaged build (daemon and Settings in one process) a preview made while Jarvis is speaking can phonemise one sentence with the other voice's accent.

## Logging

`debug_log` (category `tts`) records: engine loads and their device, Kokoro fallbacks, downloads accepted or rejected (never the file contents), first audio latency, and preview renders. Reply text is never logged.

## Tests

`tests/test_kokoro_tts.py` (fake ONNX session; one test uses the real model when downloaded, or the folder in `JARVIS_TEST_KOKORO_DIR`), `tests/test_voice_downloads.py`, `tests/test_voice_preview.py`, `tests/test_config_voice.py`, `tests/test_settings_voice.py`, plus the Piper player suites `tests/test_piper_tts.py` and `tests/test_tts_voice_outputs.py`.

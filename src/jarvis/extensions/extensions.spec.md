# Local extensions

A local extension is a folder of Python code that adds to Jarvis without changing Jarvis's own files: tools, fast-path phrases, a settings page, an extra voice output, and work that starts and stops with the daemon. It is how personal or hardware-specific features (a home-built device, a private service) live beside a shared Jarvis checkout, so updates to Jarvis merge without touching them.

Extensions are the user's own code, trusted like `config.json`. Nothing is loaded unless the user names it. Everything an extension adds goes through the same paths as built-in features: its tools run through central safety and confirmation, its phrases are fast-path rules like any other, and its settings are ordinary `config.json` keys.

## Layout

| Module | Responsibility |
|--------|----------------|
| `extensions/loader.py` | Finding, importing and registering enabled extensions; installing their tools and phrases; start and stop |
| `extensions/api.py` | `ExtensionAPI`, the only object an extension receives; `SettingField`; the `VoiceOutput` and `VoiceSink` protocols |
| `<extensions_dir>/<name>/__init__.py` | One extension. Defines `register(api)` |

`extensions/` imports nothing from `platform/`, `desktop_app` or any model code. Jarvis core reaches extensions only through the loader's results (tools, phrase rules, voice outputs, settings pages); no core module names a particular extension. An extension keeps its own spec, tests and documents in its folder.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `extensions_enabled` | `[]` | Names of the extensions to load, in order. Empty loads nothing |
| `extensions_dir` | `""` | The folder holding extension folders. Empty means `extensions/` at the root of the Jarvis checkout |

- A name must be a folder directly inside `extensions_dir` containing `__init__.py`, and may use only letters, digits and underscores. A malformed or repeated name, or a missing folder, is skipped with a warning naming it.
- An extension's own settings are top-level `config.json` keys that it declares (see Settings). Jarvis keeps unknown keys on save, so an extension's keys survive whether it is enabled or not.

## Loading and lifecycle

During daemon start-up, after the built-in tools are configured and before the voice engine is created, the daemon:

1. **loads**: imports each enabled extension as a package under a private namespace (`jarvis_local_extensions.<name>`, once per folder, so its modules import each other relatively and module state survives a second load) and calls its `register(api)`;
2. **installs**: adds the extensions' tools to the tool registry and their phrase rules to the fast path;
3. **starts**: runs their start callbacks, in load order, then subscribes the state callbacks of each extension whose start callbacks all succeeded;
4. hands their voice outputs to the voice engine.

At shutdown it **stops** them: state callbacks are unsubscribed, stop callbacks run once in reverse order (only after a start), and the tools and phrase rules are removed. Stopping twice does nothing.

`register` only declares. Work that opens devices, sockets or threads belongs in a start callback, because the Settings window also loads enabled extensions (to show their pages) and never starts them.

A failure prints one line and Jarvis carries on:

- loading (a missing folder, an import error, no `register`, or an exception from `register`): `🧩 Extension <name> not loaded: <reason>`. Anything that extension declared is discarded and its modules are dropped, so the next load imports it afresh.
- a start callback raising: `🧩 Extension <name> did not start: <exception type>`. Its other start callbacks and other extensions still run; that extension's state callbacks are not subscribed, and its stop callbacks still run at shutdown so it can release what it opened.

Loading prints `🧩 Extensions: <names>` once for those that loaded. With `extensions_enabled` empty nothing is imported and nothing is printed. Logs carry extension names and exception types only.

## What `register(api)` can do

| Call | Effect |
|------|--------|
| `api.name`, `api.config` | The extension's name and the loaded `Settings` Jarvis itself uses. In the Settings window it holds every `config.json` value (defaults merged in) as an attribute, not normalised |
| `api.add_settings(label, fields)` | Declares `SettingField`s shown on one Settings page titled `label` (see Settings) |
| `api.setting(key)` | The `config.json` value of a declared setting, or its default. An undeclared key raises `KeyError` |
| `api.add_tool(tool)` | A `Tool` (`tools/base.py`) registered beside the built-in tools |
| `api.add_phrases(path)` | A fast-path phrase file (see Phrases) |
| `api.add_voice_output(output)` | An extra voice output (see Voice outputs) |
| `api.on_start(callback)` / `api.on_stop(callback)` | See Loading and lifecycle |
| `api.subscribe_state(callback)` | Receives every `assistant_state` change between start and stop, on the caller's thread (return quickly). An exception in it is logged and ignored |
| `api.record_device(device, tool, last_action)` | Makes `device` what a follow-up such as "turn it off" is about (`memory/desktop_referents.spec.md`). `device` is the name Jarvis shows, for example "robot head" |

Extensions use only these calls and the public `Tool`, `ToolExecutionResult`, `ConfirmationRequest`, `debug_log` and `assistant_state` interfaces. Reaching into other Jarvis internals is unsupported and may break on any update.

## Tools

- Extension tools are offered to the router, the planner, the chat loop, routines and the reply bridges exactly like built-in tools, and every call goes through `run_tool_with_retries` and `evaluate_safety`. The tool's `classify_safety` decides confirmation; nothing about an extension makes a tool safer.
- A tool whose name is already registered (a built-in tool or an earlier extension's) is refused with `🧩 Extension <name>: tool <tool> skipped, that name is already taken`; the extension's other tools still install.
- A tool whose results include personal data sets `personal_data = True`. It is then offered to a cloud reply mode only when that mode shares long-term memory, like the tools in `bridge/tools.py`'s `PERSONAL_DATA_TOOLS`.

## Phrases

A phrase file uses the `fastpath/phrases/<lang>.json` rule format (`id`, `family`, `tool`, `args`, `phrases`, `reply`, optional `slot_args` and `needs_wake_word`) under a top-level `rules` list; its file name is the language code (`en.json`). Its rules are added after the built-in rules for that language and follow `fastpath/fastpath.spec.md`: whole-utterance matches, SAFE actions only, a fall-through when unsure, and available only while their tool is registered. A file that cannot be read, has a malformed rule, or has a rule naming a tool the same extension did not install is ignored as a whole with `🧩 Extension <name>: phrases in <file> ignored (<reason>)`. So is one whose `phrases` is not a non-empty list of text or whose phrases do not compile with the locale's slots, or whose language has no built-in phrase file. As a second guard the fast path skips any extension rule that does not compile, so the built-in commands of a language always keep working.

## Voice outputs

A voice output receives Jarvis's spoken replies in addition to (or instead of) this PC's speakers. Only the Piper engine feeds voice outputs; with Chatterbox selected, each enabled output prints once that it needs Piper and stays silent.

```python
class VoiceOutput(Protocol):
    name: str                                    # shown in messages and logs
    def enabled(self) -> bool: ...               # read at the start of every reply
    def lead_ms(self) -> int: ...                # how long the PC voice waits so both line up (kept to 0..500)
    def open(self) -> Optional[VoiceSink]: ...   # this reply's sink, or None when unavailable
    def convert(self, samples_int16, rate: int) -> bytes: ...  # one synthesised chunk in the sink's format
    def scale(self, pcm: bytes, gains) -> bytes: ...           # per-sample gain (ducking, fades)
    # optional: def warm(self) -> None             called once on the thread that starts the voice engine
```

`VoiceSink` has `write(pcm)`, `end()` and `abort()`, none of which may block.

- At the start of each reply the engine offers the reply to the outputs in registration order; the first that is enabled and opens a sink speaks it. `open` raising counts as unavailable.
- Each chunk is converted on the synthesis thread and released to the sink from the audio callback in step with this PC's playback, the output running `lead_ms` ahead, so ducking, interruption and underruns affect both together. Echo timing includes the lead.
- The sink is ended when the reply has played and aborted on interruption. A failing conversion or write stops that output for the rest of the reply only, with a `debug_log` line; this PC's voice is never affected and an unavailable output never delays it.
- `tts_pc_speakers_enabled` turns this PC's voice off independently; with it off the reply keeps its timing and plays only through the output.
- `warm` lets an output load converters that pull in DLLs on the starting thread, where a load cannot deadlock with the listener's microphone probe.

## Settings

`SettingField(key, label, description, type, default, choices=None, min=None, max=None, step=None, suffix=None, nullable=False)`, with `type` one of `bool`, `int`, `float`, `str`, `choice`, `list`. The Settings window loads the enabled extensions without starting them and shows each one's fields on its own page after the built-in pages. Values are shown, validated and written exactly like built-in fields (only non-default values written, unknown keys preserved); a key is shown once (a built-in field first, then the first extension that declared it). An extension that fails to load adds no page. Changes take effect after a restart.

## Privacy

- Nothing is loaded by default, and loading reads only the named folders.
- An extension can reach the network or hardware like any Python code; it is the user's code and the user's responsibility. Jarvis adds no network access of its own for extensions.
- Git ignores `wifi_config.h` anywhere under `extensions/`, so device credentials kept beside firmware stay out of commits.

## Packaged builds

Extensions load from source checkouts. A packaged build loads them too, but only modules the build already contains can be imported, so an extension needing extra packages works only from source.

## Verification contract

Behaviours that automated tests assert with throwaway extensions written to a temporary folder (`tests/test_extensions.py`, `tests/test_settings_extensions.py`, `tests/test_tts_voice_outputs.py`, `tests/test_config_extensions.py`):

- Nothing is imported or printed when `extensions_enabled` is empty; a named extension is imported and registered once; its modules import each other relatively; loading again from the same folder keeps module state.
- A missing folder, an invalid name, an import error, a missing `register` and an exception in `register` each print one warning, leave nothing behind (no tools, callbacks or modules), and do not stop other extensions.
- An extension tool runs through central safety; a CONFIRM tool asks before running; a name clash is refused; a personal-data tool stays out of cloud snapshots unless memory is shared.
- Extension phrases match whole utterances for the extension's own tools; a file naming another tool, with phrases that are not a list or that do not compile is ignored with a warning and built-in commands keep working; the fast path skips an uncompilable rule; stop removes tools and phrases.
- Declared settings read their defaults and `config.json` values; an undeclared key fails the load; the Settings window shows an enabled extension's page last, saves its fields like built-in ones, lets `register` read any config value, shows a key two extensions declare once, shows no page for a disabled or broken extension and keeps a disabled extension's values.
- Start, state and stop callbacks run in order, once, and stop unsubscribes; a failing start callback warns, the others still start and that extension gets no state changes; `record_device` makes a follow-up referent.
- Voice outputs: the PC is delayed by the lead and the output runs that far ahead, also after an underrun; the lead is kept to 0..500 ms; output audio is released from the audio callback; the first enabled output that opens takes the reply; PC voice off plays silence with unchanged timing; a switched-off, unavailable or failing output (open, write or conversion) leaves the PC voice unchanged; interruption aborts the sink; ducking applies to both; switch changes apply from the next reply; Chatterbox names each enabled output once; outputs are warmed on the starting thread.

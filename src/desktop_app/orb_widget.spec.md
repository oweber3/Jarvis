# Orb Widget Specification

The face window shows a reactive holographic data sphere, the orb (`orb_widget.py`), hosted by `FaceWindow` in `face_widget.py`. `face_widget.py` also owns the file-backed `JarvisState` channel the daemon writes; core imports it through guarded imports and the orb only reads it.

## Layers

| Layer | Responsibility |
|-------|----------------|
| `orb_state_for` / `OrbState` | Map `JarvisState` values to visual states. Unknown values read as `OFFLINE`; `dictation_processing` reads as `THINKING`. `MUTED` and `ERROR` exist only as overrides |
| `state_colours` / `synthetic_speech_level` | The colour and accent of each state, and the speech-like envelope used when no real amplitude exists. Shared with the wake screen effect (`wake_overlay.spec.md`) so both read as one look |
| `build_fragments` / `Fragments` | Qt-free. The fixed, seeded set of 3D shell fragments, built once per process (`shared_fragments`) and never regenerated per frame |
| `OrbModel` | Qt-free animation maths. Smooths glow, shell rotation, the thinking glow (`scan_strength`), the output pulse, the voice sectors (`bars`), listening ripples and the wake flash towards per-state targets, and projects the shell for each frame (`shell_frame`) with numpy. Frame gaps are clamped |
| `OrbWidget` | `QPainter` renderer. Owns the timer, polls state (about 10 Hz), reads audio level, paints the model. Emits `clicked` on a left-button release over it; the host decides what a click means |
| `AudioLevelSource` | Thread-safe 0..1 level with staleness expiry. Producers `push`, the orb reads `current` |

## Holographic look: a data sphere

The orb is a see-through sphere made of light, in the spirit of a hologram assembled from data, not a solid ball or a tidy set of rings. It has no core: the shell's own layers fill it.

- **Shells inside shells**: about 2,800 fixed fragments. The dense outer shell and three nested inner layers (each a little sparser and dimmer than the one outside it) are made of curved filaments that follow the sphere (mostly along its latitudes), tiny circuit-like blocks and twinkling sparks, with eight broken rings at different tilts and sizes through the interior, faint long flow trails, and ragged fragments just past the rim, so the outline is never clean.
- **Depth and see-through**: the sphere is projected from 3D. The back shows through dimmer than the front, and every layer is brightest at its own rim, so the nested layers read as shells inside shells. The outer rim is the brightest band; the interior is filled but dimmer, so you still see into the sphere.
- **Layered motion**: three layers of the shell turn about a leaning vertical axis at their own speeds and directions, slowly at rest and faster when active. Everything is the same fragment set moved by rotation, so nothing is generated per frame.
- **Thinking lights it from within**: while thinking the inner layers brighten, so the interior glows while the shell turns faster.
- **Responds to the voice**: the shell is split into sectors round the centre (`bars`); with sound, the sectors swell outward and brighten in a travelling wave.
- **Light is additive**: fragments and glows add light over the dark backdrop; the brightest filaments and sparks get a soft bloom. A soft haze of the state colour surrounds the sphere, strongest at its rim, and swells a little with the output level.
- **Wake flash**: moving from a resting state (offline, idle, muted, error) into an active one (listening, thinking, speaking, dictating) fires one flash: a broken ring of light spreads out through the sphere from its centre and the whole shell flares, fading within about two seconds. Moving between active states never flashes.

## Visual states

| State | Look |
|-------|------|
| offline | Very dim slate shell, near static |
| idle | Dim but full cyan sphere, slow turning |
| listening | Brighter, the shell swells with the input level, broken ripples spread out |
| thinking | Indigo, the shell turns faster and the inner layers glow from within; no swelling (distinct from listening) |
| speaking | Brightest, the shell and its haze pulse with the output level |
| dictating | Green, listening-style response |
| muted / error | Slate / red, set via `set_state_override` |

The footer shows the state label, or a caption set with `set_caption` (the splash uses "starting up") until it is cleared.

Text is minimal: a `J.A.R.V.I.S.` header and a single state label footer.

## Principles

- **Lightweight**: the timer runs only while the widget is visible (show/hide events): about 32 FPS (30 ms) in an active state or during the wake flash, about 16 FPS (62 ms) at rest, where the motion is slow. The fragments are grouped by layer, so the shell is projected with one matrix product per layer, and drawn in batches by fragment kind, tone and brightness step (steps on a square-root scale so dim fragments still show). Each frame's strokes go into one numpy-backed `sip.array` that `QPainter` reads in place, so no Qt object is built per fragment. The faintest steps are drawn without antialiasing, short curves as one stroke, and only the very brightest get a bloom, so a frame costs about 3.5 ms at rest and 4.5 to 5 ms when speaking at the face window's default size.
- **Resizes**: geometry derives from the widget size, with space reserved for header and footer; tiny sizes paint without error.
- **Real amplitude is optional**: with no fresh level, listening stays calm and speaking uses a synthetic speech-like envelope. A producer that stops pushing never freezes the shell.
- **One palette**: the orb's state colours and backdrop come from `ORB_PALETTE` in `themes.py`, which the chat window's HUD palette is derived from. Dictating (green) and error (red) take their hues from the status colours in `ORB_PALETTE`. The splash screen hosts this widget rather than painting an orb of its own.
- **Core independence**: the orb depends on core only for `debug_log`; core never depends on the orb's internals.

## Audio level wiring

`get_audio_level_source()` is the default source. Producers connect by calling `push(level)`: mic RMS from the listener frame loop, and playback RMS from the TTS audio callback. Both producers run in the daemon, so in dev mode (daemon as subprocess) the level has to cross the process boundary the same way the state file does before the orb sees it.

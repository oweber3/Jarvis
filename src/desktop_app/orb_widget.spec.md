# Orb Widget Specification

The face window shows a reactive sphere of light, the orb (`orb_widget.py`), hosted by `FaceWindow` in `face_widget.py`. `face_widget.py` also owns the file-backed `JarvisState` channel the daemon writes; core imports it through guarded imports and the orb only reads it.

## Layers

| Layer | Responsibility |
|-------|----------------|
| `orb_state_for` / `OrbState` | Map `JarvisState` values to visual states. Unknown values read as `OFFLINE`; `dictation_processing` reads as `THINKING`. `MUTED` and `ERROR` exist only as overrides |
| `state_colours` / `synthetic_speech_level` | The colour and accent of each state, and the speech-like envelope used when no real amplitude exists. Shared with the wake screen effect (`wake_overlay.spec.md`) so both read as one look |
| `build_sphere` / `SpherePoints` | Qt-free. The fixed set of `SPHERE_POINTS` (5,000) points, built once per process (`shared_sphere`) and never regenerated per frame |
| `OrbModel` | Qt-free animation maths. Smooths glow, rotation, the voice level (`voice`), the thinking glow (`think`) and the wake flash towards per-state targets, and projects the sphere for each frame (`sphere_frame`) with numpy. Frame gaps are clamped |
| `OrbWidget` | `QPainter` renderer. Owns the timer, polls state (about 10 Hz), reads audio level, paints the model. Emits `clicked` on a left-button release over it; the host decides what a click means |
| `AudioLevelSource` | Thread-safe 0..1 level with staleness expiry. Producers `push`, the orb reads `current` |

## Look: a sphere of light that moves as one

The orb is a see-through sphere made of evenly spread points of light. There are no lines, streaks or trails: every point keeps its place on the sphere, and the whole surface moves together.

- **Two spheres**: four fifths of the points spread evenly over the outer sphere (equal area per point, so no clumps or gaps); the rest form a smaller, dimmer inner sphere that fills the middle and moves out of step with the outer one.
- **Depth and see-through**: the sphere is projected from 3D, seen from a little above. The back shows through dimmer than the front, and the outline is the brightest band. The brightest points are whiter and round; the rest are small dots of the state colour, and the inner sphere takes the accent colour.
- **Turning**: the sphere turns slowly about its vertical axis, faster while thinking.
- **The whole surface reacts to the voice**: a smooth shape flows over the sphere (`_surface_field`). At rest it barely moves; with sound the surface rises and falls with the level, reshaping the outline, the raised parts catch more light, and the shape flows faster. The level rises quickly and falls back more slowly, like a level meter.
- **Light is additive**: points and glows add light over the dark backdrop. A soft haze of the state colour surrounds the sphere, strongest at its rim, and swells with the voice.
- **Thinking lights it from within**: the inner sphere brightens, the sphere pulses gently, faint rings ripple across it and a band of light sweeps over it from top to bottom.
- **Wake flash**: moving from a resting state (offline, idle, muted, error) into an active one (listening, thinking, speaking, dictating) fires one flash: a soft ring of light spreads out from the centre and the sphere swells and flares, fading within about two seconds. Moving between active states never flashes.

## Visual states

| State | Look |
|-------|------|
| offline | Very dim slate sphere, near static |
| idle | Dim but full cyan sphere, slow turning, the surface barely moving |
| listening | Brighter; the surface swells and ripples with the input level |
| thinking | Indigo, turns faster, the inner sphere glows and a band of light sweeps over it; it ignores sound (distinct from listening) |
| speaking | Brightest; the surface and its haze move with the output level, reshaping further and flowing faster than listening does at the same level |
| dictating | Green, listening-style response |
| muted / error | Slate / red, set via `set_state_override` |

The footer shows the state label, or a caption set with `set_caption` (the splash uses "starting up") until it is cleared.

Text is minimal: a `J.A.R.V.I.S.` header and a single state label footer.

## Principles

- **Lightweight**: the timer runs only while the widget is visible (show/hide events): about 32 FPS (30 ms) in an active state or during the wake flash, about 16 FPS (62 ms) at rest. The sphere is projected with one matrix product per frame and drawn in batches by sphere and brightness step (steps on a square-root scale so dim points still show). Each frame's points go into one numpy-backed `sip.array` that `QPainter` reads in place, so no Qt object is built per point. Only the brightest steps are antialiased, so a frame costs about 3.5 ms at rest and under 4 ms when speaking at the face window's default size.
- **Resizes**: geometry derives from the widget size, with space reserved for header and footer; tiny sizes paint without error.
- **Real amplitude is optional**: with no fresh level, listening stays calm and speaking uses a synthetic speech-like envelope. A producer that stops pushing never freezes the orb.
- **One palette**: the orb's state colours and backdrop come from `ORB_PALETTE` in `themes.py`, which the chat window's HUD palette is derived from. Dictating (green) and error (red) take their hues from the status colours in `ORB_PALETTE`. The splash screen hosts this widget rather than painting an orb of its own.
- **Core independence**: the orb depends on core only for `debug_log`; core never depends on the orb's internals.

## Audio level wiring

`get_audio_level_source()` is the default source. Producers connect by calling `push(level)`: mic RMS from the listener frame loop, and playback RMS from the TTS audio callback. Both producers run in the daemon, so in dev mode (daemon as subprocess) the level has to cross the process boundary the same way the state file does before the orb sees it.

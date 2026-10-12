# Pointer Ring Specification

While gesture snapping is on (`jarvis/gestures/gestures.spec.md`, Gesture snapping), a small glowing ring surrounds the mouse pointer in the orb's holographic colours, so the pointer is easy to follow while the hand drives it. It is a companion of the orb (`orb_widget.spec.md`) and the wake screen effect (`wake_overlay.spec.md`) and shares their look.

## Layers

| Layer | Responsibility |
|-------|----------------|
| `RingModel` | Qt-free. Decides whether the ring is wanted from the gesture snapping flag and the `gesture_snap_pointer_ring` switch, and animates the fade and the breathing glow. Driven by `observe(flag)`, `set_enabled` and `step(dt)`, so it is testable with an injected clock |
| `ring_rect` | Qt-free geometry: the ring's window, `RING_SIZE` square, centred on the pointer |
| `paint_ring` | `QPainter` renderer of the ring |
| `PointerRing` | Qt controller. Polls the flag, shows and hides the one ring window, owns the follow timer. `configured_enabled` reads the switch from the config file |

## When it shows

- **Only while gesture snapping runs**: the controller reads `jarvis.gestures.snap_state.is_on()` about five times a second (every 200 ms). The flag is written by the daemon's gesture snapping with a heartbeat, so it reads off within about a second if the daemon dies; an unreadable flag, or a missing `snap_state` module, counts as off.
- **Off switch**: `gesture_snap_pointer_ring` (default on, Settings > Gesture Snapping > Pointer Ring). Saving Settings applies it at once, without a restart; turning it off removes the ring and stops polling.
- **Fades**: it fades in when the flag turns on and out when it turns off, each in `FADE_S` (0.2 s).

## Look

- One soft ring: a radial glow that is clear in the middle, rises to a thin bright ring a little over half way out and fades to nothing at the edge. The hollow middle (about a third of the radius) leaves the pointer's tip uncovered.
- Holographic: the glow is the listening colour from `state_colours` (cyan) and the thin ring alternates that colour, the accent (blue) and white round the circle, turning slowly, so it reads as the orb's light.
- Breathing: the glow swells and settles gently every `BREATH_PERIOD_S` (2.4 s), never going dark while shown.

## Behaviour rules

- **Never in the way**: one small window (`RING_SIZE`, 64 logical pixels; Qt scales logical pixels to each screen's DPI). It is frameless, always on top, a tool window (no taskbar button, no Alt+Tab entry), with a transparent background, transparent to input so clicks and scrolling pass through (`WindowTransparentForInput`, `WA_TransparentForMouseEvents`), and never takes focus (`WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, and `win32_overlay.make_never_activate` on Windows).
- **Follows the real pointer**: while visible, a precise timer at about 60 Hz (16 ms) reads `QCursor.pos()` and moves the window so it stays centred on it. It reads the pointer and does not care what moved it (the hand, the mouse or anything else).
- **Every monitor**: positions are Qt's logical global coordinates, so monitors left of or above the primary (negative coordinates) work the same.
- **No CPU when off**: the follow timer runs only while the ring is visible; once faded out the window is hidden and the timer stops, leaving only the light flag poll. The window is created on first show and deleted on shutdown.
- **Privacy**: nothing is captured, stored or sent. Debug logs (category `desktop`) mark the flag turning on and off, the ring hiding, the switch changing and an unreadable flag (once); never pointer positions.
- **Core independence**: lives in `desktop_app`; core knows nothing of it. The flag module is imported lazily inside the default reader.

## Manual check

`python -m desktop_app.pointer_ring` (with `src` on `PYTHONPATH`) shows the ring on its own while the flag is on, ignoring the switch, for example alongside `python -m jarvis.gestures --snap` in another console. It prints a start line, a line each time gesture snapping turns on or off, and a stop line; Ctrl+C quits.

## Wiring

`JarvisSystemTray` creates one `PointerRing` next to the `WakeOverlay`, starts it with the app, applies `gesture_snap_pointer_ring` after Settings are saved, and shuts it down on quit.

## Tests

`tests/test_pointer_ring.py`: the model (shows only with the flag and the switch on, fades within `FADE_S`, hides when the flag turns off, breathes), the geometry (centred on the pointer across negative-coordinate monitors), the controller on the offscreen platform with an injected flag and pointer (window flags, follows the pointer, hollow centre, hides and stops the timer, unreadable flag, switch, shutdown), the setting (default on, loaded, wrong type uses the default, offered in Settings) and the tray wiring.

# Wake Screen Effect Specification

When the user wakes Jarvis, the edges of every screen fill with soft holographic light that breathes and pulses with what Jarvis is doing, then fades away when the conversation ends. It is the screen-wide companion of the orb (`orb_widget.spec.md`) and works wherever the face window is, or when it is hidden.

## Layers

| Layer | Responsibility |
|-------|----------------|
| `OverlayModel` | Qt-free. Decides when the effect engages and disengages from the orb states it observes, and animates the fade, the breath, the wake heartbeat, the flash and the audio level |
| `edge_strips` / `frame_thickness` / `screens_to_cover` | Qt-free geometry: the four edge bands the effect is drawn in, how deep they are, and which screens to cover |
| `frame_interval_ms` / `ToneFade` | Qt-free pacing: how long to wait for the next frame, and the cross-fade from one colour palette to the next |
| `FramePainter` | `QPainter` renderer of one edge band of a screen's frame, in screen coordinates. Renders its glow in small steps (`prepare_next`) so it can be done ahead of showing it, and says whether its band looks different since it was last asked (`needs_repaint`) |
| `WakeOverlay` | Qt controller. Watches the state channel, builds and tears down the edge windows, owns the frame timer and the animation clock. `configured_enabled` reads the switch from the config file |
| `win32_overlay` | Windows only: `fullscreen_monitor_rect` finds the monitor a full-screen foreground app (a game, a full-screen video) covers, if any; `make_never_activate` adds `WS_EX_NOACTIVATE` to an edge window, which Qt's flags do not set |

## When it shows

- **Engages on wake only**: a change from idle to listening, which is what saying the wake word (or clicking the orb) does. With the wake word sound detector on, this happens as soon as the wake word is heard, and a sound-alike that the transcript does not confirm returns to idle, which fades it (`listening/listening.spec.md`, Wake word sound). A conversation already listening when the app starts, a typed chat request (idle to thinking) and dictation never engage it.
- **Follows the conversation**: once engaged it stays through listening, thinking and speaking, including the follow-up listening window after a reply.
- **Disengages** when the state leaves the conversation (idle, asleep, dictating, or anything unknown) and fades out in under a second. As a safety net it also fades out when the state has not changed for `MAX_UNCHANGED_S` (two minutes); the next wake engages it again.
- **Off switch**: `wake_overlay_enabled` (default on, Settings → Listening → Wake Screen Effect). Saving Settings applies it at once; turning it off removes any light on screen.

## Look: pulsing light

The whole effect is one soft glow hugging the screen edges, brightest at the edge and fading inward, and it pulses. There are no lines, ticks, brackets, moving lights or text.

- **Breathing**: the glow swells inward and brightens, then recedes, in a continuous rhythm set by the state. The rhythm changes smoothly when the state changes, never with a jump.

| State | Rhythm |
|-------|--------|
| listening | Calm, slow breath (about 3 seconds), cyan and blue; louder input brightens it |
| thinking | A quicker, crisper pulse (about once a second), indigo and sky |
| speaking | Follows the voice level, with a light breath under it, bright cyan and blue |

- **Wake heartbeat**: on wake the light flares in with two quick beats a third of a second apart, the first the strongest, then settles into breathing within about two seconds.
- **Holographic**: the glow has two tones that alternate round the screen (the state's colour and accent from `state_colours`, so the frame and the orb are one look) and fine horizontal scanlines, as on the orb.
- **Readable on any screen**: the glow is strong enough at the very edge to show on white as well as dark screens, and fades to nothing well before the middle.

## Smooth start and changes of colour

- **Rendered before shown**: on a wake, every window's glow is rendered in the state's palette before any window is shown, so the first frames cost no more than the later ones and nothing is rendered while the light fades in. Scanlines are cut a row at a time rather than painted as lines.
- **Time, not ticks**: the animation advances by the time that actually passed between frames, read from a monotonic clock, so a late frame lands where steady frames would have (one frame accounts for at most a quarter of a second, so a long stall or a sleep never fast-forwards the wake beats). The result does not depend on the frame rate.
- **Starts when it is on screen**: the animation clock starts once every window has painted its first frame (or after a quarter of a second if some never report in), not when the windows were built, so the wake beats and the fade-in are never spent while the windows are still being created.
- **Colour changes cross-fade**: when the state's palette changes, the new palette is rendered a little each frame (a few milliseconds at most) while the old one stays on screen, and only then fades in over about a third of a second. Layers are composited over each other and the glow keeps about the same strength through the fade. Re-tinting never happens inside a frame that is being drawn.

## Behaviour rules

- **Never in the way**: each screen gets four thin edge windows (top and bottom full width, left and right between them) instead of one full-screen window, so the middle of the screen is never covered and only the edge pixels are composited. The windows are frameless, always on top, tool windows (no taskbar button, no Alt+Tab entry), transparent to input (clicks pass through) and never take focus (`WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, `WS_EX_NOACTIVATE`).
- **Multi-monitor**: every screen is covered, using its full geometry in Qt's logical coordinates. A screen whose foreground app is full screen (and not merely maximised) is skipped so games and full-screen video are never overlaid; on other platforms no screen is skipped.
- **Clean up fully**: the edge windows exist only while the effect is visible. When it has faded out, or on disable or shutdown, the frame timer stops and every window is closed and deleted, and the rendered glow goes with them. Screens are read again at each wake, so monitors added or removed in between are handled.
- **Animates only while visible**: the frame timer runs only while the windows are up, on a precise timer so frames do not drift by whole ticks. While the light changes quickly (the wake beats, the fade in and the fade out) frames come every 16 ms (about 60 FPS); once it only breathes they come every 33 ms (about 30 FPS). While hidden, only a light state watch runs: Qt reports a change of the state file the orb reads, the file is read a few milliseconds later (once the write has finished; a file caught empty between the writer's truncate and write is given a few more milliseconds rather than read as an unknown state), and a poll about ten times a second stays underneath as a safety net, which also covers a missing state file.
- **Cheap to draw**: each band's glow is rendered once per palette at a shallow and a deep reach and the breath cross-fades between the two, so a frame is a few image draws per band. A band whose look has not changed to within 1/128 of full strength is not repainted. Only the palettes in use are kept in memory.
- **Audio is optional**: it reads the live voice level like the orb (`shared_voice_level`, `orb_widget.spec.md`); with no fresh level, speaking uses the orb's synthetic speech envelope.
- **Fails clean**: if building or showing the windows fails, whatever was built is removed and the next wake tries again; nothing stays half shown. After shutdown, wakes are ignored.
- **Local only**: nothing is logged beyond state changes, window counts, how long a wake took to render and show, and errors, and nothing leaves the PC.
- **Core independence**: lives in `desktop_app`; core knows nothing of it. The Windows API calls live only in `win32_overlay.py`, imported lazily and failing safe (no screen skipped, the window left as Qt made it).

## Wiring

`JarvisSystemTray` creates one `WakeOverlay`, starts it with the app, applies `wake_overlay_enabled` after Settings are saved, and shuts it down on quit.

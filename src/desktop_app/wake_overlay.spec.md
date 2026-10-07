# Wake Screen Effect Specification

When the user wakes Jarvis, the edges of every screen fill with soft holographic light that breathes and pulses with what Jarvis is doing, then fades away when the conversation ends. It is the screen-wide companion of the orb (`orb_widget.spec.md`) and works wherever the face window is, or when it is hidden.

## Layers

| Layer | Responsibility |
|-------|----------------|
| `OverlayModel` | Qt-free. Decides when the effect engages and disengages from the orb states it observes, and animates the fade, the breath, the wake heartbeat, the flash and the audio level |
| `edge_strips` / `frame_thickness` / `screens_to_cover` | Qt-free geometry: the four edge bands the effect is drawn in, how deep they are, and which screens to cover |
| `FramePainter` | `QPainter` renderer of one edge band of a screen's frame, in screen coordinates. Says whether its band looks different since it was last asked (`needs_repaint`) |
| `WakeOverlay` | Qt controller. Watches the state channel, builds and tears down the edge windows, owns the frame timer. `configured_enabled` reads the switch from the config file |
| `win32_overlay` | Windows only: `fullscreen_monitor_rect` finds the monitor a full-screen foreground app (a game, a full-screen video) covers, if any; `make_never_activate` adds `WS_EX_NOACTIVATE` to an edge window, which Qt's flags do not set |

## When it shows

- **Engages on wake only**: a change from idle to listening, which is what saying the wake word (or clicking the orb) does. A conversation already listening when the app starts, a typed chat request (idle to thinking) and dictation never engage it.
- **Follows the conversation**: once engaged it stays through listening, thinking and speaking, including the follow-up listening window after a reply.
- **Disengages** when the state leaves the conversation (idle, asleep, dictating, or anything unknown) and fades out in under a second. As a safety net it also fades out when the state has not changed for `MAX_UNCHANGED_S` (two minutes); the next wake engages it again.
- **Off switch**: `wake_overlay_enabled` (default on, Settings → Features → Wake Screen Effect). Saving Settings applies it at once; turning it off removes any light on screen.

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

## Behaviour rules

- **Never in the way**: each screen gets four thin edge windows (top and bottom full width, left and right between them) instead of one full-screen window, so the middle of the screen is never covered and only the edge pixels are composited. The windows are frameless, always on top, tool windows (no taskbar button, no Alt+Tab entry), transparent to input (clicks pass through) and never take focus (`WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, `WS_EX_NOACTIVATE`).
- **Multi-monitor**: every screen is covered, using its full geometry in Qt's logical coordinates. A screen whose foreground app is full screen (and not merely maximised) is skipped so games and full-screen video are never overlaid; on other platforms no screen is skipped.
- **Clean up fully**: the edge windows exist only while the effect is visible. When it has faded out, or on disable or shutdown, the frame timer stops and every window is closed and deleted. Screens are read again at each wake, so monitors added or removed in between are handled.
- **Animates only while visible**: the frame timer (30 ms, about 32 FPS on Windows) runs only while the windows are up. While hidden, only a light state watch runs (about 10 Hz, reading the same state file as the orb).
- **Cheap to draw**: each band's glow is rendered once at a shallow and a deep reach, tinted, and the breath cross-fades between the two, so a frame is two image draws per band. A band whose look has not changed is not repainted.
- **Audio is optional**: it reads the live voice level like the orb (`shared_voice_level`, `orb_widget.spec.md`); with no fresh level, speaking uses the orb's synthetic speech envelope.
- **Local only**: nothing is logged beyond state changes and window counts, and nothing leaves the PC.
- **Core independence**: lives in `desktop_app`; core knows nothing of it. The Windows API calls live only in `win32_overlay.py`, imported lazily and failing safe (no screen skipped, the window left as Qt made it).

## Wiring

`JarvisSystemTray` creates one `WakeOverlay`, starts it with the app, applies `wake_overlay_enabled` after Settings are saved, and shuts it down on quit.

# Gestures: hand tracking and gesture snapping

Webcam hand tracking that turns camera frames into hand landmarks for gesture control. This spec covers the hand-tracking pipeline, landmark recordings, the diagnostic command and gesture snapping, which moves windows into FancyZones zones and scrolls them with the user's hand. Gestures are built on the `HandFrame` stream the pipeline produces.

## Principles

- **Local only.** Detection runs on this machine with Google's MediaPipe Hand Landmarker on the CPU. No frame, landmark or result leaves the machine. The only network request is the one-time model download described below.
- **Frames stay in memory.** Camera frames are flipped, searched for hands and dropped. They are never written to disk, logged, sent anywhere or kept after detection, and Jarvis never shows them. Subscribers receive landmarks only, never pixels. The one place frames are shown is the diagnostic command's `--preview` window, which the user opens explicitly to see what the camera sees; it shows the newest frame live on screen and keeps or saves nothing.
- **The camera is on only while the pipeline runs.** `stop()` releases the camera, so the webcam light goes off. Nothing in Jarvis starts the pipeline on its own: the diagnostic command and the gesture feature start it explicitly.
- **Honest about failure.** A missing MediaPipe install or model keeps the pipeline off and says why once on the console. A busy or unplugged camera is retried in the background and reported once, with recovery reported once too.
- **Platform-neutral.** The pipeline uses OpenCV for capture and knows nothing of windows, tools, the reply engine or the LLM.

## Modules

| Module | Role |
|---|---|
| `gestures/hand_tracking.py` | Data model (`Landmark`, `Point`, `Hand`, `HandFrame`), the MediaPipe adapter, the OpenCV camera and `HandTracker` |
| `gestures/recording.py` | Writing a `HandFrame` stream to a JSON Lines file and reading it back |
| `gestures/__main__.py` | `python -m jarvis.gestures`: the diagnostic command |
| `gestures/window_snap.py` | The pure gesture model: hand measures (pinch, open palm), the One Euro filter, desktop clamping and `WindowSnap`, which turns `HandFrame`s into pointer moves, grabs, window moves and snaps |
| `gestures/window_snap_service.py` | `WindowSnapService`: one `HandTracker`, one `WindowSnap` and a watchdog, started and stopped together |
| `gestures/preview.py` | The diagnostic preview window and its drawing |
| `tools/builtin/gesture_control.py` | The `gestureControl` tool: start, stop and status, wiring the service to the Windows output |
| `platform/windows/window_snap.py` | The Windows output: monitor rectangles, the pointer, grabbing, moving and snapping windows (see `platform/windows/windows.spec.md`) |
| `utils/model_files.py` | Downloading a model file once and verifying it against a pinned SHA-256 (shared with face tracking) |

## Data model

All types are immutable.

- **`Landmark`** is an `IntEnum` of MediaPipe's 21 hand points: `WRIST` (0); `THUMB_CMC`, `THUMB_MCP`, `THUMB_IP`, `THUMB_TIP` (1 to 4); and `MCP`, `PIP`, `DIP`, `TIP` for `INDEX` (5 to 8), `MIDDLE` (9 to 12), `RING` (13 to 16) and `PINKY` (17 to 20).
- **`Point`** is a named tuple `(x, y, z)` of floats.
- **`Hand`**:
  - `side`: `"left"` or `"right"`, the user's own hand.
  - `score`: MediaPipe's confidence in `side`, 0 to 1.
  - `points`: 21 `Point`s in image coordinates of the user's (mirrored) view. `x` runs 0 to 1 from the user's left to their right, `y` 0 to 1 from top to bottom, and `z` is depth relative to the wrist on roughly the same scale as `x` (negative is nearer the camera).
  - `world`: 21 `Point`s in metres, centred near the middle of the hand, in the same mirrored orientation. These are independent of how far the hand is from the camera, which makes them the right input for sizes and distances such as a pinch.
  - `point(landmark)` returns `points[landmark]`; `world_point(landmark)` returns `world[landmark]`.
- **`HandFrame`**:
  - `time`: when the frame was captured, in seconds on the high-resolution monotonic clock (`time.perf_counter`).
  - `width`, `height`: the frame size in pixels, for converting `x`/`y` distances into a consistent aspect ratio.
  - `hands`: zero, one or two `Hand`s. Every processed frame is delivered, including frames with no hands, so subscribers see a hand leave.

## Mirroring

Each frame is flipped horizontally before detection, so the pipeline sees what a mirror would show. MediaPipe assumes this mirrored input when it labels hands, so `side` names the user's own hand and moving the hand to the user's right increases `x`. Flipping a camera frame before it reaches the pipeline therefore swaps every `side` and maps every `x` to `1 - x`.

## The model

The Hand Landmarker model (`hand_landmarker.task`, float16, version 1, Apache 2.0) is fetched from Google's MediaPipe model storage on the first start and saved under `~/.local/share/jarvis/models/`. It is verified against a pinned SHA-256 before it is saved and every time it is loaded; a file that does not match is replaced. After the first download, hand tracking works fully offline. A model that cannot be fetched or verified raises `ModelUnavailable`, which keeps the pipeline off.

`utils/model_files.ensure_verified_file(filename, url, sha256, what, fetch=None)` implements this for any model: it returns the path of a verified file, downloading it when missing or corrupt and writing through a `.part` file so a failed write never leaves a partial model in place. A download that fails or does not match the checksum raises `ModelUnavailable` with a message naming `what`, and nothing is saved.

## Detection

`MediaPipeHands(model_path, max_hands=2)` wraps the Hand Landmarker in video mode:

- It is called with a BGR frame (as OpenCV delivers it) and a capture time, and returns a tuple of `Hand`s.
- It flips the frame, converts it to RGB and runs detection with MediaPipe's default confidence thresholds (0.5).
- Video mode needs strictly increasing timestamps, so it never passes one that is not later than the last.
- MediaPipe is imported only when the adapter is created. Without it, creating the adapter raises `ImportError`.

## Camera

`OpenCVCamera(index)` opens the webcam at that index with OpenCV, asking for 640 x 480 at 30 frames per second to keep detection cheap. On Windows it tries Media Foundation first and then DirectShow, because some webcams open only through one of them; the automatic choice is never used, as it also probes depth-camera drivers. A backend counts as working only when it opens the camera and delivers a frame. `open()` returns whether the camera opened, `read()` returns a BGR frame or `None`, and `close()` releases it.

## `HandTracker`

`HandTracker(camera=0, *, hands_factory=None, camera_factory=None, max_fps=30, clock=time.perf_counter)` runs the pipeline. `camera` is the webcam index; the factories replace the MediaPipe adapter and the OpenCV camera in tests.

- **`start()`** loads the model and the adapter, then starts two threads and returns `True`. If MediaPipe is not installed, or the model or adapter cannot be loaded, it says why once on the console (`✋ Hand tracking is off: ...`, with a hint where one helps) and returns `False`; nothing is started and the camera is never opened. Calling `start()` while running returns `True` and changes nothing.
- **The capture thread** opens the camera and keeps only the newest frame, replacing any frame detection has not reached yet, so detection always works on the latest image and never falls behind. Frames are read no faster than `max_fps`. If the camera cannot be opened or stops delivering frames, it is closed and retried with back-off (1 second, doubling to at most 5 seconds) until it works or the tracker stops. The first failure prints `✋ Hand tracking cannot use camera N` once, with the hint that another app may be using it; recovery prints once.
- **The detection thread** takes the newest frame, runs detection, drops the frame and delivers a `HandFrame` stamped with the frame's capture time to every subscriber, in subscription order. A subscriber that raises is logged and skipped; the others still receive the frame. A frame that fails detection is skipped and reported once.
- **`subscribe(callback)`** registers a callback taking a `HandFrame` and returns a function that unsubscribes it. Callbacks run on the detection thread and should return quickly.
- **`stop()`** stops both threads, releases the camera and drops any pending frame. It is safe to call when not running and returns once the threads have finished (each joined with a 2 second limit).
- **`running`** is true between a successful `start()` and `stop()`.

Debug logs (category `gestures`) mark start, stop, the camera opening, failing and recovering, and hands appearing and disappearing, never every frame and never coordinates.

## Recordings

Recordings capture landmark streams for building and testing gesture logic without a camera. They hold landmarks only, never images, and are written only to a path the user names.

- **Format:** JSON Lines. The first line is the header `{"format": "jarvis-hand-frames", "version": 1}`. Each following line is one `HandFrame` as `{"t", "w", "h", "hands"}`, with `t` in seconds since the first recorded frame and each hand as `{"side", "score", "points", "world"}` (points as `[x, y, z]` lists, rounded to 5 decimal places).
- **`HandRecorder(path)`** is a subscriber: calling it with a `HandFrame` appends a line. `close()` flushes and closes the file. It is also a context manager.
- **`read_recording(path)`** yields the `HandFrame`s back in order, with `time` as written. A file without the header, or with a different format or a newer version, raises `ValueError`.

## Diagnostic command

`python -m jarvis.gestures` shows the pipeline's output so tracking can be checked on the real webcam.

- **Live (default):** `--camera N` (default 0) starts a `HandTracker` and prints a status line about once a second: frames per second, and for each hand its side, confidence and index fingertip position (or that it is waiting while no frame has arrived yet). It also prints when hands appear and disappear. `--seconds S` stops after `S` seconds; otherwise Ctrl+C stops it. `--record PATH` also writes a recording.
- **Gesture snapping:** `--snap` runs gesture snapping on the real desktop (Windows only) with the thresholds from the user's config file, whether or not `gesture_snap_enabled` is on, so it can be tried and tuned without restarting Jarvis. The camera is `--camera N` when given, otherwise `gesture_snap_camera`. It prints the same on and off lines as the tool and, about once a second, the side of the hand in view, its pinch ratio, its index curl, whether it reads as an open palm and the state, to help choose the thresholds. `--seconds` or Ctrl+C stop it, and stopping lets go of a held window and releases the camera. `--record PATH` also writes a recording.
- **Preview:** `--preview` (with the live readout or `--snap`) opens a window showing the camera's mirrored image as the detector sees it, with each hand's 21 landmarks and bones drawn on it, the thumb and index tips marked, and a caption with the hand's side, pinch ratio, index curl, pose and, with `--snap`, the gesture snapping state. It uses PyQt6 and stays above other windows; the console readout continues once a second. The frame is taken from the camera before detection and only the newest one is kept for drawing; closing the window stops the command like `--seconds` does.
- **Photo:** `--image PATH` runs detection once on a photo, treated as a camera frame (so it is mirrored), prints what was found and exits.
- Failures to start print the same one-time reason as `start()` and exit with status 1.

## Gesture snapping

Gesture snapping moves and scrolls windows with the hand: the pointer follows the index fingertip as an aim, a fist grabs the window under it, the window follows the hand, and opening the hand snaps it into the zone under the pointer. A pinch scrolls the window under the pointer. It never clicks.

### Input from the user's own hand: an exception to "Jarvis never moves the pointer"

Jarvis's automation never moves the pointer or sends mouse input on its own (`platform/windows/ui_automation.spec.md`): it acts through UI Automation patterns, never through coordinates. Gesture snapping is a deliberate, narrow exception. It is not Jarvis acting: it relays the user's own hand in real time, like a pointing device. Every pointer move, window move and scroll comes from a hand movement the camera saw a fraction of a second earlier; no model, tool result, routine or other code chooses a position, a window or an amount. It sends mouse-wheel scrolling, but never presses a mouse button, never clicks and never types. Nothing outside `WindowSnap` sends this input, and `WindowSnap` sends it only from `HandFrame`s while the user has the mode on. The Windows code that moves the pointer and windows and scrolls for it (`platform/windows/window_snap.py`) is used by gesture snapping only and offers no tool action that moves or scrolls anything.

### Turning it on and off

- **Off by default.** `gesture_snap_enabled` (default `false`, Settings > Gesture Snapping) offers the `gestureControl` tool and its fast-path phrases. With it off the tool is not registered, nothing can start the camera for gesture snapping, and the phrases fall through to the model, which has no tool for them.
- **Started only when asked.** The setting does not start the camera. The user starts the mode by voice or text ("start gesture control") and stops it the same way ("stop gesture control"). Both go through `gestureControl`, the normal tool path and central safety. Both actions are routine (`SAFE`), so the phrases are fast-routed. The camera runs only while the mode is on.
- **Windows only.** The tool is registered only on Windows, where the output exists.
- **Not offered to cloud models.** `gestureControl` is never in a Codex or Claude tool snapshot (`bridge/bridge.spec.md`): a cloud model cannot turn the camera on. The fast-path phrases work in every reply mode because the fast path runs before any bridge.
- **Stopping lets go.** `stop()` leaves a held window where it is (no snap) and ends a scroll, then stops the hand tracker, which releases the camera. It is safe to call at any time and returns once both are done. The daemon calls it on shutdown, and the service also registers it to run when the process exits normally.

### Gestures

| Gesture | Effect |
|---|---|
| Point (index finger straight, the other fingers curled) and move | The pointer follows the index fingertip (landmark 8) as an aim. Nothing is clicked |
| Open palm (index, middle, ring and little fingers all extended) | Clutch: the pointer stays where it is, so the hand can be moved back to a comfortable spot |
| Hand out of view | Clutch, as for an open palm |
| Fist (index finger curled too) | Grabs the window under the pointer and brings it to the front. While the fist is held, the window follows the hand with the pointer |
| Open the fist | Snaps the held window into the zone under the pointer |
| Open palm while holding a window | Cancels: the window goes back to where it was, maximised again if it was |
| Pinch (thumb tip to index tip, index straight) and move up or down | Scrolls the window under the pointer, which stays still. Moving the hand up moves the content up, as when dragging a page on a touch screen |
| Let go of the pinch | Ends the scroll; the pointer follows the hand again |

Only one hand drives. `gesture_snap_hand` chooses it: `right`, `left` or `any` (default). Once a hand drives, only it is followed: a frame where it is missing counts as a frame without a hand, even when another hand is in view, and when two hands carry the same side label the one nearest the driving fingertip's last position is followed. Another hand can take over only after the clutch state is reached.

### Measures

Every measure is a ratio of two lengths on the same hand, so none changes with the hand's distance from the camera.

- **Hand size** is the distance from the wrist (0) to the middle finger's base (`MIDDLE_MCP`, 9).
- **Pinch ratio** is the distance from the thumb tip (4) to the index tip (8) divided by the hand size, both measured in the camera image in pixels (image `x` times the frame width, `y` times its height). The tracker's depth estimate is deliberately left out: measured on a real webcam with the hand side-on to the camera (thumb and index pointing up), the 3D distance between touching fingertips stayed between 0.30 and 0.41 of the hand size, overlapping an open hand, while in the image a pinch read 0.14 to 0.18 and an open hand 0.42 to 0.76.
- **Index curl** is the distance from the wrist to the index tip divided by the distance from the wrist to the index middle joint (PIP), on the `world` landmarks (metres). A straight index reads above 1 and a curled one below. Measured on a real webcam with the hand front-on: pointing read 1.08 to 1.24 (median 1.12), a pinch 1.10 to 1.17 and a fist 0.69 to 0.73 once closed.
- **A finger is extended** (on the `world` landmarks) when its tip is farther from the wrist than its middle joint by a factor of more than 1.2. An **open palm** has all four fingers extended.

A fist's thumb lies close to the index finger (its pinch ratio read 0.04 to 0.47 in the same measurement), so a pinch counts only while the index finger is straight.

### Thresholds, hysteresis and debounce

- **Fist.** A fist starts when the index curl stays below `gesture_snap_fist_start` (default 0.85) for `gesture_snap_debounce_ms` (default 50 ms), and ends when it stays above `gesture_snap_fist_release` (default 1.0) for the same time.
- **Pinch.** A pinch starts when the pinch ratio stays below `gesture_snap_pinch_start` (default 0.25), with the index curl above `gesture_snap_fist_release`, for the debounce time. It ends when the pinch ratio stays above `gesture_snap_pinch_release` (default 0.35) for the debounce time, or at once when the hand starts to make a fist (index curl below `gesture_snap_fist_start`).
- Between a start and its release threshold a gesture keeps its state, so a value that hovers near one threshold cannot flicker. Each release must be greater than its start; a configuration where it is not is corrected on load to the start plus 0.1 for the pinch and plus 0.15 for the fist.
- **The aim is frozen while the hand closes.** As soon as the pinch ratio falls below `gesture_snap_pinch_release` or the index curl below `gesture_snap_fist_release`, the pointer stops at its current position, the anchor, until the hand opens again without a gesture, or a fist grabs or a pinch scrolls at the anchor. The fingers' own movement while closing therefore never moves the aim. While a release is being confirmed the held window and the pointer also stay still, so letting go does not shift the drop point.

### Grabbing, moving and snapping

- **Grab.** The window grabbed is the top-level window under the anchor. The desktop, the taskbar, a point over no window and a window still settling from the previous snap grab nothing; the pointer then stays at the anchor until the fist opens. A maximised window is restored first, keeping the pointer at the same share of its width. The grabbed window is brought to the front without taking the keyboard focus. Nothing in a grab waits for the application.
- **Move.** While the fist is held the pointer follows the hand as when aiming, and the window moves with it, keeping the pointer at the same place on the window. Moves are posted to the window without waiting for it, so a slow application never holds up tracking.
- **Snap.** Opening the fist places the window into the zone under the pointer. The zones are the FancyZones layouts the user set up in Microsoft PowerToys, read the same way voice placement reads them (only while `windows_fancyzones_enabled` is on, `platform/windows/apps_paths.spec.md`), for the monitor under the pointer on the current virtual desktop. A pointer in the spacing between zones snaps to the nearest zone on that monitor. A monitor without zones, PowerToys not being installed or FancyZones zones switched off leave the window where it was dropped. Placement uses the shared `windows_mgmt.place_window` (visible frame, DPI-aware, verified) on a worker thread, so tracking continues while the window settles.
- **Cancel.** An open palm while a window is held puts it back where it was grabbed, maximised again if it was, with its own restored size intact, on a worker thread.
- **Lost hand.** If the hand stays out of view for 0.25 s while a window is held, the window is left where it is, not snapped.
- **Failures.** A window that refuses to move (a higher-privilege application, or one that closed) is let go and reported once on the console; tracking continues.

### Scrolling

- A pinch freezes the pointer at the anchor, so the wheel events reach the window under it, and turns vertical movement of the fingertip into mouse-wheel scrolling. `gesture_snap_scroll_speed` (default 5) is the number of wheel notches (120 units each) for a movement of a tenth of the camera's height. Movement is accumulated and sent in steps of at least 30 units, so slow movements scroll smoothly without a flood of events. Horizontal movement is ignored.
- Moving the hand up scrolls the content up (towards the end of a page), as when dragging a page on a touch screen.
- A scroll the window refuses (a higher-privilege application) is reported once; tracking continues.

### Moving the pointer

- **Comfort box.** The pointer moves like a mouse or trackpad: each fingertip movement moves the pointer by the same movement scaled up. The scale is set so that moving the fingertip across `gesture_snap_comfort_box` of the camera's width (default 0.5, half of it) carries the pointer across the whole virtual desktop, from the left edge of the leftmost monitor to the right edge of the rightmost. Up and down use the same scale. Small arm movements therefore cover every monitor.
- **Starting point.** When the hand starts driving (on starting the mode, after a clutch or after the hand returns) the pointer continues from where it is, read from the system, so it never jumps, and moving the real mouse in between is respected.
- **Smoothing.** The fingertip position (scaled to desktop pixels) is smoothed with a One Euro filter: strong smoothing when the hand is still, little lag when it moves fast. `gesture_snap_smoothing` (default 1.0) is the filter's minimum cutoff in hertz (lower is steadier but slower) and `gesture_snap_speed_response` (default 0.005) its speed coefficient (higher reacts faster during quick movements). The filter restarts when the hand is lost. Scrolling uses the same smoothed fingertip.
- **Bounds.** The pointer is kept inside the desktop: a position outside every monitor is moved to the nearest point of the nearest monitor, so on desktops whose monitors do not form a rectangle the pointer never lands in a gap. Pushing against an edge works like a mouse: moving back starts moving the pointer straight away. The monitor layout is read when the mode starts.

### Losing the hand

- A frame without the driving hand stops the pointer. If the hand stays out of view for 0.25 s (timed by when frames arrive, not when they were captured, so slow detection never drops a held window) the mode returns to the clutch state: a gesture in progress is cancelled, a scroll ends and a held window is left where it is.
- A watchdog thread checks every 50 ms, so this also happens when frames stop arriving altogether (a camera that stops, unplugs or freezes).

### `WindowSnap` and `WindowSnapService`

- **`WindowSnap(output, settings, clock)`** is the gesture model. `output` is the platform output: `desktop()` returning monitor rectangles `(left, top, right, bottom)` with exclusive right and bottom edges, `position()` and `move_to(x, y)` for the pointer, `grab(x, y)` returning a grabbed window (an object with the window's outer frame as `rect`) or `None`, `move_window(window, left, top)`, `drop(window, x, y)` to snap it, `put_back(window)` to cancel, and `scroll(delta)` for wheel units. Tests use a fake. `on_frame(frame)` handles one `HandFrame`, `tick()` runs the watchdog check, and `close()` lets go of a held window. It is thread safe: frames arrive on the detection thread, ticks on the watchdog thread and `close` on whichever thread stops the mode.
- **`WindowSnap.state`** names the current state for display: `paused`, `aiming`, `hand closing`, `holding`, `holding nothing` (a fist over the desktop) or `scrolling`. `WindowSnapService.state` is that, or `off`.
- **`WindowSnapService(cfg, output_factory, tracker_factory=HandTracker)`** owns one tracker, one `WindowSnap` and the watchdog thread. `start()` reads the monitors, starts the tracker on `gesture_snap_camera` (default 0) and subscribes the model; it returns `"started"`, or `"already"` when it was on, and raises `WindowSnapUnavailable` with a plain reason (the displays could not be read, or hand tracking could not load) when it cannot start, in which case nothing is left running. A camera that is busy or unplugged does not stop it from starting: the tracker retries it in the background and says so on the console. `stop()` is described above. `running` says whether the mode is on. A failure from the output is reported once on the console and logged once until that kind of call works again.
- One service exists per daemon (`window_snap_service.get_service`).

### The `gestureControl` tool

| Action | Behaviour |
|---|---|
| `start` | Starts gesture snapping. Reports success with a one-line reminder of the gestures, or that it was already on, or why it could not start (hand tracking unavailable, displays unreadable) |
| `stop` | Stops it, letting go of a held window and releasing the camera. Reports that it is off (also when it was already off) |
| `status` | Whether gesture snapping is on |

Console output: `🖐️ Gesture snapping on` with the gestures indented below, and `🖐️ Gesture snapping off`.

### Fast path

The `gesture_control` family in `fastpath/phrases/<language>.json` maps whole utterances such as "start gesture control", "turn on gesture snapping", "stop gesture control" and "turn off gesture snapping" to `gestureControl` `start` and `stop` (`fastpath/fastpath.spec.md`). It is available only while the tool is registered.

### Privacy and logs

Gesture snapping adds nothing that leaves the machine and nothing written to disk. Debug logs (category `gestures`) mark starting and stopping, clutch, a grab (or a fist that grabbed nothing), a snap (whether a zone was found), a cancel, the start and end of a scroll, and a window left where it is because the hand was lost or the mode stopped. They never contain coordinates, measures, scroll amounts, window titles or process names.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `gesture_snap_enabled` | `false` | Offer gesture snapping (the camera still starts only when asked) |
| `gesture_snap_camera` | `0` | Webcam index |
| `gesture_snap_hand` | `any` | `any`, `right` or `left` |
| `gesture_snap_comfort_box` | `0.5` | Fraction of the camera's width that spans the whole desktop (0.15 to 1.0) |
| `gesture_snap_fist_start` | `0.85` | Index curl below which a fist starts (0.3 to 1.2) |
| `gesture_snap_fist_release` | `1.0` | Index curl above which a fist ends and a pinch may start (0.4 to 1.5, greater than the fist start) |
| `gesture_snap_pinch_start` | `0.25` | Pinch ratio below which a pinch starts (0.05 to 1.0) |
| `gesture_snap_pinch_release` | `0.35` | Pinch ratio above which a pinch ends (0.1 to 1.5, greater than the pinch start) |
| `gesture_snap_debounce_ms` | `50` | How long a gesture or its release must last to count (0 to 500 ms) |
| `gesture_snap_scroll_speed` | `5` | Wheel notches per tenth of the camera's height (0.5 to 30) |
| `gesture_snap_smoothing` | `1.0` | One Euro minimum cutoff in hertz (0.05 to 10) |
| `gesture_snap_speed_response` | `0.005` | One Euro speed coefficient (0 to 0.1) |

Values outside the range are clamped on load; values of the wrong type use the default. All are shown in Settings > Gesture Snapping. Changes take effect after Jarvis restarts.

## Dependencies

`mediapipe==0.10.31` provides the Hand Landmarker; it needs no OpenCV build of its own (Jarvis's `opencv-python-headless` is used for capture) and requires `sounddevice` 0.5. `0.10.30`'s Windows build cannot load its native library, and later releases import matplotlib on start and require a second OpenCV package.

## Tests

- **Unit** (`tests/test_hand_tracking.py`, `tests/test_hand_recording.py`, `tests/test_model_files.py`): the pipeline against a fake camera and a fake detector (delivery order, newest-frame-only, failing subscribers, camera failure and recovery, start-up failures, stop releasing the camera), recordings round-tripping, and the model file helper.
- **Gesture snapping** (`tests/test_window_snap.py`, `tests/test_window_snap_service.py`, `tests/test_gesture_control_tool.py`, `tests/test_windows_window_snap.py`, `tests/test_gesture_preview.py`): the gesture model on synthetic `HandFrame`s against a fake output (fist and pinch thresholds, hysteresis and debounce, the frozen aim, grab, move, snap, cancel, scrolling, clutch, a held window let go on hand loss and by the watchdog, the comfort-box scale and bounds across several monitors), zone choice, the service and tool against a fake tracker, and the preview drawing. The pinch thresholds come from a calibration on a real webcam (see Measures); the synthetic hands reproduce its side-on case, where the tracker's depth is off.
- **Integration** (`tests/test_hand_tracking_mediapipe.py`): the real model on photos of hands from MediaPipe's test assets in `tests/fixtures/hands/`. Skipped when MediaPipe or the cached model is missing; tests never download.

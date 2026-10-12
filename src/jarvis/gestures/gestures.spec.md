# Gestures: hand tracking and gesture snapping

Webcam hand tracking that turns camera frames into hand landmarks for gesture control. This spec covers the hand-tracking pipeline, landmark recordings, the diagnostic command and gesture snapping, which moves windows into FancyZones zones and scrolls them with the user's hand. Gestures are built on the `HandFrame` stream the pipeline produces.

## Principles

- **Local only.** Detection runs on this machine with Google's MediaPipe Hand Landmarker on the CPU. No frame, landmark or result leaves the machine. The only network request is the one-time model download described below.
- **Frames stay in memory.** Camera frames are flipped, searched for hands and dropped. They are never written to disk, logged, sent anywhere or kept after detection, and Jarvis never shows them. Subscribers receive landmarks only, never pixels. The one place frames are shown is the diagnostic command's `--preview` window, which the user opens explicitly to see what the camera sees; it shows the newest frame live on screen, keeps or saves nothing, and on Windows is kept out of every screen capture, so the camera image never reaches a screenshot (including the `screenshot` tool and so a model), a screen share or a recording.
- **The camera is on only while the pipeline runs.** `stop()` releases the camera, so the webcam light goes off. Nothing in Jarvis starts the pipeline on its own: the diagnostic command and the gesture feature start it explicitly.
- **Honest about failure.** A missing MediaPipe install or model keeps the pipeline off and says why once on the console. A busy or unplugged camera is retried in the background and reported once, with recovery reported once too.
- **Platform-neutral.** The pipeline uses OpenCV for capture and knows nothing of windows, tools, the reply engine or the LLM.

## Modules

| Module | Role |
|---|---|
| `gestures/hand_tracking.py` | Data model (`Landmark`, `Point`, `Hand`, `HandFrame`), the MediaPipe adapter, the OpenCV camera and `HandTracker` |
| `gestures/recording.py` | Writing a `HandFrame` stream to a JSON Lines file and reading it back |
| `gestures/__main__.py` | `python -m jarvis.gestures`: the diagnostic command |
| `gestures/window_snap.py` | The pure gesture model: finger curl and the raised-finger count, the One Euro filter, desktop clamping and `WindowSnap`, which turns `HandFrame`s into pointer moves, grabs, window moves and snaps |
| `gestures/window_snap_service.py` | `WindowSnapService`: one `HandTracker`, one `WindowSnap` and a watchdog, started and stopped together |
| `gestures/snap_state.py` | The shared on flag (with heartbeat) the desktop app's pointer ring reads |
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
  - `world`: 21 `Point`s in metres, centred near the middle of the hand, in the same mirrored orientation. These are independent of how far the hand is from the camera, which makes them the right input for sizes and distances such as how far a finger is folded.
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
- `max_hands` is the most hands it looks for. With one, MediaPipe follows that hand from frame to frame and does not search the rest of every frame for a second one, which more than halves the cost of a frame with a hand in view. The landmarks of a hand are the same either way; the only difference is which hand is reported when two are in view.
- It runs on the CPU. MediaPipe's GPU delegate is not used: on Windows it is not available, and asking for it still runs the model on the CPU.
- It flips the frame, converts it to RGB and runs detection with MediaPipe's default confidence thresholds (0.5).
- Video mode needs strictly increasing timestamps, so it never passes one that is not later than the last.
- MediaPipe is imported only when the adapter is created. Without it, creating the adapter raises `ImportError`.

## Camera

`OpenCVCamera(index)` opens the webcam at that index with OpenCV, asking for 640 x 480 at 30 frames per second to keep detection cheap. On Windows it tries Media Foundation first and then DirectShow, because some webcams open only through one of them; the automatic choice is never used, as it also probes depth-camera drivers. A backend counts as working only when it opens the camera and delivers a frame. `open()` returns whether the camera opened, `read()` returns a BGR frame or `None`, and `close()` releases it.

The size and rate are requests. A webcam delivers what its driver allows: in dim light many lower their frame rate to expose longer, whatever size and format are asked for, so the rate the pipeline sees is the camera's, not the detector's. The capture size does not change the cost of detection, because MediaPipe scales every frame down to its own input size, so 640 x 480 stays.

## `HandTracker`

`HandTracker(camera=0, *, hands_factory=None, camera_factory=None, max_fps=30, max_hands=2, idle_fps=None, idle_after=3.0, clock=time.perf_counter)` runs the pipeline. `camera` is the webcam index; the factories replace the MediaPipe adapter and the OpenCV camera in tests. `max_hands` is passed to the default MediaPipe adapter.

- **`start()`** loads the model and the adapter, then starts two threads and returns `True`. If MediaPipe is not installed, or the model or adapter cannot be loaded, it says why once on the console (`✋ Hand tracking is off: ...`, with a hint where one helps) and returns `False`; nothing is started and the camera is never opened. Calling `start()` while running returns `True` and changes nothing.
- **The capture thread** opens the camera and keeps only the newest frame, replacing any frame detection has not reached yet, so detection always works on the latest image and never falls behind. Frames are read no faster than `max_fps`. If the camera cannot be opened or stops delivering frames, it is closed and retried with back-off (1 second, doubling to at most 5 seconds) until it works or the tracker stops. The first failure prints `✋ Hand tracking cannot use camera N` once, with the hint that another app may be using it; recovery prints once.
- **The detection thread** takes the newest frame, runs detection, drops the frame and delivers a `HandFrame` stamped with the frame's capture time to every subscriber, in subscription order. A subscriber that raises is logged and skipped; the others still receive the frame. A frame that fails detection is skipped and reported once.
- **Idle rate.** With `idle_fps` set, the tracker searches less often while there are no hands. Once no hand has been seen for `idle_after` seconds (counted from the start, then from the last frame with a hand) it runs detection on at most `idle_fps` frames a second and drops the frames in between without searching them; the camera is still read at its own rate. The first frame with a hand ends idling at once: every frame is searched again from then on. Dropped frames are not delivered, which costs a subscriber nothing: it has already been told for seconds that no hand is in view. `idle` is true while the tracker is in this mode. Without `idle_fps` (the default) every frame is searched, as the plain live readout of the diagnostic command does.
- **`subscribe(callback)`** registers a callback taking a `HandFrame` and returns a function that unsubscribes it. Callbacks run on the detection thread and should return quickly.
- **`stop()`** stops both threads, releases the camera and drops any pending frame. It is safe to call when not running and returns once the threads have finished (each joined with a 2 second limit).
- **`running`** is true between a successful `start()` and `stop()`.

Debug logs (category `gestures`) mark start, stop and the camera opening, failing and recovering, never every frame, never what the camera sees (whether hands are in view, which hands, landmarks or coordinates) and never the message of a failure while frames are flowing, only its type.

## Recordings

Recordings capture landmark streams for building and testing gesture logic without a camera. They hold landmarks only, never images, and are written only to a path the user names.

- **Format:** JSON Lines. The first line is the header `{"format": "jarvis-hand-frames", "version": 1}`. Each following line is one `HandFrame` as `{"t", "w", "h", "hands"}`, with `t` in seconds since the first recorded frame and each hand as `{"side", "score", "points", "world"}` (points as `[x, y, z]` lists, rounded to 5 decimal places).
- **`HandRecorder(path)`** is a subscriber: calling it with a `HandFrame` appends a line. `close()` flushes and closes the file. It is also a context manager.
- **`read_recording(path)`** yields the `HandFrame`s back in order, with `time` as written. A file without the header, or with a different format or a newer version, raises `ValueError`.

## Diagnostic command

`python -m jarvis.gestures` shows the pipeline's output so tracking can be checked on the real webcam.

- **Live (default):** `--camera N` (default 0) starts a `HandTracker` and prints a status line about once a second: frames per second, and for each hand its side, confidence and index fingertip position (or that it is waiting while no frame has arrived yet). It also prints when hands appear and disappear. `--seconds S` stops after `S` seconds; otherwise Ctrl+C stops it. `--record PATH` also writes a recording.
- **Gesture snapping:** `--snap` runs gesture snapping on the real desktop (Windows only) with the thresholds from the user's config file, whether or not `gesture_snap_enabled` is on, so it can be tried and tuned without restarting Jarvis. The camera is `--camera N` when given, otherwise `gesture_snap_camera`. It prints the same on and off lines as the tool and, about once a second, the side of the hand in view, each finger's curl (index, middle, ring, little), the gesture it reads as and the state, to help choose the thresholds. `--seconds` or Ctrl+C stop it, and stopping lets go of a held window and releases the camera. `--record PATH` also writes a recording. It runs on the same hand tracker as the mode (see Keeping the cost low), so after a few seconds without a hand its frames-a-second reading drops to the idle rate and a recording holds fewer frames for that stretch.
- **Preview:** `--preview` (with the live readout or `--snap`) opens a window showing the camera's mirrored image as the detector sees it, with each hand's 21 landmarks and bones drawn on it, the palm centre (what the pointer follows) marked, and a caption with the hand's side, each finger's curl, the gesture it reads as and, with `--snap`, the gesture snapping state. It uses PyQt6 and stays above other windows; the console readout continues once a second. On Windows the window is excluded from screen capture each time it is shown (`SetWindowDisplayAffinity` with `WDA_EXCLUDEFROMCAPTURE`, or `WDA_MONITOR`, which captures it as black, where Windows is too old); screenshots, screen sharing and the `screenshot` tool see what is behind it. If neither works, the console says once that the preview could not be hidden from screenshots and screen sharing. The frame is taken from the camera before detection and only the newest one is kept for drawing; closing the window stops the command like `--seconds` does.
- **Photo:** `--image PATH` runs detection once on a photo, treated as a camera frame (so it is mirrored), prints what was found and exits.
- Failures to start print the same one-time reason as `start()` and exit with status 1.

## Gesture snapping

Gesture snapping moves and scrolls windows with the hand, recognised by how many fingers are raised: one finger aims the pointer, a fist grabs the window under it and the window follows the hand, raising a finger or opening the hand snaps it into the zone under the pointer, and two fingers scroll. It never clicks.

### Input from the user's own hand: an exception to "Jarvis never moves the pointer"

Jarvis's automation never moves the pointer or sends mouse input on its own (`platform/windows/ui_automation.spec.md`): it acts through UI Automation patterns, never through coordinates. Gesture snapping is a deliberate, narrow exception. It is not Jarvis acting: it relays the user's own hand in real time, like a pointing device. Every pointer move, window move and scroll comes from a hand movement the camera saw a fraction of a second earlier; no model, tool result, routine or other code chooses a position, a window or an amount. It sends mouse-wheel scrolling, but never presses a mouse button, never clicks and never types. Nothing outside `WindowSnap` sends this input, and `WindowSnap` sends it only from `HandFrame`s while the user has the mode on. The Windows code that moves the pointer and windows and scrolls for it (`platform/windows/window_snap.py`) is used by gesture snapping only and offers no tool action that moves or scrolls anything.

### Turning it on and off

- **Off by default.** `gesture_snap_enabled` (default `false`, Settings > Extras > Gesture control > Allow Gesture Control) offers the `gestureControl` tool and its fast-path phrases. With it off the tool is not registered, nothing can start the camera for gesture snapping, and the phrases fall through to the model, which has no tool for them.
- **Started only when asked.** The setting does not start the camera. The user starts the mode by voice or text ("start gesture control") and stops it the same way ("stop gesture control"). Both go through `gestureControl`, the normal tool path and central safety. Both actions are routine (`SAFE`), so the phrases are fast-routed. The camera runs only while the mode is on.
- **Windows only.** The tool is registered only on Windows, where the output exists.
- **Not offered to cloud models.** `gestureControl` is never in a Codex or Claude tool snapshot (`bridge/bridge.spec.md`): a cloud model cannot turn the camera on, a call it makes for the tool by name is refused, and a routine it runs that has a `gestureControl` step fails validation before any step runs (`routines/routines.spec.md`). The fast-path phrases work in every reply mode because the fast path runs before any bridge.
- **Started only at the PC.** A request from a paired phone (`remote/remote.spec.md`) may stop gesture snapping or ask whether it is on, but `start` refuses it ("Gesture snapping turns the webcam on, so it can only be started at the PC."), whether it comes as a phrase, through the local model or as a routine step, and also when the step runs after the user approves its routine in the desktop dialog. The tool reads the request's origin from the request scope (`tools/request_scope.py`); an approved action keeps the origin of the request that raised it.
- **Stopping lets go.** `stop()` leaves a held window where it is (no snap) and ends a scroll, then stops the hand tracker, which releases the camera. It is safe to call at any time and returns once both are done. The daemon calls it on shutdown, and the service also registers it to run when the process exits normally.

### Gestures

The palm faces the camera. Only the index, middle, ring and little fingers are counted; the thumb is ignored.

| Gesture | Fingers raised | Effect |
|---|---|---|
| One finger | Index only | The pointer follows the hand (the centre of the palm) as an aim. Nothing is clicked |
| Fist | None | Grabs the window under the pointer and brings it to the front. While the fist is held, the window follows the hand with the pointer |
| Raise a finger or open the hand while holding | Index (with any others) | Snaps the held window into the zone under the pointer |
| Two fingers | Index and middle | Scrolls the window under the pointer, which stays still: hold the hand above or below where it was when the scroll started, further for faster |
| Open hand | All four | Pause: the pointer stays where it is until one finger is raised again |
| Hand out of view | | Pause, as for an open hand |

Any other combination (for example index and little finger) changes nothing and holds the pointer still. Only one hand drives. `gesture_snap_hand` chooses which hand may start driving: `right`, `left` or `any` (default). Once a hand drives, the hand nearest the driving palm's last position is followed whatever side it is labelled, because the tracker's left and right labels flicker for a single hand (measured on a real webcam: one hand read as the other side for several frames at a time). A palm more than a quarter of the camera's width from the last position counts as no hand, so another hand cannot take over until the driving hand is lost.

### Recognising fingers

- **Finger curl** is the distance from the wrist to a fingertip divided by the distance from the wrist to that finger's middle joint (PIP), on the `world` landmarks (metres), so it does not change with the hand's distance from the camera. A straight finger reads well above 1 and a folded one below 1.
- **Up and down, with hysteresis.** A finger is up when its curl is above `gesture_snap_finger_up` (default 1.1) and down when it is below `gesture_snap_finger_down` (default 0.95). Between the two it keeps its last state, so a finger hovering near one threshold cannot flicker. `gesture_snap_finger_down` must be below `gesture_snap_finger_up`; a configuration where it is not is corrected on load to `gesture_snap_finger_up - 0.1`. A finger first seen between the thresholds counts as down.
- Measured on a real webcam with the palm towards the camera: raised fingers read 1.34 to 1.39 and folded fingers 0.64 to 0.84, in every gesture. The gap between thumb and index tips is not used: on the same camera it overlapped between a pinch, a relaxed hand and a fist.
- **Debounce.** A new gesture counts only once it has been seen without a break for `gesture_snap_debounce_ms` (default 100 ms). Until then the previous gesture stays in force.
- **The aim is frozen while the hand changes.** As soon as the hand shows a different gesture from the one in force, the pointer stops at its current position, the anchor. A fist then grabs the window under the anchor and two fingers scroll the window under it, so the fingers' own movement while changing never moves the aim. If the change does not last the debounce time, aiming resumes from where it was without a jump. While a held window is being released the window and pointer also stay still, so letting go does not shift the drop point.

### Grabbing, moving and snapping

- **Grab.** A fist grabs when it follows one finger, or straight after a pause; a fist that follows two fingers grabs nothing (see Scrolling). The window grabbed is the top-level window under the anchor (where the pointer was before the hand closed). A fist raised straight after a pause grabs the window under the palm's place. The desktop, the taskbar, a point over no window and a window still settling from the previous snap grab nothing; the pointer then stays at the anchor until the fist opens. A maximised window is restored first, keeping the pointer at the same share of its width. The grabbed window is brought to the front without taking the keyboard focus. Nothing in a grab waits for the application.
- **Move.** While the fist is held the pointer follows the hand as when aiming, and the window moves with it, keeping the pointer at the same place on the window. Moves are posted to the window without waiting for it, so a slow application never holds up tracking. A hand shape without the index finger (a fist with the little finger loosening, say) keeps holding.
- **Snap.** Raising the index finger (one finger, two fingers or an open hand) places the window into the zone under the pointer. The zones are the FancyZones layouts the user set up in Microsoft PowerToys, read the same way voice placement reads them (only while `windows_fancyzones_enabled` is on, `platform/windows/apps_paths.spec.md`), for the monitor under the pointer on the current virtual desktop. A pointer in the spacing between zones snaps to the nearest zone on that monitor. A monitor without zones, PowerToys not being installed or FancyZones zones switched off leave the window where it was dropped. Placement uses the shared `windows_mgmt.place_window` (visible frame, DPI-aware, verified) on a worker thread, so tracking continues while the window settles.
- **Lost hand.** If the hand stays out of view for 0.25 s while a window is held, the window is left where it is, not snapped. This is also the way to let go of a window without snapping it.
- **Failures.** A window that refuses to move (a higher-privilege application, or one that closed) is let go and reported once on the console; tracking continues.

### Scrolling

Scrolling works like a joystick (or a browser's middle-click autoscroll), because a moving hand is the hardest thing to read: the scroll speed comes from where the hand is, not from each movement.

- **Neutral.** When two fingers start a scroll, the pointer stays where it is (so the wheel events reach the window under it) and the palm's height at that moment becomes the neutral point.
- **Speed from distance.** Holding the hand above neutral scrolls up (towards the top of the page) and below neutral scrolls down. Within 3% of the camera's height of neutral nothing scrolls, so a steady hand never creeps. Beyond that the speed grows with the distance: `gesture_snap_scroll_speed` (default 15) wheel notches a second for every further tenth of the camera's height. Wheel units (120 a notch) are sent in steps of at least 30, so slow scrolling is smooth without a flood of events. Sideways movement is ignored.
- **Misreads do not stop it.** While the hand is briefly misread as another gesture the scroll carries on at the speed its position gives.
- Leaving a scroll needs the new gesture to last at least 0.5 s (or the debounce time, if longer), because a moving hand is misread: measured on a real webcam at about 15 frames a second, two fingers moving up and down read as one finger or a fist for 1 or 2 frames at a time and as a fist for up to 6 frames (0.4 s), and the raised fingers dipped to 0.92 to 1.0. In the same measurement the MediaPipe Gesture Recognizer classified the moving two-finger pose correctly in only 26% of frames against 47% for finger counting, and was not better on any held pose, so it is not used. Then one finger aims again and an open hand pauses. A fist ends the scroll but grabs nothing: a fist grabs only after one finger or straight after a pause, so closing the hand while scrolling never picks up a window. The pointer then stays still until a finger is raised.
- A scroll the window refuses (a higher-privilege application) is reported once; tracking continues.

### Moving the pointer

- **Palm centre.** The pointer follows the centre of the palm: the average image position of the wrist (0) and the four finger bases (`INDEX_MCP`, `MIDDLE_MCP`, `RING_MCP`, `PINKY_MCP`). It does not move when fingers fold or rise, so changing gesture never shifts the pointer.
- **Comfort box, absolute.** A box in the camera's view maps onto the whole virtual desktop: the centre of the box is the centre of the desktop's bounding rectangle (all monitors), and its edges are the desktop's edges. The box is centred on the palm's place when the hand starts driving: when the mode starts, and whenever the hand comes back after being out of view. Raising the hand wherever it is comfortable therefore puts the pointer in the middle of the desktop, and taking the hand out of view and raising it again re-centres the box. An open-hand pause keeps the box where it is. Its width is `gesture_snap_comfort_box` of the camera's width (default 0.5, half of it); its height follows the desktop's shape, so up and down use the same scale as left and right. The pointer is wherever the palm is in the box, so every part of every monitor is always within reach, and a palm outside the box puts the pointer at the nearest desktop edge.
- **No relative movement.** When aiming resumes after an open hand or another pause, the pointer goes straight to the palm's place in the box. An open hand therefore pauses the pointer but cannot reposition the hand against it.
- **No hop on a gesture change.** The pointer stands still while the hand changes gesture. When the new gesture counts, the pointer continues from where it stood and the small difference from the palm's place (a closing hand shifts the palm a little) fades out over about half a second, so grabbing, dropping or scrolling never makes the pointer jump.
- **Smoothing.** The palm position (scaled to desktop pixels) is smoothed with a One Euro filter: strong smoothing when the hand is still, little lag when it moves fast. `gesture_snap_smoothing` (default 1.0) is the filter's minimum cutoff in hertz (lower is steadier but slower) and `gesture_snap_speed_response` (default 0.005) its speed coefficient (higher reacts faster during quick movements). The filter restarts when the hand is lost. Moving a held window and scrolling use the same smoothed palm position.
- **Bounds.** The pointer is kept inside the desktop: a position outside every monitor is moved to the nearest point of the nearest monitor, so on desktops whose monitors do not form a rectangle the pointer never lands in a gap. The monitor layout is read when the mode starts.

### Losing the hand

- A frame without the driving hand stops the pointer. If the hand stays out of view for 0.25 s (timed by when frames arrive, not when they were captured, so slow detection never drops a held window) the mode pauses: a gesture in progress is cancelled, a scroll ends, a held window is left where it is and the finger states are forgotten.
- A watchdog thread checks every 50 ms, so this also happens when frames stop arriving altogether (a camera that stops, unplugs or freezes).

### Keeping the cost low

The mode keeps the camera and the detector busy for as long as it is on, so it spends effort only where gestures need it. `snap_tracker(cfg, camera, **kwargs)` (in `window_snap_service.py`) builds the hand tracker the mode and `--snap` use:

- **One hand.** With `gesture_snap_hand` at `any` (the default) the tracker looks for one hand (`max_hands=1`), because only one hand ever drives. `left` and `right` look for two, since the side named may be the second hand found.
- **Idle rate.** The tracker idles at 6 frames a second at most (`IDLE_FPS`; every third frame of a 15 frames a second webcam) once no hand has been in view for 3 seconds (`IDLE_AFTER_SEC`) and wakes on the first frame that shows one (see `HandTracker`). A hand raised after a long absence is therefore noticed up to about 0.2 s later than at the full rate, once; by then the mode has been paused for seconds, so nothing is held, scrolling or aimed.
- **Nothing runs while off.** `stop()` ends the capture, detection and watchdog threads and releases the camera; the service holds no timer. The watchdog wakes every 50 ms, which costs too little to measure.

### `WindowSnap` and `WindowSnapService`

- **`WindowSnap(output, settings, clock)`** is the gesture model. `output` is the platform output: `desktop()` returning monitor rectangles `(left, top, right, bottom)` with exclusive right and bottom edges, `move_to(x, y)` for the pointer, `grab(x, y)` returning a grabbed window (an object with the window's outer frame as `rect`) or `None`, `move_window(window, left, top)`, `drop(window, x, y)` to snap it, and `scroll(delta)` for wheel units. Tests use a fake. `on_frame(frame)` handles one `HandFrame`, `tick()` runs the watchdog check, and `close()` lets go of a held window. It is thread safe: frames arrive on the detection thread, ticks on the watchdog thread and `close` on whichever thread stops the mode.
- **`WindowSnap.state`** names the current state for display: `paused`, `aiming`, `hand changing`, `holding`, `holding nothing` (a fist over the desktop) or `scrolling`. `WindowSnapService.state` is that, or `off`.
- **`WindowSnapService(cfg, output_factory, tracker_factory=None)`** owns one tracker (made by `tracker_factory(camera)`, by default `snap_tracker`), one `WindowSnap` and the watchdog thread. `start()` reads the monitors, starts the tracker on `gesture_snap_camera` (default 0) and subscribes the model; it returns `"started"`, or `"already"` when it was on, and raises `WindowSnapUnavailable` with a plain reason (the displays could not be read, or hand tracking could not load) when it cannot start, in which case nothing is left running. A camera that is busy or unplugged does not stop it from starting: the tracker retries it in the background and says so on the console. `stop()` is described above. `running` says whether the mode is on. A failure from the output is reported once on the console and logged once until that kind of call works again.
- One service exists per daemon (`window_snap_service.get_service`).

### Shared on flag for the desktop app

`gestures/snap_state.py` tells other processes whether gesture snapping is running, so the desktop app can show its pointer ring (`desktop_app/pointer_ring.spec.md`) even when the daemon runs as a separate process. It follows the voice levels channel (`jarvis/voice_levels.py`): a small memory-mapped file in the temp folder (`jarvis_gesture_snap`), guarded by a sequence number so a reader never sees half a write. It holds one flag and the wall-clock time it was written, nothing else.

- `publish(on, now=None)` writes the flag. The service publishes on when it starts, again from the watchdog every 50 ms while it runs (the heartbeat), and off when it stops.
- `is_on(now=None)` is true only when the flag is on and was written within `MAX_AGE_S` (1 second), so a daemon that crashes or is killed without stopping reads as off within a second.
- Neither call raises: a file that cannot be opened is reported once in the debug log and reads as off.

### The `gestureControl` tool

| Action | Behaviour |
|---|---|
| `start` | Starts gesture snapping. Reports success with a one-line reminder of the gestures, or that it was already on, or why it could not start (hand tracking unavailable, displays unreadable, a request from a phone) |
| `stop` | Stops it, letting go of a held window and releasing the camera. Reports that it is off (also when it was already off) |
| `status` | Whether gesture snapping is on |

Console output: `🖐️ Gesture snapping on` with the gestures indented below, and `🖐️ Gesture snapping off`.

### Fast path

The `gesture_control` family in `fastpath/phrases/<language>.json` maps whole utterances such as "start gesture control", "start gesture controls", "turn on gestures", "stop gesture control" and "turn off the gesture controls" to `gestureControl` `start` and `stop` (`fastpath/fastpath.spec.md`). Every phrase pairs a start or stop verb phrase with a name for the feature (in English: gesture control or controls, gestures, hand gestures, gesture snapping or gesture mode); a bare name or a question about the feature falls through to the model. It is available only while the tool is registered.

### Privacy and logs

Nothing the camera sees leaves the gesture pipeline: frames, landmarks, finger readings, gestures, pointer positions, window positions, scroll amounts and which window was grabbed reach no cloud model, MCP server, phone, web chat, dialogue memory, diary, routine, desktop referent, log, issue report or file.

- **Tool results are fixed sentences** (The `gestureControl` tool) that say only whether gesture snapping is on, so they are the same whatever the hand did. They are the only gesture snapping text that reaches the conversation, and so the phone and web chat mirrors, the diary and, when recent dialogue is shared, Codex or Claude.
- **Routines.** `gestureControl` calls are journalled like other tool calls (`start` or `stop`, in memory), so a routine run locally can start gesture snapping. A cloud model cannot run such a routine (Turning it on and off); listing routines or recent actions tells it only that gesture snapping was started or stopped, as the conversation already does.
- **Desktop referents** record nothing from gesture snapping: grabbing, moving and snapping are not tool calls.
- **Debug logs** (category `gestures`) mark starting and stopping, pausing, a grab (or a fist that grabbed nothing), a snap (whether a zone was found), the start and end of a scroll, and a window left where it is because the hand was lost or the mode stopped. They never contain coordinates, measures, scroll amounts, window titles, process names, whether or which hands are in view, or the message of a failure while tracking (only its type). Console lines from gesture snapping follow the same rule; only the diagnostic command, which the user runs to see what the camera sees, prints hands and gestures. This matters because the desktop app's issue report can include the log.
- **Disk.** Gesture snapping writes only the shared on flag (Shared on flag for the desktop app). The hand model is downloaded once; recordings are written only by the diagnostic command, to a path the user names.
- **Network.** The only request is the one-time model download. MediaPipe 0.10.31 makes no network requests and sends no usage data: its Python package has no networking code, its native library imports no networking functions, and detection runs with no connections (checked with Python's sockets blocked and by watching the process's connections).
- **Screen capture.** The diagnostic preview is kept out of screen captures (Diagnostic command); the pointer ring shows only where the pointer is.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `gesture_snap_enabled` | `false` | Offer gesture snapping (the camera still starts only when asked) |
| `gesture_snap_camera` | `0` | Webcam index |
| `gesture_snap_hand` | `any` | `any`, `right` or `left` (`any` looks for one hand, a side for both: see Keeping the cost low) |
| `gesture_snap_comfort_box` | `0.5` | Fraction of the camera's width that spans the whole desktop (0.15 to 1.0) |
| `gesture_snap_finger_up` | `1.1` | Finger curl above which a finger counts as raised (0.8 to 2.0) |
| `gesture_snap_finger_down` | `0.95` | Finger curl below which a finger counts as folded (0.5 to 1.5, below the raised threshold) |
| `gesture_snap_debounce_ms` | `100` | How long a gesture must last to count (0 to 500 ms) |
| `gesture_snap_scroll_speed` | `15` | Wheel notches a second for each tenth of the camera's height the hand is held beyond neutral (0.5 to 60) |
| `gesture_snap_smoothing` | `1.0` | One Euro minimum cutoff in hertz (0.05 to 10) |
| `gesture_snap_speed_response` | `0.005` | One Euro speed coefficient (0 to 0.1) |
| `gesture_snap_pointer_ring` | `true` | Show the desktop app's glowing ring around the pointer while gesture snapping runs (`desktop_app/pointer_ring.spec.md`) |

Values outside the range are clamped on load; values of the wrong type use the default. All are shown in Settings > Extras > Gesture control. Changes take effect after Jarvis restarts, except `gesture_snap_pointer_ring`, which applies as soon as Settings are saved.

## Dependencies

`mediapipe==0.10.31` provides the Hand Landmarker; it needs no OpenCV build of its own (Jarvis's `opencv-python-headless` is used for capture) and requires `sounddevice` 0.5. `0.10.30`'s Windows build cannot load its native library, and later releases import matplotlib on start and require a second OpenCV package.

## Tests

- **Unit** (`tests/test_hand_tracking.py`, `tests/test_hand_recording.py`, `tests/test_model_files.py`): the pipeline against a fake camera and a fake detector (delivery order, newest-frame-only, failing subscribers, camera failure and recovery, start-up failures, stop releasing the camera) and `tests/test_hand_tracking_idle.py` (the idle gate and how late a hand is noticed from idle, the tracker thinning its searches and waking, the number of hands the detector is built for, and `snap_tracker`), recordings round-tripping, and the model file helper.
- **Gesture snapping** (`tests/test_window_snap.py`, `tests/test_window_snap_service.py`, `tests/test_gesture_control_tool.py`, `tests/test_windows_window_snap.py`, `tests/test_gesture_preview.py`): the gesture model on synthetic `HandFrame`s against a fake output (finger up and down thresholds with hysteresis, debounce, the frozen aim, grab, move, snap, two-finger scrolling, pausing, a held window let go on hand loss and by the watchdog, the absolute comfort-box mapping and bounds across several monitors), zone choice, the service and tool against a fake tracker, and the preview drawing. The finger thresholds come from a calibration on a real webcam (see Recognising fingers); the synthetic hands set each finger's curl exactly.
- **Privacy** (`tests/test_gesture_privacy.py`): neither cloud mode is offered `gestureControl` in any sharing setting; a call for it by name and a routine with a `gestureControl` step are refused for a bridge request; a phone request cannot start it, even through a routine approved in the desktop dialog, but can stop it; a whole simulated session on the real tracker and service (fake camera and detector) logs and prints no coordinates, measures, window titles or which hands are in view, writes no file but the on flag and opens no network connection; tool results are the same whatever the hand did; failures carrying camera data log only their type; the flag file holds only the flag and the time; the preview window is not in a screen capture (Windows).
- **Integration** (`tests/test_hand_tracking_mediapipe.py`): the real model on photos of hands from MediaPipe's test assets in `tests/fixtures/hands/`. Skipped when MediaPipe or the cached model is missing; tests never download.

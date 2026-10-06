# Gestures: hand tracking

Webcam hand tracking that turns camera frames into hand landmarks for gesture control. This spec covers the hand-tracking pipeline, landmark recordings and the diagnostic command. Gestures are built on the `HandFrame` stream it produces.

## Principles

- **Local only.** Detection runs on this machine with Google's MediaPipe Hand Landmarker on the CPU. No frame, landmark or result leaves the machine. The only network request is the one-time model download described below.
- **Frames stay in memory.** Camera frames are flipped, searched for hands and dropped. They are never written to disk, logged, shown, sent anywhere or kept after detection. Subscribers receive landmarks only, never pixels.
- **The camera is on only while the pipeline runs.** `stop()` releases the camera, so the webcam light goes off. Nothing in Jarvis starts the pipeline on its own: the diagnostic command and the gesture feature start it explicitly.
- **Honest about failure.** A missing MediaPipe install or model keeps the pipeline off and says why once on the console. A busy or unplugged camera is retried in the background and reported once, with recovery reported once too.
- **Platform-neutral.** The pipeline uses OpenCV for capture and knows nothing of windows, tools, the reply engine or the LLM.

## Modules

| Module | Role |
|---|---|
| `gestures/hand_tracking.py` | Data model (`Landmark`, `Point`, `Hand`, `HandFrame`), the MediaPipe adapter, the OpenCV camera and `HandTracker` |
| `gestures/recording.py` | Writing a `HandFrame` stream to a JSON Lines file and reading it back |
| `gestures/__main__.py` | `python -m jarvis.gestures`: the diagnostic command |
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

`OpenCVCamera(index)` opens the webcam at that index with OpenCV (through Media Foundation on Windows, so no other capture driver is probed), asking for 640 x 480 at 30 frames per second to keep detection cheap. `open()` returns whether the camera opened, `read()` returns a BGR frame or `None`, and `close()` releases it.

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
- **Photo:** `--image PATH` runs detection once on a photo, treated as a camera frame (so it is mirrored), prints what was found and exits.
- Failures to start print the same one-time reason as `start()` and exit with status 1.

## Dependencies

`mediapipe==0.10.31` provides the Hand Landmarker; it needs no OpenCV build of its own (Jarvis's `opencv-python-headless` is used for capture) and requires `sounddevice` 0.5. `0.10.30`'s Windows build cannot load its native library, and later releases import matplotlib on start and require a second OpenCV package.

## Tests

- **Unit** (`tests/test_hand_tracking.py`, `tests/test_hand_recording.py`, `tests/test_model_files.py`): the pipeline against a fake camera and a fake detector (delivery order, newest-frame-only, failing subscribers, camera failure and recovery, start-up failures, stop releasing the camera), recordings round-tripping, and the model file helper.
- **Integration** (`tests/test_hand_tracking_mediapipe.py`): the real model on photos of hands from MediaPipe's test assets in `tests/fixtures/hands/`. Skipped when MediaPipe or the cached model is missing; tests never download.

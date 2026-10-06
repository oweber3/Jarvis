"""Webcam hand tracking: camera frames in, hand landmarks out.

Two threads. The capture thread reads the webcam and keeps only the newest frame. The detection thread
mirrors it, finds hands with MediaPipe's Hand Landmarker and hands a ``HandFrame`` of landmarks to every
subscriber. Frames live in memory only: they are searched and dropped, never written, logged, shown or kept,
and subscribers never see pixels. See ``gestures.spec.md``.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, List, NamedTuple, Optional, Set, Tuple

from ..debug import debug_log
from ..utils.model_files import ModelUnavailable, ensure_verified_file

HAND_MODEL_FILENAME = "hand_landmarker.task"
HAND_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/"
                  "float16/1/hand_landmarker.task")
HAND_MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"

CAPTURE_WIDTH = 640
CAPTURE_HEIGHT = 480
DEFAULT_MAX_FPS = 30.0

_BACKOFF_FIRST_SEC = 1.0
_BACKOFF_MAX_SEC = 5.0
_JOIN_SEC = 2.0


class Landmark(IntEnum):
    """MediaPipe's 21 hand points."""

    WRIST = 0
    THUMB_CMC = 1
    THUMB_MCP = 2
    THUMB_IP = 3
    THUMB_TIP = 4
    INDEX_MCP = 5
    INDEX_PIP = 6
    INDEX_DIP = 7
    INDEX_TIP = 8
    MIDDLE_MCP = 9
    MIDDLE_PIP = 10
    MIDDLE_DIP = 11
    MIDDLE_TIP = 12
    RING_MCP = 13
    RING_PIP = 14
    RING_DIP = 15
    RING_TIP = 16
    PINKY_MCP = 17
    PINKY_PIP = 18
    PINKY_DIP = 19
    PINKY_TIP = 20


class Point(NamedTuple):
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Hand:
    """One hand as seen in the user's mirrored view.

    ``points`` are image coordinates: ``x`` 0 to 1 from the user's left to their right, ``y`` 0 to 1 from top
    to bottom, ``z`` depth relative to the wrist (negative is nearer the camera). ``world`` are metres around
    the middle of the hand, independent of its distance from the camera."""

    side: str
    score: float
    points: Tuple[Point, ...]
    world: Tuple[Point, ...]

    def point(self, landmark: int) -> Point:
        return self.points[landmark]

    def world_point(self, landmark: int) -> Point:
        return self.world[landmark]


@dataclass(frozen=True)
class HandFrame:
    """The hands found in one camera frame captured at ``time`` (seconds, ``time.perf_counter``)."""

    time: float
    width: int
    height: int
    hands: Tuple[Hand, ...]


def ensure_hand_model(fetch: Optional[Callable[[str], bytes]] = None) -> Path:
    """The path of the Hand Landmarker model, fetched and verified against the pinned SHA-256 when needed."""
    return ensure_verified_file(HAND_MODEL_FILENAME, HAND_MODEL_URL, HAND_MODEL_SHA256,
                                "the hand tracking model", fetch)


# --------------------------------------------------------------------------------------------------
# Detection with MediaPipe
# --------------------------------------------------------------------------------------------------

class MediaPipeHands:
    """Finds hands in a BGR camera frame, mirrored first so ``side`` names the user's own hand."""

    def __init__(self, model_path: str, max_hands: int = 2):
        import cv2
        import mediapipe
        import numpy
        from mediapipe.tasks.python import BaseOptions, vision

        self._cv2, self._mp, self._np = cv2, mediapipe, numpy
        options = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_path=model_path),
                                               running_mode=vision.RunningMode.VIDEO, num_hands=max_hands)
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._last_ms = -1

    def __call__(self, frame: Any, timestamp: float) -> Tuple[Hand, ...]:
        rgb = self._cv2.cvtColor(self._cv2.flip(frame, 1), self._cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=self._np.ascontiguousarray(rgb))
        # Video mode refuses a timestamp that is not later than the last one.
        ms = max(self._last_ms + 1, int(timestamp * 1000))
        self._last_ms = ms
        result = self._landmarker.detect_for_video(image, ms)
        hands: List[Hand] = []
        for index, points in enumerate(result.hand_landmarks):
            categories = result.handedness[index] if index < len(result.handedness) else []
            if not categories or index >= len(result.hand_world_landmarks):
                continue
            hands.append(Hand(
                side=categories[0].category_name.lower(),
                score=float(categories[0].score),
                points=tuple(Point(float(p.x), float(p.y), float(p.z)) for p in points),
                world=tuple(Point(float(p.x), float(p.y), float(p.z))
                            for p in result.hand_world_landmarks[index])))
        return tuple(hands)

    def close(self) -> None:
        self._landmarker.close()


def _load_mediapipe_hands() -> MediaPipeHands:
    import mediapipe  # noqa: F401  (fail on a missing install before fetching the model)

    return MediaPipeHands(str(ensure_hand_model()))


# --------------------------------------------------------------------------------------------------
# The webcam
# --------------------------------------------------------------------------------------------------

class OpenCVCamera:
    """The webcam at ``index``, asked for 640 x 480 at 30 frames per second."""

    def __init__(self, index: int):
        self._index = index
        self._capture = None

    def open(self) -> bool:
        import cv2

        # On Windows, Media Foundation directly: the automatic choice also probes depth-camera drivers.
        backend = cv2.CAP_MSMF if sys.platform == "win32" else cv2.CAP_ANY
        capture = cv2.VideoCapture(self._index, backend)
        if not capture.isOpened():
            capture.release()
            return False
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, DEFAULT_MAX_FPS)
        self._capture = capture
        return True

    def read(self) -> Optional[Any]:
        if self._capture is None:
            return None
        ok, frame = self._capture.read()
        return frame if ok else None

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()


# --------------------------------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------------------------------

class HandTracker:
    """Reads the webcam and delivers a ``HandFrame`` per processed frame to every subscriber.

    ``start`` returns whether tracking is running. Without MediaPipe or the model it says why once on the
    console and returns ``False`` without opening the camera. ``hands_factory`` (no arguments, returns a
    callable taking a BGR frame and its capture time) and ``camera_factory`` (takes the index, returns an
    object with ``open``, ``read`` and ``close``) replace MediaPipe and OpenCV."""

    def __init__(self, camera: int = 0, *,
                 hands_factory: Optional[Callable[[], Callable[[Any, float], Tuple[Hand, ...]]]] = None,
                 camera_factory: Optional[Callable[[int], Any]] = None,
                 max_fps: float = DEFAULT_MAX_FPS,
                 clock: Callable[[], float] = time.perf_counter):
        self._index = camera
        self._hands_factory = hands_factory or _load_mediapipe_hands
        self._camera_factory = camera_factory or OpenCVCamera
        self._period = 1.0 / max_fps
        self._clock = clock
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._pending: Optional[Tuple[float, Any]] = None
        self._subscribers: List[Callable[[HandFrame], None]] = []
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._hands: Optional[Callable[[Any, float], Tuple[Hand, ...]]] = None
        self._said: Set[str] = set()
        self._sides: Tuple[str, ...] = ()

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._threads)

    def subscribe(self, callback: Callable[[HandFrame], None]) -> Callable[[], None]:
        """Deliver every ``HandFrame`` to ``callback`` (on the detection thread); returns the unsubscribe."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self._threads:
                return True
        if self._hands is None and not self._load_hands():
            return False
        self._stop.clear()
        camera = self._camera_factory(self._index)
        threads = [threading.Thread(target=self._capture_loop, args=(camera,), name="hand-capture", daemon=True),
                   threading.Thread(target=self._detect_loop, name="hand-detect", daemon=True)]
        with self._lock:
            self._threads = threads
        for thread in threads:
            thread.start()
        debug_log(f"hand tracking started on camera {self._index}", "gestures")
        return True

    def stop(self) -> None:
        with self._lock:
            threads, self._threads = self._threads, []
        if not threads:
            return
        self._stop.set()
        with self._frame_ready:
            self._pending = None
            self._frame_ready.notify_all()
        for thread in threads:
            thread.join(timeout=_JOIN_SEC)
        with self._lock:
            self._pending = None
        self._sides = ()
        debug_log("hand tracking stopped", "gestures")

    def _say(self, key: str, text: str) -> None:
        if key in self._said:
            return
        self._said.add(key)
        print(text, flush=True)

    def _load_hands(self) -> bool:
        try:
            self._hands = self._hands_factory()
        except ImportError as exc:
            debug_log(f"hand tracking is off: MediaPipe did not import ({exc})", "gestures")
            self._say("mediapipe_missing",
                      "✋ Hand tracking is off: MediaPipe is not installed\n"
                      "   💡 Run pip install -r requirements.txt with the Jarvis environment's Python")
            return False
        except ModelUnavailable as exc:
            debug_log(f"hand tracking is off: {exc}", "gestures")
            self._say("model_missing", f"✋ Hand tracking is off: {exc}")
            return False
        except Exception as exc:
            debug_log(f"hand tracking is off: the detector did not load ({type(exc).__name__}: {exc})", "gestures")
            self._say("detector_failed",
                      f"✋ Hand tracking is off: the hand detector could not be loaded ({type(exc).__name__})")
            return False
        return True

    # -- capture ---------------------------------------------------------------------------------

    def _capture_loop(self, camera: Any) -> None:
        is_open = False
        failures = 0
        next_read = 0.0
        try:
            while not self._stop.is_set():
                if not is_open:
                    is_open = self._open(camera)
                    if not is_open:
                        failures += 1
                        self._camera_failed("it did not open", failures)
                        continue
                    debug_log(f"camera {self._index} opened", "gestures")
                wait = next_read - time.perf_counter()
                if wait > 0:
                    self._stop.wait(wait)
                    continue
                next_read = time.perf_counter() + self._period
                try:
                    frame = camera.read()
                except Exception:
                    frame = None
                if self._stop.is_set():
                    return
                if frame is None:
                    self._close(camera)
                    is_open = False
                    failures += 1
                    self._camera_failed("it stopped delivering frames", failures)
                    continue
                captured = self._clock()
                if failures:
                    failures = 0
                    debug_log(f"camera {self._index} is working again", "gestures")
                    self._say("camera_back", f"✋ Camera {self._index} is working again")
                with self._frame_ready:
                    self._pending = (captured, frame)
                    self._frame_ready.notify()
                del frame
        finally:
            self._close(camera)

    @staticmethod
    def _open(camera: Any) -> bool:
        try:
            return bool(camera.open())
        except Exception:
            return False

    @staticmethod
    def _close(camera: Any) -> None:
        try:
            camera.close()
        except Exception:
            pass

    def _camera_failed(self, reason: str, failures: int) -> None:
        debug_log(f"camera {self._index} failed ({reason}), attempt {failures}", "gestures")
        self._say("camera_down",
                  f"✋ Hand tracking cannot use camera {self._index} ({reason})\n"
                  "   💡 Another app may be using it, or it is unplugged. Retrying in the background")
        self._stop.wait(min(_BACKOFF_FIRST_SEC * 2 ** (failures - 1), _BACKOFF_MAX_SEC))

    # -- detection -------------------------------------------------------------------------------

    def _next_frame(self) -> Optional[Tuple[float, Any]]:
        with self._frame_ready:
            if self._pending is None and not self._stop.is_set():
                self._frame_ready.wait(0.25)
            item, self._pending = self._pending, None
            return item

    def _detect_loop(self) -> None:
        hands_of = self._hands
        while not self._stop.is_set():
            item = self._next_frame()
            if item is None or self._stop.is_set():
                continue
            captured, frame = item
            del item
            height, width = frame.shape[:2]
            try:
                hands = tuple(hands_of(frame, captured))
            except Exception as exc:
                debug_log(f"hand detection failed on a frame ({type(exc).__name__})", "gestures")
                self._say("frame_unreadable", "✋ Hand tracking could not read a camera frame")
                continue
            finally:
                del frame
            self._note_sides(hands)
            self._deliver(HandFrame(captured, int(width), int(height), hands))

    def _note_sides(self, hands: Tuple[Hand, ...]) -> None:
        sides = tuple(sorted(hand.side for hand in hands))
        if sides != self._sides:
            self._sides = sides
            debug_log(f"hands in view: {', '.join(sides) or 'none'}", "gestures")

    def _deliver(self, hand_frame: HandFrame) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(hand_frame)
            except Exception as exc:
                debug_log(f"a hand frame subscriber failed ({type(exc).__name__})", "gestures")

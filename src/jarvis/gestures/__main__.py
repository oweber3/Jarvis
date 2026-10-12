"""Watch hand tracking: ``python -m jarvis.gestures [--camera N] [--seconds S] [--record PATH] [--image PATH]``,
or try gesture snapping with ``--snap``.

See ``gestures.spec.md``.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from .hand_tracking import Hand, HandFrame, HandTracker, Landmark
from .recording import HandRecorder


def _describe(hand: Hand) -> str:
    tip = hand.point(Landmark.INDEX_TIP)
    return f"{hand.side} {hand.score:.2f}, index tip ({tip.x:.2f}, {tip.y:.2f})"


class _Readout:
    """Counts frames and prints hands appearing and leaving."""

    def __init__(self):
        self._lock = threading.Lock()
        self.frames = 0
        self.latest: Optional[HandFrame] = None
        self._sides: List[str] = []

    def __call__(self, frame: HandFrame) -> None:
        sides = sorted(hand.side for hand in frame.hands)
        with self._lock:
            self.frames += 1
            self.latest = frame
            before, self._sides = self._sides, sides
        for side in sides:
            if side not in before:
                print(f"   ✋ {side} hand appeared", flush=True)
        for side in before:
            if side not in sides:
                print(f"   👋 {side} hand left", flush=True)

    def take(self):
        with self._lock:
            return self.frames, self.latest


def _tap(camera: int):
    from .hand_tracking import OpenCVCamera
    from .preview import FrameTap
    return FrameTap(OpenCVCamera(camera))


def _live(args: argparse.Namespace, make_tracker: Callable[[int], HandTracker]) -> int:
    tap = _tap(args.camera) if args.preview else None
    tracker = make_tracker(args.camera) if tap is None else HandTracker(args.camera, camera_factory=lambda i: tap)
    readout = _Readout()
    tracker.subscribe(readout)
    recorder = None
    if args.record:
        recorder = HandRecorder(args.record)
        tracker.subscribe(recorder)
    print(f"✋ Hand tracking on camera {args.camera}", flush=True)
    if not tracker.start():
        if recorder is not None:
            recorder.close()
        return 1
    print(f"   ⏱️ Stopping after {args.seconds:g} s" if args.seconds else "   ⏹️ Press Ctrl+C to stop", flush=True)
    if recorder is not None:
        print(f"   💾 Recording landmarks to {args.record}", flush=True)
    started = time.perf_counter()
    last_tick, last_count = started, 0
    try:
        if tap is not None:
            from .preview import run_window
            print("   🪟 Close the preview window to stop", flush=True)
            run_window(tap, lambda: readout.take()[1], lambda: "watching", args.seconds)
        while tap is None and not (args.seconds and time.perf_counter() - started >= args.seconds):
            time.sleep(0.05)
            if time.perf_counter() - last_tick >= 1.0:
                count, latest = readout.take()
                elapsed = time.perf_counter() - last_tick
                fps = (count - last_count) / elapsed
                last_tick, last_count = time.perf_counter(), count
                if latest is None:
                    print("   ⏳ Waiting for the camera", flush=True)
                    continue
                hands = " · ".join(_describe(h) for h in latest.hands) if latest.hands else "no hands"
                print(f"   📊 {fps:.0f} fps · {hands}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
        if recorder is not None:
            recorder.close()
    count, _ = readout.take()
    print(f"⏹️ Stopped after {count} frames", flush=True)
    if recorder is not None:
        print(f"   💾 Recording saved to {args.record}", flush=True)
    return 0


class _FingerReadout:
    """The latest hand's finger curls and gesture, for choosing the gesture snapping thresholds."""

    def __init__(self, settings: Any = None):
        self._lock = threading.Lock()
        self._settings = settings
        self.frames = 0
        self.latest: Optional[str] = None
        self.hand_frame: Optional[HandFrame] = None

    def __call__(self, frame: HandFrame) -> None:
        from .window_snap import describe_hand, describe_two_hands

        text = None
        self.hand_frame = frame
        if frame.hands:
            hand = max(frame.hands, key=lambda h: h.score)
            text = f"{hand.side} · {describe_hand(hand, self._settings)}"
        if len(frame.hands) >= 2:
            text += f" · two hands: {describe_two_hands(frame.hands[0], frame.hands[1], frame.width, frame.height)}"
        with self._lock:
            self.frames += 1
            self.latest = text

    def take(self):
        with self._lock:
            return self.frames, self.latest


def _snap(args: argparse.Namespace, make_tracker: Optional[Callable[[int], HandTracker]],
          make_output: Optional[Callable[[], Any]]) -> int:
    from ..config import load_settings
    from .window_snap_service import WindowSnapService, WindowSnapUnavailable, snap_tracker

    if make_output is None and sys.platform != "win32":
        print("🖐️ Gesture snapping needs Windows", flush=True)
        return 1
    cfg = load_settings()
    if make_output is None:
        from ..platform.windows.window_snap import WindowsSnapOutput

        def make_output() -> Any:
            return WindowsSnapOutput(use_fancyzones=getattr(cfg, "windows_fancyzones_enabled", True),
                                     languages=(*getattr(cfg, "fast_commands_locales", ["en"]), "en"))
    camera = args.camera if args.camera is not None else cfg.gesture_snap_camera
    from .window_snap import WindowSnapSettings
    readout = _FingerReadout(WindowSnapSettings.from_config(cfg))
    recorder = HandRecorder(args.record) if args.record else None
    tap = _tap(camera) if args.preview else None

    def tracker_for(_index: int) -> HandTracker:
        if tap is not None:
            tracker = snap_tracker(cfg, camera, camera_factory=lambda i: tap)
        else:
            tracker = make_tracker(camera) if make_tracker is not None else snap_tracker(cfg, camera)
        tracker.subscribe(readout)
        if recorder is not None:
            tracker.subscribe(recorder)
        return tracker

    service = WindowSnapService(cfg, output_factory=make_output, tracker_factory=tracker_for)
    try:
        service.start()
    except WindowSnapUnavailable as exc:
        print(f"🖐️ Gesture snapping could not start: {exc}", flush=True)
        if recorder is not None:
            recorder.close()
        return 1
    print(f"   📷 Camera {camera}", flush=True)
    print(f"   ⏱️ Stopping after {args.seconds:g} s" if args.seconds else "   ⏹️ Press Ctrl+C to stop", flush=True)
    started = time.perf_counter()
    last_tick, last_count = started, 0
    try:
        if tap is not None:
            from .preview import run_window
            print("   🪟 Close the preview window to stop", flush=True)
            run_window(tap, lambda: readout.hand_frame, lambda: service.state, args.seconds,
                       lambda: print(f"   📊 {readout.take()[1] or 'no hands'} · {service.state}", flush=True),
                       readout._settings)
        # Crossed wrists turn the mode off, which ends the command too.
        while tap is None and service.running and not (args.seconds
                                                       and time.perf_counter() - started >= args.seconds):
            time.sleep(0.05)
            if time.perf_counter() - last_tick >= 1.0:
                count, latest = readout.take()
                fps = (count - last_count) / (time.perf_counter() - last_tick)
                last_tick, last_count = time.perf_counter(), count
                print(f"   📊 {fps:.0f} fps · {latest or 'no hands'} · {service.state}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        if recorder is not None:
            recorder.close()
            print(f"   💾 Recording saved to {args.record}", flush=True)
    return 0


class _Photo:
    """A camera that shows the same photo for ever."""

    def __init__(self, image: Any):
        self._image = image

    def open(self) -> bool:
        return True

    def read(self) -> Any:
        return self._image.copy()

    def close(self) -> None:
        pass


def _photo(path: str) -> int:
    import cv2

    image = cv2.imread(path)
    if image is None:
        print(f"🖼️ Could not read {path} as an image", flush=True)
        return 1
    found: List[HandFrame] = []
    done = threading.Event()

    def first(frame: HandFrame) -> None:
        if not found:
            found.append(frame)
            done.set()

    tracker = HandTracker(camera_factory=lambda index: _Photo(image))
    tracker.subscribe(first)
    if not tracker.start():
        return 1
    try:
        if not done.wait(30):
            print("🖼️ The photo was not processed in time", flush=True)
            return 1
    finally:
        tracker.stop()
    hands = found[0].hands
    print(f"🖼️ {Path(path).name}: {len(hands)} hand{'' if len(hands) == 1 else 's'}", flush=True)
    for hand in hands:
        print(f"   ✋ {_describe(hand)}", flush=True)
    return 0


def main(argv: Optional[List[str]] = None, *,
         make_tracker: Optional[Callable[[int], HandTracker]] = None,
         make_output: Optional[Callable[[], Any]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a Windows console may not default to UTF-8, and lines start with emoji
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m jarvis.gestures",
                                     description="Watch webcam hand tracking, or run it on a photo.")
    parser.add_argument("--camera", type=int, default=None,
                        help="webcam index (default 0, or gesture_snap_camera with --snap)")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--record", metavar="PATH", help="also write the landmarks to this file")
    parser.add_argument("--image", metavar="PATH", help="find hands in a photo instead of the webcam")
    parser.add_argument("--preview", action="store_true",
                        help="show the camera image with the detected hand drawn on it")
    parser.add_argument("--snap", action="store_true",
                        help="move and scroll windows with your hand, showing each finger's curl to tune thresholds")
    args = parser.parse_args(argv)
    if args.image:
        return _photo(args.image)
    if args.snap:
        return _snap(args, make_tracker, make_output)
    if args.camera is None:
        args.camera = 0
    return _live(args, make_tracker or (lambda index: HandTracker(index)))


if __name__ == "__main__":
    sys.exit(main())

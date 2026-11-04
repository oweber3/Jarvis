#!/usr/bin/env python3
"""Measure how gesture snapping reads real hands, on landmarks from HaGRID photos.

The benchmark is a fixture of MediaPipe Hand Landmarker landmarks (no images) taken from photos of many people
in HaGRID, the HAnd Gesture Recognition Image Dataset, each labelled with the gesture the person was asked to
make: fist, one (pointing), peace (two fingers), palm and stop (open hands), plus relaxed second hands
(no_gesture). See ``tests/fixtures/hand_landmarks/README.md`` for its source and licence.

    python scripts/gesture_benchmark.py report              # accuracy and confusion matrices
    python scripts/gesture_benchmark.py report --split all  # every sample, not only the held-out people
    python scripts/gesture_benchmark.py cost                # per-frame cost of the detector and the reading
    python scripts/gesture_benchmark.py build --hagrid DIR  # rebuild the fixture from HaGRID photos

``build`` needs MediaPipe, OpenCV, the cached hand model (run ``python -m jarvis.gestures`` once) and a copy
of HaGRID's 384p sample (``hagrid-sample-30k-384p``: ``ann_train_val/<class>.json`` and
``hagrid_30k/train_val_<class>/<id>.jpg``). It runs locally; photos are read from that folder and never copied.
``--recogniser PATH`` also records the MediaPipe Gesture Recognizer's reading of each hand.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.hand_benchmark import EXPECTED, FIXTURE, GESTURE_LABELS, Sample, benchmark  # noqa: E402

CLASSES = ("fist", "one", "peace", "palm", "stop")
# The hand is framed as a desk webcam sees it: the crop is this many times the hand's larger side tall, 4:3,
# and scaled to the 640 x 480 frames Jarvis asks the webcam for.
CROP_HANDS = 2.6
FRAME_W, FRAME_H = 640, 480
# A detected hand matches the annotated one when their boxes overlap at least this much (intersection over union).
MIN_IOU = 0.3
FORMAT = "jarvis-hand-benchmark"


# --------------------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------------------

def _crop(image, box) -> Optional[Tuple[object, Tuple[float, float, float, float]]]:
    """The 640 x 480 frame around ``box`` (normalised x, y, w, h of the photo) and the box in that frame."""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    bx, by, bw, bh = box[0] * width, box[1] * height, box[2] * width, box[3] * height
    side = max(bw, bh)
    if side < 12:
        return None
    ch = CROP_HANDS * side
    cw = ch * FRAME_W / FRAME_H
    cx, cy = bx + bw / 2, by + bh / 2
    left, top = int(round(cx - cw / 2)), int(round(cy - ch / 2))
    right, bottom = left + int(round(cw)), top + int(round(ch))
    pad = max(0, -left, -top, right - width, bottom - height)
    if pad:
        image = cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        left, top, right, bottom = left + pad, top + pad, right + pad, bottom + pad
        bx, by = bx + pad, by + pad
    frame = np.ascontiguousarray(cv2.resize(image[top:bottom, left:right], (FRAME_W, FRAME_H),
                                            interpolation=cv2.INTER_LINEAR))
    sx, sy = 1.0 / (right - left), 1.0 / (bottom - top)
    return frame, ((bx - left) * sx, (by - top) * sy, bw * sx, bh * sy)


def _iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    h = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = w * h
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _box_of(landmarks) -> Tuple[float, float, float, float]:
    """The box of 21 landmarks given in the mirrored frame, in the photo's own (unmirrored) orientation."""
    xs = [1.0 - p.x for p in landmarks]
    ys = [p.y for p in landmarks]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


class _Detectors:
    """The Hand Landmarker Jarvis uses (same model, two hands, default thresholds), in image mode so unrelated
    photos never track into each other, and optionally the Gesture Recognizer. Frames are mirrored and converted
    to RGB exactly as ``MediaPipeHands`` does."""

    def __init__(self, recogniser: Optional[str]):
        import cv2
        import mediapipe
        from mediapipe.tasks.python import BaseOptions, vision

        from jarvis.gestures.hand_tracking import ensure_hand_model

        self._cv2, self._mp = cv2, mediapipe
        self._landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_hand_model())),
            running_mode=vision.RunningMode.IMAGE, num_hands=2))
        self._recogniser = None
        if recogniser:
            self._recogniser = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
                base_options=BaseOptions(model_asset_path=recogniser),
                running_mode=vision.RunningMode.IMAGE, num_hands=2))

    def close(self) -> None:
        self._landmarker.close()
        if self._recogniser is not None:
            self._recogniser.close()

    def _image(self, frame):
        import numpy as np
        rgb = self._cv2.cvtColor(self._cv2.flip(frame, 1), self._cv2.COLOR_BGR2RGB)
        return self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))

    def hands(self, frame, box):
        """The detected hand matching ``box``, as (side, score, points, world), or None."""
        result = self._landmarker.detect(self._image(frame))
        best, best_iou = None, MIN_IOU
        for index, points in enumerate(result.hand_landmarks):
            iou = _iou(_box_of(points), box)
            if iou >= best_iou and index < len(result.handedness) and index < len(result.hand_world_landmarks):
                best, best_iou = index, iou
        if best is None:
            return None
        category = result.handedness[best][0]
        return (category.category_name.lower(), float(category.score),
                [(p.x, p.y, p.z) for p in result.hand_landmarks[best]],
                [(p.x, p.y, p.z) for p in result.hand_world_landmarks[best]])

    def recognise(self, frame, box) -> Optional[Tuple[str, float]]:
        if self._recogniser is None:
            return None
        result = self._recogniser.recognize(self._image(frame))
        best, best_iou = None, MIN_IOU
        for index, points in enumerate(result.hand_landmarks):
            iou = _iou(_box_of(points), box)
            if iou >= best_iou and index < len(result.gestures) and result.gestures[index]:
                best, best_iou = index, iou
        if best is None:
            return None
        top = result.gestures[best][0]
        return top.category_name, round(float(top.score), 3)


def _split(user: str) -> str:
    """Half the people tune, the other half are held out, decided by their (hashed) HaGRID user id."""
    return "tune" if int(hashlib.sha256(user.encode()).hexdigest(), 16) % 2 == 0 else "test"


def _series_jobs(root: Path, taken, per_class: int) -> List[Tuple[str, str, str, list]]:
    """Several photos of each gesture from the same people, for replaying a person's hand closing, holding and
    opening: everyone with at least two photos each of a fist, one finger and an open hand (palm or stop),
    up to ``per_class`` photos of each of the five gestures, skipping photos already ``taken``."""
    photos: Dict[str, Dict[str, List[Tuple[str, list]]]] = defaultdict(lambda: defaultdict(list))
    for name in CLASSES:
        annotations = json.loads((root / "ann_train_val" / f"{name}.json").read_text())
        for path in sorted((root / "hagrid_30k" / f"train_val_{name}").glob("*.jpg")):
            entry = annotations.get(path.stem)
            if entry and name in entry["labels"]:
                photos[entry["user_id"]][name].append((path.stem, entry["bboxes"][entry["labels"].index(name)]))
    jobs = []
    for user in sorted(photos):
        have = photos[user]
        if len(have["fist"]) < 2 or len(have["one"]) < 2 or len(have["palm"]) + len(have["stop"]) < 2:
            continue
        for name in CLASSES:
            for image_id, box in have[name][:per_class]:
                if (name, image_id) not in taken:
                    jobs.append((name, image_id, user, box))
    return jobs


def build(args) -> None:
    import cv2

    root = Path(args.hagrid)
    rng = random.Random(args.seed)
    detectors = _Detectors(args.recogniser)
    jobs: List[Tuple[str, str, str, list]] = []  # (label, image id, user, box)
    relaxed: List[Tuple[str, str, str, list]] = []
    for name in CLASSES:
        annotations = json.loads((root / "ann_train_val" / f"{name}.json").read_text())
        folder = root / "hagrid_30k" / f"train_val_{name}"
        ids = sorted(p.stem for p in folder.glob("*.jpg") if p.stem in annotations)
        by_user: Dict[str, List[str]] = defaultdict(list)
        for image_id in ids:
            by_user[annotations[image_id]["user_id"]].append(image_id)
        users = sorted(by_user)
        rng.shuffle(users)
        for user in users[:args.per_class]:  # one photo per person
            image_id = rng.choice(by_user[user])
            entry = annotations[image_id]
            for label, box in zip(entry["labels"], entry["bboxes"]):
                if label == name:
                    jobs.append((name, image_id, user, box))
                elif label == "no_gesture":
                    relaxed.append(("no_gesture", f"{name}/{image_id}", user, box))
    rng.shuffle(relaxed)
    jobs += relaxed[:args.relaxed]
    series = _series_jobs(root, {(label, image_id) for label, image_id, _, _ in jobs}, args.series_photos)
    jobs += series
    people: Dict[str, int] = {}
    rows, missed = [], Counter()
    main_jobs = len(jobs) - len(series)
    started = time.perf_counter()
    for count, (label, image_id, user, box) in enumerate(jobs, 1):
        folder = label if label != "no_gesture" else image_id.split("/")[0]
        photo = cv2.imread(str(root / "hagrid_30k" / f"train_val_{folder}" / f"{image_id.split('/')[-1]}.jpg"))
        cropped = _crop(photo, box) if photo is not None else None
        found = detectors.hands(*cropped) if cropped else None
        if found is None:
            missed[label] += 1
            continue
        side, score, points, world = found
        rows.append({"id": image_id.split("/")[-1], "user": people.setdefault(user, len(people)),
                     "split": _split(user), "label": label, "extra": count > main_jobs,
                     "side": side, "score": round(score, 3),
                     "points": [[round(v, 4) for v in p] for p in points],
                     "world": [[round(v, 5) for v in p] for p in world],
                     "recogniser": detectors.recognise(*cropped)})
        if count % 250 == 0:
            print(f"   ⏳ {count}/{len(jobs)} photos, {time.perf_counter() - started:.0f} s", flush=True)
    detectors.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = {"format": FORMAT, "version": 1, "source": "HaGRID hagrid-sample-30k-384p (see README.md)",
              "frame": [FRAME_W, FRAME_H], "crop_hands": CROP_HANDS, "seed": args.seed,
              "recogniser": bool(args.recogniser)}
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(json.dumps(header) + "\n")
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"📦 {len(rows)} hands from {len(people)} people written to {out}")
    for label in CLASSES + ("no_gesture",):
        kept = sum(1 for r in rows if r["label"] == label)
        print(f"   ✋ {label:10s} {kept:5d} kept, {missed[label]:4d} not found by the detector")
    print(f"   🔁 {sum(1 for r in rows if r['extra'])} extra photos of people replayed as sequences")


# --------------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------------

SHAPES = ("fist", "one finger", "two fingers", "open hand", "other")


def _readings(samples: Sequence[Sample]) -> List[str]:
    from jarvis.gestures.window_snap import WindowSnapSettings, read_shape
    settings = WindowSnapSettings()
    return [read_shape(s.hand, settings) for s in samples]


def _matrix(samples: Sequence[Sample], readings: Sequence[str]) -> None:
    labels = [label for label in GESTURE_LABELS if any(s.label == label for s in samples)]
    print("   " + " " * 12 + "".join(f"{shape:>13s}" for shape in SHAPES) + "      n  correct")
    for label in labels:
        row = Counter(r for s, r in zip(samples, readings) if s.label == label)
        n = sum(row.values())
        right = row[EXPECTED[label]] / n * 100 if n else 0.0
        print(f"   {label:12s}" + "".join(f"{row[shape] / n * 100:12.1f}%" for shape in SHAPES)
              + f"  {n:5d}  {right:6.1f}%")


def _fist_scores(samples: Sequence[Sample], readings: Sequence[str]) -> Tuple[float, float, int, int]:
    pairs = [(s.label == "fist", r == "fist") for s, r in zip(samples, readings) if s.label in GESTURE_LABELS]
    tp = sum(1 for truth, read in pairs if truth and read)
    fp = sum(1 for truth, read in pairs if read and not truth)
    fn = sum(1 for truth, read in pairs if truth and not read)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall, fp, fn


def report(args) -> None:
    samples = benchmark(args.split, args.fixture)
    gestures = [s for s in samples if s.label in GESTURE_LABELS]
    readings = _readings(gestures)
    people = len({s.user for s in gestures})
    print(f"🎯 Gesture reading on {len(gestures)} HaGRID hands from {people} people ({args.split} split)")
    print("   📊 Rows: the gesture asked for. Columns: what gesture snapping reads (each finger fresh, as on the "
          "first frame)")
    _matrix(gestures, readings)
    correct = sum(1 for s, r in zip(gestures, readings) if r == EXPECTED[s.label]) / len(gestures) * 100
    precision, recall, fp, fn = _fist_scores(gestures, readings)
    print(f"   ✅ Overall {correct:.1f}% read as asked")
    print(f"   ✊ Fist precision {precision * 100:.1f}% ({fp} other hands read as a fist), "
          f"recall {recall * 100:.1f}% ({fn} fists missed)")
    relaxed = [s for s in samples if s.label == "no_gesture"]
    if relaxed:
        shares = Counter(_readings(relaxed))
        print(f"   💤 Relaxed second hands (no gesture asked, n={len(relaxed)}): "
              + ", ".join(f"{shape} {shares[shape] / len(relaxed) * 100:.0f}%" for shape in SHAPES))
    recognised = [s for s in gestures if s.recogniser is not None]
    if recognised and args.recogniser:
        _recogniser_report(recognised)


_RECOGNISER = {"Closed_Fist": "fist", "Pointing_Up": "one finger", "Victory": "two fingers",
               "Open_Palm": "open hand"}


def _recogniser_report(samples: Sequence[Sample]) -> None:
    from jarvis.gestures.window_snap import WindowSnapSettings, read_shape
    settings = WindowSnapSettings()
    print(f"\n🤖 MediaPipe Gesture Recognizer on the same hands (n={len(samples)})")
    readings = [_RECOGNISER.get(s.recogniser[0], "other") for s in samples]
    _matrix(samples, readings)
    precision, recall, fp, fn = _fist_scores(samples, readings)
    print(f"   ✊ Recogniser alone: fist precision {precision * 100:.1f}% ({fp}), recall {recall * 100:.1f}% ({fn})")
    both = ["fist" if r == "fist" and read_shape(s.hand, settings) == "fist" else "other"
            for s, r in zip(samples, readings)]
    precision, recall, fp, fn = _fist_scores(samples, both)
    print(f"   🤝 Fist only when both agree: precision {precision * 100:.1f}% ({fp}), recall {recall * 100:.1f}% ({fn})")


# --------------------------------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------------------------------

def cost(args) -> None:
    from jarvis.gestures.window_snap import WindowSnapSettings, read_shape
    samples = [s for s in benchmark("all", args.fixture) if s.label in GESTURE_LABELS]
    settings = WindowSnapSettings()
    rounds = max(1, 20000 // len(samples))
    started = time.perf_counter()
    for _ in range(rounds):
        for s in samples:
            read_shape(s.hand, settings)
    per = (time.perf_counter() - started) / (rounds * len(samples))
    print(f"⏱️  Reading one hand's gesture: {per * 1e6:.1f} µs (pure Python, {rounds * len(samples)} hands)")
    if args.hagrid:
        _detector_cost(args)


def _detector_cost(args) -> None:
    """Per-frame cost of the Hand Landmarker and the Gesture Recognizer in video mode, as Jarvis runs it, on
    640 x 480 frames: a still hand (tracked from frame to frame) and a new hand every frame (searched afresh)."""
    import cv2
    import mediapipe
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision

    from jarvis.gestures.hand_tracking import ensure_hand_model

    root = Path(args.hagrid)
    annotations = json.loads((root / "ann_train_val" / "fist.json").read_text())
    photos = sorted((root / "hagrid_30k" / "train_val_fist").glob("*.jpg"))[:60]
    frames = []
    for path in photos:
        entry = annotations.get(path.stem)
        image = cv2.imread(str(path))
        cropped = _crop(image, entry["bboxes"][entry["labels"].index("fist")]) if entry and image is not None else None
        if cropped:
            frames.append(cropped[0])

    def image(frame):
        rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
        return mediapipe.Image(image_format=mediapipe.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))

    makers = {"Hand Landmarker": lambda: vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ensure_hand_model())), running_mode=vision.RunningMode.VIDEO,
        num_hands=2))}
    if args.recogniser:
        makers["Gesture Recognizer"] = lambda: vision.GestureRecognizer.create_from_options(
            vision.GestureRecognizerOptions(base_options=BaseOptions(model_asset_path=args.recogniser),
                                            running_mode=vision.RunningMode.VIDEO, num_hands=2))
    for name, make in makers.items():
        for case, sequence in (("still hand", [frames[0]] * 150), ("new hand every frame", frames * 3)):
            task = make()
            run = task.detect_for_video if hasattr(task, "detect_for_video") else task.recognize_for_video
            times = []
            for i, frame in enumerate(sequence):
                mp_image = image(frame)
                started = time.perf_counter()
                run(mp_image, 33 * (i + 1))
                times.append(time.perf_counter() - started)
            task.close()
            times = times[10:]
            print(f"⏱️  {name:18s} {case:21s} median {statistics.median(times) * 1000:5.1f} ms, "
                  f"p90 {sorted(times)[int(len(times) * 0.9)] * 1000:5.1f} ms")


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="rebuild the fixture from HaGRID photos")
    b.add_argument("--hagrid", required=True, help="folder holding ann_train_val/ and hagrid_30k/")
    b.add_argument("--out", default=str(FIXTURE))
    b.add_argument("--per-class", type=int, default=700, help="people (one photo each) per gesture")
    b.add_argument("--relaxed", type=int, default=500, help="relaxed second hands to keep")
    b.add_argument("--series-photos", type=int, default=4,
                   help="photos of each gesture kept for each person replayed as a sequence")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--recogniser", help="path of gesture_recognizer.task to also record its reading")
    r = sub.add_parser("report", help="accuracy and confusion matrices")
    r.add_argument("--fixture", default=str(FIXTURE))
    r.add_argument("--split", choices=("test", "tune", "all"), default="test")
    r.add_argument("--recogniser", action="store_true", help="also report the recorded Gesture Recognizer readings")
    c = sub.add_parser("cost", help="per-frame cost")
    c.add_argument("--fixture", default=str(FIXTURE))
    c.add_argument("--hagrid", help="HaGRID folder, to also time the detectors on real frames")
    c.add_argument("--recogniser", help="path of gesture_recognizer.task to time it too")
    args = parser.parse_args(argv)
    {"build": build, "report": report, "cost": cost}[args.command](args)


if __name__ == "__main__":
    main()

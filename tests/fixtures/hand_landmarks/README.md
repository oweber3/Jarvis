# Hand landmarks from HaGRID

`hagrid_landmarks.jsonl.gz` holds MediaPipe Hand Landmarker landmarks of 4,812 hands from 2,392 people, each labelled with the gesture the person was asked to make. It holds no images. Gesture snapping's benchmark (`scripts/gesture_benchmark.py`) and its tests read it: `tests/test_gesture_benchmark.py` (single frames, through `tests/hand_benchmark.py`) and `tests/test_gesture_sequences.py` (people's own hands replayed over time, through `tests/hand_sequences.py`).

## Source and attribution

The hands come from **HaGRID, the HAnd Gesture Recognition Image Dataset**, by Alexander Kapitanov, Karina Kvanchiani, Alexander Nagaev, Roman Kraynov and Andrei Makhliarchuk (SberDevices): <https://github.com/hukenovs/hagrid>.

> Kapitanov, A., Kvanchiani, K., Nagaev, A., Kraynov, R. and Makhliarchuk, A. HaGRID – HAnd Gesture Recognition Image Dataset. *IEEE/CVF Winter Conference on Applications of Computer Vision (WACV)*, 2024. arXiv:2206.08219.

The photos used are the 384-pixel sample of HaGRID published by Christian J. Mills as `hagrid-sample-30k-384p` (about 1,700 photos per gesture, with HaGRID's annotations), read from a public copy of it on GitHub.

## Licence

HaGRID is provided under its own licence, a variant of Creative Commons Attribution-ShareAlike 4.0: <https://github.com/hukenovs/hagrid/blob/master/license/en_us.pdf>. This file is Adapted Material made from HaGRID and is provided under that same licence. The licence of the rest of Jarvis does not apply to it. HaGRID's licence comes with no warranty.

The Gesture Recognizer readings recorded with each hand come from Google's MediaPipe Gesture Recognizer model (`gesture_recognizer.task`, float16, version 1), used under the Apache License 2.0.

## What was changed

For each photo, the annotated hand was framed as a desk webcam would show it: a 4:3 crop 2.6 times the hand's larger side tall, centred on HaGRID's box and scaled to 640 x 480. The crop was mirrored and passed to the same Hand Landmarker model Jarvis uses (`hand_landmarker.task`, float16, version 1, two hands, MediaPipe's default thresholds) in image mode, and the hand whose landmarks overlap HaGRID's box was kept. Only that hand's 21 image points, 21 world points, side and score were kept, rounded to 4 and 5 decimal places. Photos were read locally and never copied.

- **Gestures:** `fist`, `one` (index finger), `peace` (two fingers), `palm` and `stop` (open hands, fingers spread and together): one photo per person for 700 people each, chosen with a fixed seed. Hands the detector did not find are left out.
- **Relaxed hands:** `no_gesture` hands are second hands in the same photos with no gesture asked for (430 of them).
- **Sequences:** `extra` rows are further photos of the 87 people with at least two photos each of a fist, one finger and an open hand, so tests can replay a person's own hand closing, holding and opening.
- **People:** HaGRID's hashed user ids were replaced by sequential numbers. Half the people (by their HaGRID id) are marked `tune` and half `test`, so thresholds tuned on one half can be checked on people they never saw. `id` is the HaGRID image id the hand came from.

## Format

Gzipped JSON Lines. The first line is the header `{"format": "jarvis-hand-benchmark", "version": 1, ...}`. Each further line is one hand: `id`, `user`, `split`, `label`, `extra`, `side`, `score`, `points` and `world` (21 `[x, y, z]` each, as in `jarvis.gestures.hand_tracking.Hand`) and `recogniser` (the Gesture Recognizer's top gesture and score, or `null`).

Rebuild it with `python scripts/gesture_benchmark.py build --hagrid DIR --recogniser PATH`, where `DIR` holds the sample's `ann_train_val/` and `hagrid_30k/`.

#!/usr/bin/env python3
"""Measure speaker verification, barge-in and echo cancellation on Piper voices.

No microphone and no speakers: every voice is synthesised to an array and fed
through the WAV harness (tests/audio_harness). Shares the CPU with whatever
else is running, so rerun it at a quiet time for clean timings.

    python scripts/measure_voice_id.py accuracy
    python scripts/measure_voice_id.py barge-in
    python scripts/measure_voice_id.py aec
    python scripts/measure_voice_id.py all

Needs the speaker model (JARVIS_SPEAKER_MODEL, or run scripts/enrol_voice.py
once to fetch it) and Piper voices (JARVIS_HARNESS_VOICES).
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.audio_harness import ListenerHarness, silence, speakers  # noqa: E402
from tests.audio_harness.audio import rms  # noqa: E402

SR = 16000
CONDITIONS = [None, "music10", "noise10", "noise5"]


def _pct(values, q):
    return float(np.percentile(values, q)) if len(values) else float("nan")


def accuracy() -> None:
    print("🎯 Speaker verification accuracy (Piper voices; the owner is enrolled from 6 phrases)")
    verifier = speakers.make_verifier("soft")
    reject, accept = verifier.reject_threshold, verifier.accept_threshold
    print(f"   ⚙️  soft rejects below {reject}, strict accepts from {accept}")
    centroid = speakers.enrolled_centroid()
    for condition in CONDITIONS:
        def scores(voice):
            out = []
            for i, phrase in enumerate(speakers.HELD_OUT_PHRASES):
                audio = speakers.speak(phrase, voice)
                if len(audio) >= 0.5 * SR:
                    out.append(float(speakers.embedder().embed(speakers.with_bed(audio, condition, seed=i)) @ centroid))
            return out
        owner = scores(speakers.OWNER)
        others = [s for v in speakers.other_voices() for s in scores(v)]
        label = condition or "clean"
        print(f"   🎧 {label:8s} owner n={len(owner)} min={min(owner):.2f} mean={np.mean(owner):.2f} | "
              f"others n={len(others)} max={max(others):.2f} p99={_pct(others, 99):.2f}")
        print(f"      🔓 soft:   owner wrongly ignored {np.mean([s < reject for s in owner]) * 100:.0f}%, "
              f"other voices let through {np.mean([s >= reject for s in others]) * 100:.1f}%")
        print(f"      🔒 strict: owner wrongly ignored {np.mean([s < accept for s in owner]) * 100:.0f}%, "
              f"other voices let through {np.mean([s >= accept for s in others]) * 100:.1f}%")
    window = speakers.speak(speakers.HELD_OUT_PHRASES[3], speakers.OWNER)[: int(0.3 * SR)]
    started = time.perf_counter()
    for _ in range(20):
        speakers.embedder().embed(window)
    print(f"   ⏱️  embedding a 300 ms window: {(time.perf_counter() - started) / 20 * 1000:.1f} ms on the CPU")


def _barge_in_run(user_audio, over_db, tts_text=speakers.JARVIS_REPLY, offset=1.0):
    tts_audio = speakers.speak(tts_text, speakers.JARVIS_TTS)
    mic = speakers.room_with_jarvis_talking(tts_audio, user_audio, user_offset=offset, user_over_tts_db=over_db)
    verifier = speakers.make_verifier("soft")
    with ListenerHarness(speaker_verifier=verifier, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start(tts_text)
        start = harness.now
        harness.play(mic, text="")
        harness.play(silence(1.5))
    ducked = harness.tts.time_of("duck")
    if user_audio is None:
        return ducked is not None, None
    voiced = np.nonzero(np.abs(user_audio) > 0.05 * np.abs(user_audio).max())[0]
    onset = start + offset + float(voiced[0]) / SR
    return ducked is not None, (None if ducked is None else ducked - onset)


def barge_in() -> None:
    print("🗣️  Barge-in: time from the owner's first word to Jarvis being ducked (simulated clock)")
    for over_db in (12.0, 6.0, 0.0, -6.0):
        delays, missed = [], 0
        for phrase in speakers.HELD_OUT_PHRASES[:4]:
            _, delay = _barge_in_run(speakers.speak(phrase, speakers.OWNER), over_db)
            if delay is None:
                missed += 1
            else:
                delays.append(delay)
        median = f"{statistics.median(delays):.2f} s" if delays else "never"
        print(f"   🎤 owner {over_db:+.0f} dB over Jarvis's voice: median {median}, "
              f"slowest {max(delays) if delays else float('nan'):.2f} s, missed {missed}/4")
    triggers = sum(_barge_in_run(None, 0.0, text)[0] for text in (
        speakers.JARVIS_REPLY, "Sure. I have opened Word and set the volume to thirty percent. Anything else?"))
    print(f"   🔇 Jarvis hearing only itself: {triggers}/2 false ducks")
    strangers = sum(_barge_in_run(speakers.speak(speakers.HELD_OUT_PHRASES[3], v), 6.0)[0]
                    for v in speakers.other_voices())
    print(f"   👥 Another person talking over Jarvis (+6 dB): {strangers}/{len(speakers.other_voices())} false ducks")
    print("   ℹ️  Add up to 46 ms for the audio block in which the volume ramps, plus the output device's own latency")


# -- echo cancellation evaluation (not shipped) ---------------------------------

BLOCK, PARTITIONS = 320, 24  # 20 ms blocks, 480 ms of echo path


def _room(seed, delay_ms, rt60, taps=4800):
    from scipy.signal import fftconvolve  # noqa: F401  (used by the caller)

    rng = np.random.default_rng(seed)
    t = np.arange(taps) / SR
    ir = rng.standard_normal(taps) * np.exp(-6.9 * t / rt60) * 0.15
    start = int(delay_ms * SR / 1000)
    ir = np.concatenate([np.zeros(start), ir])[:taps]
    ir[start] += 1.0
    return ir / np.abs(ir).max()


class _PartitionedFilter:
    """Partitioned-block frequency-domain adaptive filter with a simple near-end-talker guard."""

    def __init__(self, mu=0.5):
        self.w = np.zeros((PARTITIONS, BLOCK + 1), dtype=np.complex128)
        self.x = np.zeros((PARTITIONS, BLOCK + 1), dtype=np.complex128)
        self.prev = np.zeros(BLOCK)
        self.power = np.full(BLOCK + 1, 1e-3)
        self.mu, self.blocks, self.gain = mu, 0, None

    def process(self, ref, mic):
        out = np.zeros_like(mic)
        for i in range(0, len(mic) - BLOCK + 1, BLOCK):
            x, d = ref[i:i + BLOCK], mic[i:i + BLOCK]
            spectrum = np.fft.rfft(np.concatenate([self.prev, x]))
            self.prev = x.copy()
            self.x = np.roll(self.x, 1, axis=0)
            self.x[0] = spectrum
            echo = np.fft.irfft((self.w * self.x).sum(0))[BLOCK:]
            error = d - echo
            out[i:i + BLOCK] = error
            self.power = 0.9 * self.power + 0.1 * (np.abs(self.x) ** 2).sum(0)
            self.blocks += 1
            mic_rms, ref_rms = rms(d), rms(x)
            near_end = self.gain is not None and self.blocks > 50 and mic_rms > 2.5 * self.gain * ref_rms
            if ref_rms > 1e-4 and not near_end:
                ratio = mic_rms / ref_rms
                self.gain = ratio if self.gain is None else 0.95 * self.gain + 0.05 * ratio
                err = np.fft.rfft(np.concatenate([np.zeros(BLOCK), error]))
                grad = np.fft.irfft(np.conj(self.x) * err / (self.power + 1e-6), axis=1)
                grad[:, BLOCK:] = 0
                self.w += self.mu * np.fft.rfft(grad, axis=1)
        return out


def aec() -> None:
    from scipy.signal import fftconvolve

    print("🔁 Echo cancellation using Jarvis's own samples as the reference (simulated room, not shipped)")
    embedder, centroid = speakers.embedder(), speakers.enrolled_centroid()
    tts = speakers.speak(speakers.JARVIS_REPLY, speakers.JARVIS_TTS)
    tts = tts * (0.04 / rms(tts))
    owner_clip = speakers.speak(speakers.HELD_OUT_PHRASES[3], speakers.OWNER)
    offset = 2.5
    threshold = 0.2
    for seed, delay, rt60 in [(1, 30, 0.2), (2, 50, 0.3), (3, 90, 0.4)]:
        spk = np.tanh(1.8 * tts / np.abs(tts).max()) / 1.8  # mild loudspeaker nonlinearity
        echo = fftconvolve(spk, _room(seed, delay, rt60))[: len(tts)].astype(np.float32)
        echo *= 0.04 / rms(echo)
        started = time.perf_counter()
        residual = _PartitionedFilter().process(tts, echo)
        cpu = (time.perf_counter() - started) / (len(echo) / SR)
        erle = 10 * np.log10(np.mean(echo[int(1.5 * SR):int(2.5 * SR)] ** 2)
                             / max(np.mean(residual[int(1.5 * SR):int(2.5 * SR)] ** 2), 1e-12))
        print(f"   🏠 room: {delay} ms delay, {rt60:.1f} s reverb | echo reduced by {erle:.1f} dB "
              f"after 2 s | {cpu * 100:.1f}% of one core")
        for over_db in (6, 0, -6):
            owner = owner_clip * (0.04 * 10 ** (over_db / 20) / rms(owner_clip))
            mic = echo.copy()
            end = min(len(mic), int(offset * SR) + len(owner))
            mic[int(offset * SR):end] += owner[: end - int(offset * SR)]
            cleaned = _PartitionedFilter().process(tts, mic)

            def first_detection(signal):
                run = 0
                for k, end_t in enumerate(np.arange(offset + 0.3, offset + 1.5, 0.1)):
                    window = signal[int((end_t - 0.3) * SR):int(end_t * SR)]
                    run = run + 1 if float(embedder.embed(window) @ centroid) >= threshold else 0
                    if run >= 2:
                        return f"{0.3 + 0.1 * k:.1f} s"
                return "never"
            print(f"      🎤 owner {over_db:+d} dB: detected after {first_detection(mic)} without cancellation, "
                  f"{first_detection(cleaned)} with")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["accuracy", "barge-in", "aec", "all"])
    args = parser.parse_args()
    if not speakers.available():
        print(f"❌ {speakers.skip_reason()}")
        return 1
    for name, run in (("accuracy", accuracy), ("barge-in", barge_in), ("aec", aec)):
        if args.what in (name, "all"):
            run()
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Isolated load cost of each local model Jarvis starts: time, RSS, VRAM, and steady CPU of per-frame work.

python measure_models.py whisper|kokoro|wakesound|vad
Config comes from JARVIS_CONFIG_PATH (a temp copy). No audio is captured or played.
"""
import json
import subprocess
import sys
import time

import numpy as np
import psutil

P = psutil.Process()


def vram():
    out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True)
    return int(out.strip().splitlines()[0])


def tree_rss():
    rss = P.memory_info().rss
    for c in P.children(recursive=True):
        try:
            rss += c.memory_info().rss
        except psutil.Error:
            pass
    return rss // 2**20


def main(which):
    from jarvis.config import load_settings
    cfg = load_settings()
    res = {"which": which}
    v0, r0 = vram(), tree_rss()
    t = time.time()
    if which == "whisper":
        from jarvis.listening import listener as _l; _l._probe_windows_cuda_libraries("cuda"); v0, r0 = vram(), tree_rss(); t = time.time()
        from faster_whisper import WhisperModel
        m = WhisperModel(cfg.whisper_model, device="cuda", compute_type=cfg.whisper_compute_type)
        res["load_s"] = round(time.time() - t, 2)
        audio = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32)
        t = time.time()
        segs, _ = m.transcribe(audio, language=None, vad_filter=False)
        list(segs)
        res["warmup_transcribe_s"] = round(time.time() - t, 2)
        res["model"] = cfg.whisper_model
    elif which == "kokoro":
        from jarvis.output.tts import create_tts_engine
        tts = create_tts_engine(engine="kokoro", enabled=True, voice=None, rate=None,
                                kokoro_voice=cfg.tts_kokoro_voice, kokoro_speed=cfg.tts_kokoro_speed,
                                kokoro_device=cfg.tts_kokoro_device, pc_speakers_enabled=False)
        tts.start()
        res["load_s"] = round(time.time() - t, 2)
        res["device"] = cfg.tts_kokoro_device
        res["children"] = [(c.name(), c.memory_info().rss // 2**20) for c in P.children(recursive=True)]
        time.sleep(2)
        res["vram_mb"] = vram() - v0
        res["rss_mb"] = tree_rss() - r0
        t = time.time()
        tts.stop()
        res["stop_s"] = round(time.time() - t, 2)
        print("MODEL_RESULT " + json.dumps(res), flush=True)
        return
    elif which == "wakesound":
        from jarvis.listening.wake_sound import create_wake_sound_detector
        det = create_wake_sound_detector(cfg)
        res["load_s"] = round(time.time() - t, 2)
        pcm = (np.random.default_rng(0).standard_normal(16000 * 30) * 30).astype(np.int16)
        c0 = sum(P.cpu_times()[:2])
        t = time.time()
        for i in range(0, len(pcm), 320):  # 20 ms frames like the listener
            det.feed(pcm[i:i + 320])
        wall = time.time() - t
        res["cpu_s_per_audio_s"] = round((sum(P.cpu_times()[:2]) - c0) / 30, 4)
        res["wall_s_per_audio_s"] = round(wall / 30, 4)
    elif which == "vad":
        import webrtcvad
        v = webrtcvad.Vad(2)
        frame = (np.random.default_rng(0).standard_normal(320) * 30).astype(np.int16).tobytes()
        c0 = sum(P.cpu_times()[:2])
        for _ in range(50 * 30):
            v.is_speech(frame, 16000)
        res["cpu_s_per_audio_s"] = round((sum(P.cpu_times()[:2]) - c0) / 30, 5)
    time.sleep(2)
    res["vram_mb"] = vram() - v0
    res["rss_mb"] = tree_rss() - r0
    print("MODEL_RESULT " + json.dumps(res), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])

"""Measurement harness: runs the Jarvis daemon with a silent fake microphone and muted speakers.

Usage: python bench_daemon.py smoke|run [--sample-threads OUT.json]
Every stdout line is prefixed with seconds since process creation.
"""
import os
import sys
import threading
import time

import psutil

_PROC = psutil.Process()
_T0 = _PROC.create_time()


class _Stamp:
    def __init__(self, inner):
        self._inner = inner
        self._bol = True
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            out = []
            for ch in s.splitlines(keepends=True):
                if self._bol:
                    out.append(f"[{time.time() - _T0:8.3f}] ")
                out.append(ch)
                self._bol = ch.endswith("\n")
            self._inner.write("".join(out))
        return len(s)

    def flush(self):
        self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


sys.stdout = _Stamp(sys.stdout)
print(f"HARNESS_START", flush=True)

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402


class FakeInputStream:
    """Delivers silence at real-time pace to the callback; never opens a device."""

    def __init__(self, samplerate=16000, channels=1, dtype="float32", blocksize=320, callback=None, **_):
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize or 320
        self.callback = callback
        self._run = False
        self._t = None
        self.active = False
        self.closed = False

    def _loop(self):
        period = self.blocksize / float(self.samplerate)
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal((self.samplerate * 4, self.channels)) * 0.001).astype(np.float32)
        pos = 0
        nxt = time.perf_counter()
        while self._run:
            if pos + self.blocksize > len(noise):
                pos = 0
            block = noise[pos:pos + self.blocksize]
            pos += self.blocksize
            if self.callback is not None:
                try:
                    self.callback(block, self.blocksize, None, sd.CallbackFlags())
                except (sd.CallbackStop, sd.CallbackAbort):
                    break
            nxt += period
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)

    def start(self):
        self._run = True
        self.active = True
        self._t = threading.Thread(target=self._loop, daemon=True, name="fake-mic")
        self._t.start()

    def stop(self):
        self._run = False
        self.active = False
        if self._t is not None and self._t is not threading.current_thread():
            self._t.join(1.0)

    abort = stop

    def close(self):
        self.stop()
        self.closed = True

    def read(self, frames):
        time.sleep(frames / float(self.samplerate))
        return np.zeros((frames, self.channels), dtype=np.float32), False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.close()


class FakeOutputStream(FakeInputStream):
    def write(self, data):
        time.sleep(len(data) / float(self.samplerate))


sd.InputStream = FakeInputStream
sd.OutputStream = FakeOutputStream
sd.RawOutputStream = FakeOutputStream
sd.play = lambda *a, **k: None
sd.wait = lambda *a, **k: None
sd.rec = lambda frames, *a, **k: np.zeros((int(frames), 1), dtype=np.float32)


def _thread_sampler(path, interval):
    import json
    import collections
    counts = collections.Counter()
    samples = 0
    cpu_prev = {}
    names = {}
    stop_at = time.time() + float(os.environ.get("BENCH_SAMPLE_SEC", "300"))
    while time.time() < stop_at:
        time.sleep(interval)
        samples += 1
        frames = sys._current_frames()
        for th in threading.enumerate():
            names[th.native_id] = th.name
            f = frames.get(th.ident)
            if f is None:
                continue
            code = f.f_code
            counts[f"{th.name} :: {code.co_filename.split('src')[-1]}:{f.f_lineno} {code.co_name}"] += 1
    cpu = {}
    for t in _PROC.threads():
        cpu[names.get(t.id, f"native-{t.id}")] = round(t.user_time + t.system_time, 2)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"samples": samples, "top_frames": counts.most_common(80),
                   "thread_cpu_s": dict(sorted(cpu.items(), key=lambda kv: -kv[1]))}, fh, indent=1)
    print(f"THREAD_SAMPLE_WRITTEN {path}", flush=True)


def main():
    mode = sys.argv[1]
    if "--sample-threads" in sys.argv:
        out = sys.argv[sys.argv.index("--sample-threads") + 1]
        delay = float(os.environ.get("BENCH_SAMPLE_DELAY", "90"))
        _tm = threading.Timer(delay, _thread_sampler, args=(out, 0.25)); _tm.daemon = True; _tm.start()
    from jarvis.daemon import main as daemon_main
    print("DAEMON_IMPORTED", flush=True)
    daemon_main(smoke_test=(mode == "smoke"))
    print("DAEMON_MAIN_RETURNED", flush=True)


if __name__ == "__main__":
    main()

"""Runs the harness (bench_daemon.py) with a temp config: never the real config, mic or speakers.

Outputs (logs, JSON) go to BENCH_OUT (default: the current folder); point JARVIS_CONFIG_PATH copies at a temp DB.

Runs the harness as a child process and records timings, RSS, threads, VRAM.

python run_bench.py smoke CONFIG N TAG
python run_bench.py run CONFIG TAG IDLE_SEC   (closes stdin after IDLE_SEC, measures shutdown)
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("BENCH_PYTHON", sys.executable)
SRC = os.environ.get("BENCH_SRC", os.path.abspath(os.path.join(HERE, "..", "..", "..", "src")))
OLLAMA = os.environ.get("BENCH_OLLAMA") or shutil.which("ollama") or "ollama"
OUT = os.environ.get("BENCH_OUT", os.getcwd())


def vram_used():
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True)
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


def gpu_apps():
    try:
        return subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader"], text=True)
    except Exception:
        return ""


def ollama_ps():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=3) as r:
            data = json.load(r)
        return [{"name": m["name"], "size_mb": m["size"] // 2**20, "vram_mb": m.get("size_vram", 0) // 2**20,
                 "ctx": m.get("context_length")} for m in data.get("models", [])]
    except Exception as e:
        return str(e)


def ollama_unload_all():
    ps = ollama_ps()
    if isinstance(ps, list):
        for m in ps:
            subprocess.run([OLLAMA, "stop", m["name"]], capture_output=True)
    time.sleep(2)


def tree_stats(p):
    rss = 0
    threads = 0
    cpu = 0.0
    procs = [p]
    try:
        procs += p.children(recursive=True)
    except psutil.Error:
        pass
    names = []
    for q in procs:
        try:
            with q.oneshot():
                rss += q.memory_info().rss
                threads += q.num_threads()
                t = q.cpu_times()
                cpu += t.user + t.system
                names.append(q.name())
        except psutil.Error:
            pass
    return rss, threads, cpu, names


def launch(mode, config, extra=()):
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC
    env["JARVIS_CONFIG_PATH"] = config
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if mode == "run":
        env["JARVIS_STDIN_IPC"] = "1"
    return subprocess.Popen([PY, os.path.join(HERE, "bench_daemon.py"), mode, *extra],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def reader(proc, lines, log):
    for line in proc.stdout:
        lines.append((time.time(), line.rstrip("\n")))
        log.write(line)
        log.flush()


def stamp_of(lines, needle):
    for _, l in lines:
        if needle in l and l.startswith("["):
            return float(l[1:9])
    return None


def smoke(config, n, tag):
    results = []
    for i in range(n):
        ollama_unload_all()
        v0 = vram_used()
        lines = []
        log = open(os.path.join(OUT, f"log_{tag}_smoke{i}.txt"), "w", encoding="utf-8")
        t0 = time.time()
        p = launch("smoke", config)
        ps = psutil.Process(p.pid)
        th = threading.Thread(target=reader, args=(p, lines, log), daemon=True)
        th.start()
        peak_rss = peak_vram = 0
        while p.poll() is None:
            r, _, _, _ = tree_stats(ps) if ps.is_running() else (0, 0, 0, [])
            peak_rss = max(peak_rss, r)
            peak_vram = max(peak_vram, vram_used())
            time.sleep(0.3)
        th.join(5)
        wall = time.time() - t0
        res = {"run": i, "wall_exit_s": round(wall, 2), "rc": p.returncode,
               "imported_s": stamp_of(lines, "DAEMON_IMPORTED"),
               "daemon_started_s": stamp_of(lines, "Daemon started"),
               "tts_started_s": stamp_of(lines, "TTS engine"),
               "init_ok_s": stamp_of(lines, "SMOKE_TEST_INIT_OK"),
               "returned_s": stamp_of(lines, "DAEMON_MAIN_RETURNED"),
               "peak_rss_mb": peak_rss // 2**20, "vram_before": v0, "peak_vram": peak_vram,
               "ollama_after": ollama_ps()}
        print(json.dumps(res), flush=True)
        results.append(res)
    json.dump(results, open(os.path.join(OUT, f"res_{tag}_smoke.json"), "w"), indent=1)


def run(config, tag, idle_sec, unload=True):
    if unload:
        ollama_unload_all()
    v0 = vram_used()
    lines = []
    log = open(os.path.join(OUT, f"log_{tag}_run.txt"), "w", encoding="utf-8")
    t0 = time.time()
    p = launch("run", config, ["--sample-threads", os.path.join(OUT, f"threads_{tag}.json")])
    ps = psutil.Process(p.pid)
    threading.Thread(target=reader, args=(p, lines, log), daemon=True).start()
    # wait for Listening!
    listen_t = None
    while p.poll() is None and time.time() - t0 < 240:
        if any("Listening!" in l for _, l in lines):
            listen_t = time.time() - t0
            break
        time.sleep(0.2)
    print(f"listening after {listen_t}", flush=True)
    time.sleep(30)  # settle (warmups, MCP)
    chat = os.environ.get("BENCH_CHAT")
    if chat:
        tq = time.time()
        p.stdin.write("__CHAT_QUERY__:" + json.dumps({"text": chat}) + "\n")
        p.stdin.flush()
        start_n = len(lines)
        while time.time() - tq < 240 and p.poll() is None:
            time.sleep(0.25)
            if any("__CHAT__" in l and '"complete"' in l for _, l in lines[start_n:]):
                break
        time.sleep(8)  # let post-reply work (memory, hot window) settle
        print(f"chat reply complete after {time.time() - tq - 8:.1f}s", flush=True)
    samples = []
    _, _, cpu_prev, _ = tree_stats(ps)
    tprev = time.time()
    end = time.time() + idle_sec
    while time.time() < end and p.poll() is None:
        time.sleep(5)
        rss, nth, cpu, names = tree_stats(ps)
        now = time.time()
        samples.append({"t": round(now - t0, 1), "rss_mb": rss // 2**20, "threads": nth,
                        "cpu_pct_of_one_core": round(100 * (cpu - cpu_prev) / (now - tprev), 2),
                        "vram": vram_used(), "ollama": ollama_ps()})
        cpu_prev, tprev = cpu, now
    own_rss = ps.memory_info().rss // 2**20
    children = [(c.name(), c.memory_info().rss // 2**20) for c in ps.children(recursive=True) if c.is_running()]
    om = ollama_ps()
    gapps = gpu_apps()
    # shutdown like the desktop app: close stdin
    stop_t = time.time()
    n_before = len(lines)
    p.stdin.close()
    try:
        p.wait(timeout=180)
    except subprocess.TimeoutExpired:
        p.kill()
    stop_wall = time.time() - stop_t
    time.sleep(1)
    res = {"listening_s": listen_t, "own_rss_mb_end": own_rss, "children": children,
           "ollama": om, "gpu_apps": gapps, "vram_before": v0, "samples": samples,
           "shutdown_wall_s": round(stop_wall, 2), "rc": p.returncode,
           "shutdown_lines": [f"{ts - stop_t:7.3f} {l}" for ts, l in lines[n_before:]],
           "startup_lines": [l for _, l in lines[:n_before] if l.startswith("[")][:200]}
    json.dump(res, open(os.path.join(OUT, f"res_{tag}_run.json"), "w", encoding="utf-8"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k not in ("samples", "startup_lines")}, indent=1), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "smoke":
        smoke(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        run(sys.argv[2], sys.argv[3], float(sys.argv[4]), unload=(len(sys.argv) < 6 or sys.argv[5] != "warm"))

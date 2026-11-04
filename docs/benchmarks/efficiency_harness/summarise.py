import glob
import json
import re
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")


def stamp(lines, needle):
    for l in lines:
        if needle in l:
            m = re.match(r"\[\s*([\d.]+)\]", l)
            if m:
                return float(m.group(1))
    return None


for prefix in sys.argv[1:]:
    rows = []
    for f in sorted(glob.glob(f"res_{prefix}*_run.json")):
        r = json.load(open(f, encoding="utf-8"))
        L = r["startup_lines"]
        sh = r["shutdown_lines"]
        rows.append({
            "file": f,
            "imported": stamp(L, "DAEMON_IMPORTED"),
            "tts_ready": stamp(L, "TTS engine started"),
            "whisper_loading": stamp(L, "Loading Whisper"),
            "whisper_loaded": stamp(L, "loaded on"),
            "listening": stamp(L, "Listening!"),
            "listening_wall": round(r["listening_s"], 2) if r["listening_s"] else None,
            "shutdown_wall": r["shutdown_wall_s"],
            "rss_mb": st.median([s["rss_mb"] for s in r["samples"]]) if r["samples"] else None,
            "own_rss_end": r["own_rss_mb_end"],
            "children": r["children"],
            "threads": st.median([s["threads"] for s in r["samples"]]) if r["samples"] else None,
            "cpu": st.median([s["cpu_pct_of_one_core"] for s in r["samples"]]) if r["samples"] else None,
            "vram": st.median([s["vram"] for s in r["samples"]]) if r["samples"] else None,
            "vram_before": r["vram_before"],
            "ollama_last": r["samples"][-1].get("ollama") if r["samples"] else None,
            "shutdown_lines": sh,
        })
    for row in rows:
        print(json.dumps({k: v for k, v in row.items() if k != "shutdown_lines"}, ensure_ascii=False))
    for key in ("listening", "listening_wall", "shutdown_wall", "rss_mb", "cpu", "vram"):
        vals = [r[key] for r in rows if r[key] is not None]
        if vals:
            print(f"  {prefix} {key}: median {st.median(vals)}  (n={len(vals)}, min {min(vals)}, max {max(vals)})")
    if rows:
        print("  shutdown lines of first run:")
        for l in rows[0]["shutdown_lines"]:
            print("   ", l)

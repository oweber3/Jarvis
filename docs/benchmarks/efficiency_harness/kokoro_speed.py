"""Kokoro synthesis speed per device: time to first audio and real-time factor for one reply-length text."""
import sys, time, json
import numpy as np
from jarvis.output.kokoro import load_kokoro_voice
dev = sys.argv[1]
v = load_kokoro_voice(voice="am_michael", device=dev)
list(v.synthesize("Warm up."))
text = "Did you know an octopus has three hearts? Two pump blood through the gills, and the third sends it round the rest of the body."
for _ in range(3):
    t = time.time(); first = None; samples = 0
    for chunk in v.synthesize(text):
        if first is None: first = time.time() - t
        a = getattr(chunk, "audio", None)
        if a is None: a = getattr(chunk, "samples", None)
        samples += len(a) if a is not None else 0
    wall = time.time() - t
    print(json.dumps({"device": dev, "first_audio_s": round(first, 3), "total_s": round(wall, 3), "audio_s": round(samples / 24000, 2)}))

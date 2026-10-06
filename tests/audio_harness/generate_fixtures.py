"""Regenerate the committed WAV fixtures with Piper.

    PYTHONPATH=src python -m tests.audio_harness.generate_fixtures [--voices-dir DIR]

Voices are Piper ``.onnx`` models (with their ``.onnx.json``) in
``.tmp/wake-word/voices`` unless ``--voices-dir`` or
``JARVIS_HARNESS_VOICES`` says otherwise. Audio goes to WAV files only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .audio import pad, save_wav
from .piper_voices import Voice, synthesise

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# (kind, text, voices). Kinds: "wake" (the bare wake word), "wake_command"
# (wake word and a request in one breath), "near_miss" (sounds like the wake
# word but is not), "speech" (ordinary speech without the wake word).
VOICES = {
    "alan": Voice("en_GB-alan-medium"),
    "alba": Voice("en_GB-alba-medium"),
    "lessac": Voice("en_US-lessac-medium"),
    "ryan": Voice("en_US-ryan-medium"),
    "libri12": Voice("en_US-libritts_r-medium", 12),
    "libri305": Voice("en_US-libritts_r-medium", 305),
}

CORPUS = [
    ("wake", "Jarvis.", ["alan", "alba", "lessac", "ryan", "libri12", "libri305"]),
    ("wake_command", "Jarvis, open Word.", ["alan", "lessac", "libri12"]),
    ("wake_command", "Jarvis, what time is it?", ["alba", "ryan"]),
    ("near_miss", "Travis.", ["alan", "lessac"]),
    ("near_miss", "Service.", ["alba", "ryan"]),
    ("near_miss", "A jar of this.", ["alan", "libri305"]),
    ("near_miss", "Davis.", ["lessac"]),
    ("speech", "I think we should leave for the station in about ten minutes.", ["alan"]),
    ("speech", "Can you pass me the salt and the pepper, please?", ["lessac"]),
    ("speech", "The meeting has been moved to Thursday afternoon.", ["libri12"]),
]


def _slug(text: str) -> str:
    return "_".join("".join(c for c in text.lower() if c.isalnum() or c == " ").split())


def generate(voices_dir: Path | None = None, out_dir: Path = FIXTURES_DIR) -> dict:
    manifest: dict[str, dict] = {}
    for kind, text, voice_names in CORPUS:
        for name in voice_names:
            voice = VOICES[name]
            audio = pad(synthesise(text, voice, voices_dir=voices_dir), before=0.1, after=0.1)
            filename = f"{kind}__{_slug(text)}__{name}.wav"
            save_wav(out_dir / filename, audio)
            manifest[filename] = {"kind": kind, "text": text, "voice": voice.label}
            print(f"  🎙️ {filename}")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voices-dir", type=Path, default=None)
    args = parser.parse_args()
    print("🧪 Generating harness fixtures")
    manifest = generate(args.voices_dir)
    print(f"✅ {len(manifest)} fixtures written to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()

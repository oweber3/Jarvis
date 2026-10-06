#!/usr/bin/env python3
"""Enrol your voice for speaker verification and barge-in.

    python scripts/enrol_voice.py            # read six prompted phrases into the microphone
    python scripts/enrol_voice.py --wav a.wav b.wav c.wav
    python scripts/enrol_voice.py --status
    python scripts/enrol_voice.py --delete

The voiceprint is stored beside your config file only, never logged and never
sent to any model or reply mode. Delete it with --delete or from Settings.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis.listening.enrolment import main  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

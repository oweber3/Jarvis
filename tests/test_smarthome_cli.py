"""``python -m jarvis.smarthome`` prints its emoji lines on a Windows console that is not UTF-8."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.mark.unit
@pytest.mark.parametrize("command", ["test", "devices"])
def test_the_command_line_runs_on_a_legacy_code_page_console(tmp_path, command):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"smarthome_enabled": False}), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SRC, "JARVIS_CONFIG_PATH": str(config), "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run([sys.executable, "-m", "jarvis.smarthome", command], env=env, capture_output=True,
                          timeout=60)
    assert b"Traceback" not in done.stderr, done.stderr.decode("utf-8", "replace")
    assert "\U0001f3e0".encode("utf-8") in done.stdout

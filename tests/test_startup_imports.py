"""Start-up stays light: packages that only some setups use are imported when first needed.

Each check runs a fresh interpreter, so what earlier tests imported cannot hide an eager import.
See scripts/profile_resources.py for the timings these protect."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _loaded_after_import(module: str, packages: list) -> dict:
    code = (f"import sys, {module}; "
            f"print(','.join(p for p in {packages!r} if p in sys.modules))")
    env = {"PYTHONPATH": str(SRC), "PATH": ""}
    # Windows needs these to initialise ssl and sockets and to find the home folder.
    for name in ("SYSTEMROOT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        if name in os.environ:
            env[name] = os.environ[name]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         env=env, cwd=str(SRC.parent))
    assert out.returncode == 0, out.stderr
    return set(filter(None, out.stdout.strip().split(",")))


@pytest.mark.unit
def test_the_mcp_client_is_not_imported_until_an_mcp_server_is_used():
    assert _loaded_after_import("jarvis.daemon", ["mcp"]) == set()


@pytest.mark.unit
def test_whisper_is_imported_when_the_model_loads_not_when_the_daemon_starts():
    assert _loaded_after_import("jarvis.daemon", ["faster_whisper", "ctranslate2"]) == set()

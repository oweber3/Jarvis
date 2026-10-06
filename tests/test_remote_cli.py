"""``python -m jarvis.remote``: pair, list and remove phones from a terminal, on any console encoding."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.remote.pairing import DeviceStore

SRC = Path(__file__).resolve().parents[1] / "src"


def run(tmp_path, *args):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db")}))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(JARVIS_CONFIG_PATH=str(config), PYTHONPATH=str(SRC), PYTHONUTF8="0")
    out = subprocess.run([sys.executable, "-m", "jarvis.remote", *args], env=env, capture_output=True,
                         timeout=60)
    return out.returncode, out.stdout.decode("utf-8", errors="replace")


@pytest.mark.integration
class TestRemoteCli:
    def test_pair_prints_a_code_that_pairs(self, tmp_path):
        code, out = run(tmp_path, "pair")
        assert code == 0
        line = next(line for line in out.splitlines() if "Pairing code" in line)
        pairing_code = line.rsplit(" ", 1)[-1]
        assert DeviceStore(tmp_path).complete_pairing(pairing_code, "phone") is not None

    def test_devices_lists_and_revoke_removes(self, tmp_path):
        store = DeviceStore(tmp_path)
        result = store.complete_pairing(store.start_pairing(), "Alex's iPhone")
        code, out = run(tmp_path, "devices")
        assert code == 0 and "Alex's iPhone" in out and "📱" in out
        code, _ = run(tmp_path, "revoke", result.device_id)
        assert code == 0 and DeviceStore(tmp_path).devices() == []

    def test_an_unknown_command_shows_usage(self, tmp_path):
        code, out = run(tmp_path, "nonsense")
        assert code == 2 and "Usage" in out

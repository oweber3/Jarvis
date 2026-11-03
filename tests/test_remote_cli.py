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


@pytest.mark.unit
class TestCertCommand:
    """``cert`` run in-process with a fake Tailscale, so nothing is requested from the real one."""

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db"), "remote_access_port": 9100}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        return path

    def issued(self, tmp_path):
        from jarvis.remote.tailscale import IssuedCertificate
        return IssuedCertificate("pc.tail1234.ts.net", tmp_path / "remote_access.crt", tmp_path / "remote_access.key")

    def test_it_saves_the_three_settings_and_prints_the_https_address(self, tmp_path, config, capsys):
        from jarvis.remote.__main__ import main
        assert main(["cert"], issue=lambda folder: self.issued(tmp_path)) == 0
        out = capsys.readouterr().out
        stored = json.loads(config.read_text())
        assert stored["remote_access_tls_host"] == "pc.tail1234.ts.net"
        assert stored["remote_access_tls_cert"].endswith("remote_access.crt")
        assert "https://pc.tail1234.ts.net:9100/" in out and "90 days" in out

    def test_it_never_prints_the_key_or_its_path_contents(self, tmp_path, config, capsys):
        from jarvis.remote.__main__ import main
        (tmp_path / "remote_access.key").write_text("-----BEGIN PRIVATE KEY-----\nSECRETBYTES\n")
        main(["cert"], issue=lambda folder: self.issued(tmp_path))
        assert "SECRETBYTES" not in capsys.readouterr().out

    def test_a_problem_is_explained_with_the_steps_to_take_and_changes_nothing(self, tmp_path, config, capsys):
        from jarvis.remote.__main__ import main
        from jarvis.remote.tailscale import CertProblem

        def refuse(folder):
            raise CertProblem("HTTPS certificates are not enabled for your tailnet.", "Press Enable HTTPS.")

        assert main(["cert"], issue=refuse) == 1
        out = capsys.readouterr().out
        assert "❌" in out and "Press Enable HTTPS." in out
        assert not [key for key in json.loads(config.read_text()) if key.startswith("remote_access_tls")]

    def test_the_certificate_lands_beside_the_config_file(self, tmp_path, config):
        from jarvis.remote.__main__ import main
        seen = []
        main(["cert"], issue=lambda folder: seen.append(folder) or self.issued(tmp_path))
        assert seen == [config.parent]

    def test_pair_shows_only_the_https_address_once_a_certificate_is_set(self, tmp_path, config, capsys):
        from jarvis.remote.__main__ import main
        config.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db"), "remote_access_port": 9100,
                                      "remote_access_tls_cert": "a.crt", "remote_access_tls_key": "a.key",
                                      "remote_access_tls_host": "pc.tail1234.ts.net"}))
        assert main(["pair"]) == 0
        out = capsys.readouterr().out
        assert "https://pc.tail1234.ts.net:9100/" in out and "http://" not in out

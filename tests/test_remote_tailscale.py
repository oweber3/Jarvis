"""``python -m jarvis.remote cert``: a certificate from the local Tailscale, with a fake ``tailscale`` program."""
import json
from pathlib import Path

import pytest

from jarvis.config import read_config_file
from jarvis.remote import tailscale
from jarvis.remote.tailscale import CertProblem, find_tailscale, issue_certificate

NAME = "pc.tail1234.ts.net"


def status(**overrides):
    document = {"BackendState": "Running", "Self": {"DNSName": NAME + "."}, "CertDomains": [NAME]}
    document.update(overrides)
    return json.dumps(document)


class FakeTailscale:
    """Answers ``status --json`` and ``cert`` the way the real program does."""

    def __init__(self, status_text=None, status_code=0, cert_code=0, cert_message=""):
        self.status_text = status() if status_text is None else status_text
        self.status_code, self.cert_code, self.cert_message = status_code, cert_code, cert_message
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[1] == "status":
            return self.status_code, self.status_text, ""
        if args[1] == "cert":
            if self.cert_code == 0:
                Path(args[args.index("--cert-file") + 1]).write_text("CERT")
                Path(args[args.index("--key-file") + 1]).write_text("KEY")
            return self.cert_code, "", self.cert_message
        raise AssertionError(args)


def issue(tmp_path, fake, program="tailscale"):
    return issue_certificate(tmp_path, run=fake, find=lambda: program)


@pytest.mark.unit
class TestFindTailscale:
    def test_the_program_on_the_path_wins(self):
        assert find_tailscale(which=lambda name: "C:/tools/tailscale.exe", exists=lambda p: True) == "C:/tools/tailscale.exe"

    def test_the_installed_location_is_used_when_not_on_the_path(self):
        found = find_tailscale(which=lambda name: None, exists=lambda p: p.endswith("tailscale.exe"))
        assert found is not None and found.endswith("tailscale.exe")

    def test_nothing_found_is_none(self):
        assert find_tailscale(which=lambda name: None, exists=lambda p: False) is None


@pytest.mark.unit
class TestIssueCertificate:
    def test_it_gets_the_name_from_tailscale_and_writes_both_files(self, tmp_path):
        fake = FakeTailscale()
        issued = issue(tmp_path, fake)
        assert issued.host == NAME
        assert issued.cert == tmp_path / "remote_access.crt" and issued.key == tmp_path / "remote_access.key"
        assert issued.cert.read_text() == "CERT" and issued.key.read_text() == "KEY"
        cert_call = next(call for call in fake.calls if call[1] == "cert")
        assert cert_call[-1] == NAME and str(issued.cert) in cert_call and str(issued.key) in cert_call

    def test_tailscale_not_installed_says_how_to_get_it(self, tmp_path):
        with pytest.raises(CertProblem) as problem:
            issue_certificate(tmp_path, run=FakeTailscale(), find=lambda: None)
        assert "not installed" in problem.value.lines[0] and "tailscale.com/download" in " ".join(problem.value.lines)

    def test_signed_out_says_to_start_and_sign_in(self, tmp_path):
        with pytest.raises(CertProblem) as problem:
            issue(tmp_path, FakeTailscale(status(BackendState="NeedsLogin")))
        assert "NeedsLogin" in " ".join(problem.value.lines) and "sign in" in " ".join(problem.value.lines)

    def test_a_status_that_fails_or_is_not_json_says_tailscale_did_not_answer(self, tmp_path):
        for fake in (FakeTailscale(status_code=1), FakeTailscale("not json")):
            with pytest.raises(CertProblem) as problem:
                issue(tmp_path, fake)
            assert "did not answer" in problem.value.lines[0]

    def test_https_certificates_not_enabled_says_exactly_what_to_press(self, tmp_path):
        with pytest.raises(CertProblem) as problem:
            issue(tmp_path, FakeTailscale(status(CertDomains=None)))
        text = " ".join(problem.value.lines)
        assert "admin console" in text and "HTTPS Certificates" in text and "Enable HTTPS" in text

    def test_a_pc_without_a_tailscale_name_says_to_turn_on_magicdns(self, tmp_path):
        with pytest.raises(CertProblem) as problem:
            issue(tmp_path, FakeTailscale(status(Self={"DNSName": ""})))
        assert "MagicDNS" in " ".join(problem.value.lines)

    def test_a_failing_cert_command_reports_tailscales_own_message(self, tmp_path):
        with pytest.raises(CertProblem) as problem:
            issue(tmp_path, FakeTailscale(cert_code=1, cert_message="rate limited\nretry later"))
        assert "rate limited" in " ".join(problem.value.lines)

    def test_no_files_after_a_cert_command_that_claims_success_is_a_failure(self, tmp_path):
        class Silent(FakeTailscale):
            def __call__(self, args, timeout):
                return (0, self.status_text, "") if args[1] == "status" else (0, "", "")

        with pytest.raises(CertProblem):
            issue(tmp_path, Silent())


@pytest.mark.unit
class TestRemember:
    def test_the_three_keys_are_written_and_nothing_else_changes(self, tmp_path, monkeypatch):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"voice_name": "kept"}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config))
        issued = issue(tmp_path, FakeTailscale())
        assert tailscale.remember(issued)
        stored = read_config_file()
        assert stored["voice_name"] == "kept"
        assert stored["remote_access_tls_cert"] == str(issued.cert)
        assert stored["remote_access_tls_key"] == str(issued.key)
        assert stored["remote_access_tls_host"] == NAME

"""Voice files download only from pinned addresses and are kept only when their size and digest match."""
import hashlib
from unittest.mock import MagicMock

import pytest


def _response(payload: bytes, *, length=True):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-length": str(len(payload))} if length else {}
    response.iter_content.side_effect = lambda **kw: iter([payload[i:i + 7] for i in range(0, len(payload), 7)])
    return response


@pytest.fixture
def served(monkeypatch):
    """Serves ``payloads[url]`` for each requested address and records the requests."""
    payloads = {}
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        return _response(payloads[url])

    monkeypatch.setattr("requests.get", get)
    return payloads, requested


@pytest.mark.unit
@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_a_file_matching_its_digest_is_kept(tmp_path, served, algorithm):
    from jarvis.output.model_download import download_verified

    payloads, _ = served
    payloads["https://example.test/voice.onnx"] = b"voice weights"
    target = tmp_path / "voice.onnx"
    ok = download_verified("https://example.test/voice.onnx", target, size=13,
                           digest=hashlib.new(algorithm, b"voice weights").hexdigest(), algorithm=algorithm,
                           label="Test voice")
    assert ok and target.read_bytes() == b"voice weights"


@pytest.mark.unit
@pytest.mark.parametrize("size_delta, digest_ok", [(0, False), (1, True)])
def test_a_file_with_the_wrong_digest_or_size_is_discarded(tmp_path, served, size_delta, digest_ok, capsys):
    from jarvis.output.model_download import download_verified

    payloads, _ = served
    payloads["https://example.test/voice.onnx"] = b"tampered weights"
    target = tmp_path / "voice.onnx"
    digest = hashlib.sha256(b"tampered weights" if digest_ok else b"genuine weights").hexdigest()
    ok = download_verified("https://example.test/voice.onnx", target, size=16 + size_delta, digest=digest,
                           algorithm="sha256", label="Test voice", progress=print)
    assert not ok
    assert list(tmp_path.iterdir()) == [], "neither the file nor a partial download may stay behind"
    assert "check" in capsys.readouterr().out.lower()


@pytest.mark.unit
def test_an_existing_verified_file_is_not_downloaded_again(tmp_path, served):
    from jarvis.output.model_download import download_verified

    _, requested = served
    target = tmp_path / "voice.onnx"
    target.write_bytes(b"voice weights")
    assert download_verified("https://example.test/voice.onnx", target, size=13,
                             digest=hashlib.sha256(b"voice weights").hexdigest(), algorithm="sha256", label="Test")
    assert requested == []


@pytest.mark.unit
def test_the_piper_catalogue_offers_english_voices_with_digests_for_both_files():
    from jarvis.output.piper_voices import PIPER_DEFAULT_VOICE, piper_voices

    voices = piper_voices()
    assert PIPER_DEFAULT_VOICE in voices
    assert all(v.language.startswith("en_") for v in voices.values())
    for voice in voices.values():
        names = sorted(f.name for f in voice.files)
        assert names == [f"{voice.key}.onnx", f"{voice.key}.onnx.json"]
        assert all(len(f.md5) == 32 and f.size > 0 for f in voice.files)
        assert voice.label and voice.download_mb > 0


@pytest.mark.unit
def test_a_piper_voice_downloads_from_the_pinned_catalogue_and_is_verified(tmp_path, monkeypatch, served):
    from jarvis.output import piper_voices
    from jarvis.output.tts import _download_piper_voice

    payloads, requested = served
    voice = piper_voices.piper_voices()["en_US-lessac-medium"]
    fake = {}
    for f in voice.files:
        body = f"contents of {f.name}".encode()
        payloads[f.url] = body
        fake[f.name] = (len(body), hashlib.md5(body).hexdigest())
    real_checks = piper_voices._file_checks
    monkeypatch.setattr(piper_voices, "_file_checks",
                        lambda key: fake if key == "en_US-lessac-medium" else real_checks(key))
    monkeypatch.setattr("jarvis.output.tts._get_piper_models_dir", lambda: tmp_path)

    path = _download_piper_voice("en_US-lessac-medium")

    assert path == str(tmp_path / "en_US-lessac-medium.onnx")
    assert sorted(requested) == sorted(f.url for f in voice.files)
    assert all(url.startswith(piper_voices.PIPER_VOICE_BASE_URL + "/") for url in requested)
    assert (tmp_path / "en_US-lessac-medium.onnx.json").exists()


@pytest.mark.unit
def test_a_voice_outside_the_catalogue_is_never_fetched(tmp_path, monkeypatch, served, capsys):
    from jarvis.output.tts import _download_piper_voice

    _, requested = served
    monkeypatch.setattr("jarvis.output.tts._get_piper_models_dir", lambda: tmp_path)
    assert _download_piper_voice("xx_XX-nobody-medium", print) is None
    assert requested == []
    assert "catalogue" in capsys.readouterr().out.lower()


@pytest.mark.unit
def test_downloaded_state_follows_the_files_on_disk(tmp_path):
    from jarvis.output.piper_voices import piper_voice_downloaded

    assert not piper_voice_downloaded("en_GB-alan-medium", tmp_path)
    (tmp_path / "en_GB-alan-medium.onnx").write_bytes(b"x")
    assert not piper_voice_downloaded("en_GB-alan-medium", tmp_path)
    (tmp_path / "en_GB-alan-medium.onnx.json").write_text("{}")
    assert piper_voice_downloaded("en_GB-alan-medium", tmp_path)


@pytest.mark.unit
def test_a_failing_progress_callback_never_leaves_a_partial_file(tmp_path, served):
    from jarvis.output.model_download import download_verified

    payloads, _ = served
    payloads["https://example.test/voice.onnx"] = b"voice weights"

    def closed_window(message):
        raise RuntimeError("wrapped C/C++ object has been deleted")

    target = tmp_path / "voice.onnx"
    ok = download_verified("https://example.test/voice.onnx", target, size=13,
                           digest=hashlib.sha256(b"voice weights").hexdigest(), algorithm="sha256",
                           label="Test voice", progress=closed_window)
    assert ok and target.read_bytes() == b"voice weights"
    assert [p.name for p in tmp_path.iterdir()] == ["voice.onnx"]


@pytest.mark.unit
def test_an_unexpected_error_leaves_no_partial_file(tmp_path, monkeypatch):
    from jarvis.output.model_download import download_verified

    response = _response(b"voice weights")
    response.headers = {"content-length": "not a number"}
    monkeypatch.setattr("requests.get", lambda url, **kw: response)
    target = tmp_path / "voice.onnx"
    assert not download_verified("https://example.test/voice.onnx", target, size=13,
                                 digest=hashlib.sha256(b"voice weights").hexdigest(), algorithm="sha256",
                                 label="Test voice")
    assert list(tmp_path.iterdir()) == []

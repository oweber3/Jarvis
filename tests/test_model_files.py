"""Model files: fetched once, verified against a pinned SHA-256, never left half-written."""
import hashlib

import pytest

from jarvis.utils import model_files
from jarvis.utils.model_files import ModelUnavailable, ensure_verified_file

DATA = b"pretend-model" * 200
SHA = hashlib.sha256(DATA).hexdigest()
URL = "https://example.invalid/model.bin"


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    directory = tmp_path / "models"
    monkeypatch.setattr(model_files, "models_dir", lambda: directory)
    return directory


def _ensure(fetch):
    return ensure_verified_file("model.bin", URL, SHA, "the test model", fetch=fetch)


@pytest.mark.unit
class TestEnsureVerifiedFile:
    def test_a_missing_file_is_fetched_verified_and_saved(self, models_dir):
        fetched = []
        path = _ensure(lambda url: fetched.append(url) or DATA)
        assert path == models_dir / "model.bin"
        assert path.read_bytes() == DATA
        assert fetched == [URL]

    def test_a_good_file_is_not_fetched_again(self, models_dir):
        models_dir.mkdir()
        (models_dir / "model.bin").write_bytes(DATA)

        def fetch(url):
            raise AssertionError("must not download")

        assert _ensure(fetch).read_bytes() == DATA

    def test_a_corrupt_file_is_replaced(self, models_dir):
        models_dir.mkdir()
        (models_dir / "model.bin").write_bytes(b"truncated")
        assert _ensure(lambda url: DATA).read_bytes() == DATA

    def test_a_download_that_fails_the_checksum_is_refused_and_nothing_is_saved(self, models_dir):
        with pytest.raises(ModelUnavailable, match="the test model.*checksum"):
            _ensure(lambda url: b"something else")
        assert not models_dir.exists() or list(models_dir.iterdir()) == []

    def test_a_failed_download_names_the_model_and_the_reason(self, models_dir):
        def fetch(url):
            raise OSError("offline")

        with pytest.raises(ModelUnavailable, match="the test model.*offline"):
            _ensure(fetch)

"""The phone access server certificate: loaded and checked at start, reloaded when the files change."""
import os
import ssl
import time
from types import SimpleNamespace

import pytest

from jarvis.remote.tls import ReloadingContext, TLSError, tls_config
from tls_fixtures import make_certificate


@pytest.mark.unit
class TestTlsConfig:
    def test_both_paths_make_a_configuration(self):
        cfg = SimpleNamespace(remote_access_tls_cert="a.crt", remote_access_tls_key="a.key")
        config = tls_config(cfg)
        assert (config.cert, config.key) == ("a.crt", "a.key")

    @pytest.mark.parametrize("cert,key", [("", ""), ("a.crt", ""), ("", "a.key")])
    def test_either_path_missing_means_plain_http(self, cert, key):
        assert tls_config(SimpleNamespace(remote_access_tls_cert=cert, remote_access_tls_key=key)) is None

    def test_a_settings_object_without_the_keys_means_plain_http(self):
        assert tls_config(SimpleNamespace()) is None


@pytest.mark.unit
class TestReloadingContext:
    def test_a_good_pair_gives_a_server_context(self, tmp_path):
        cert, key = make_certificate(tmp_path)
        context = ReloadingContext(str(cert), str(key)).context()
        assert isinstance(context, ssl.SSLContext)
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

    def test_a_missing_certificate_says_so(self, tmp_path):
        _, key = make_certificate(tmp_path)
        with pytest.raises(TLSError, match="certificate file"):
            ReloadingContext(str(tmp_path / "gone.crt"), str(key))

    def test_a_missing_key_says_so(self, tmp_path):
        cert, _ = make_certificate(tmp_path)
        with pytest.raises(TLSError, match="key file"):
            ReloadingContext(str(cert), str(tmp_path / "gone.key"))

    def test_a_file_that_is_not_pem_is_refused(self, tmp_path):
        cert, key = make_certificate(tmp_path)
        cert.write_text("not a certificate")
        with pytest.raises(TLSError, match="could not be loaded"):
            ReloadingContext(str(cert), str(key))

    def test_a_key_that_does_not_match_is_refused(self, tmp_path):
        cert, _ = make_certificate(tmp_path, "one")
        _, other_key = make_certificate(tmp_path, "two")
        with pytest.raises(TLSError, match="could not be loaded"):
            ReloadingContext(str(cert), str(other_key))

    def test_the_error_never_contains_the_key(self, tmp_path):
        cert, key = make_certificate(tmp_path)
        key_text = key.read_text()
        cert.write_text("junk")
        with pytest.raises(TLSError) as error:
            ReloadingContext(str(cert), str(key))
        assert key_text.splitlines()[1] not in str(error.value)

    def test_unchanged_files_keep_the_same_context(self, tmp_path):
        cert, key = make_certificate(tmp_path)
        holder = ReloadingContext(str(cert), str(key))
        assert holder.context() is holder.context()

    def test_renewed_files_give_a_new_context(self, tmp_path):
        cert, key = make_certificate(tmp_path, "old")
        holder = ReloadingContext(str(cert), str(key))
        first = holder.context()
        new_cert, new_key = make_certificate(tmp_path, "new")
        cert.write_bytes(new_cert.read_bytes())
        key.write_bytes(new_key.read_bytes())
        later = time.time() + 5
        os.utime(cert, (later, later))
        os.utime(key, (later, later))
        assert holder.context() is not first

    def test_a_broken_renewal_keeps_serving_with_the_old_pair(self, tmp_path):
        cert, key = make_certificate(tmp_path)
        holder = ReloadingContext(str(cert), str(key))
        first = holder.context()
        cert.write_text("half written")
        later = time.time() + 5
        os.utime(cert, (later, later))
        assert holder.context() is first


@pytest.mark.unit
def test_a_key_that_needs_a_passphrase_is_refused_instead_of_asking_on_the_console(tmp_path):
    from tls_fixtures import make_encrypted_certificate
    cert, key = make_encrypted_certificate(tmp_path)
    started = time.monotonic()
    with pytest.raises(TLSError, match="could not be loaded"):
        ReloadingContext(str(cert), str(key))
    assert time.monotonic() - started < 5   # never waits for someone to type at a console

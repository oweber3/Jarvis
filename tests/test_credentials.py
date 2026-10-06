"""API keys live in the OS credential store, never in config.json; plaintext keys on disk are moved there."""
import json

import pytest

from jarvis import credentials as secrets
from jarvis.config import load_config, load_settings

KEYS = ("llm_api_key", "embedding_api_key", "brave_search_api_key")


class MemoryStore:
    """A credential store with the keyring API."""

    def __init__(self, fail=False):
        self.items = {}
        self.fail = fail

    def get_password(self, service, username):
        if self.fail:
            raise RuntimeError("no store")
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        if self.fail:
            raise RuntimeError("no store")
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if self.fail:
            raise RuntimeError("no store")
        if (service, username) not in self.items:
            raise KeyError(username)
        del self.items[(service, username)]


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore()
    monkeypatch.setattr(secrets, "_backend", s)
    return s


def write(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return path


@pytest.mark.unit
class TestStore:
    def test_set_get_has_and_delete(self, store):
        assert secrets.get_secret("llm_api_key") == "" and not secrets.has_secret("llm_api_key")
        assert secrets.set_secret("llm_api_key", "sk-123")
        assert secrets.get_secret("llm_api_key") == "sk-123" and secrets.has_secret("llm_api_key")
        assert store.items == {("jarvis", "llm_api_key"): "sk-123"}
        assert secrets.delete_secret("llm_api_key") and secrets.get_secret("llm_api_key") == ""
        assert secrets.delete_secret("llm_api_key")  # removing an absent key is fine

    def test_only_known_secret_names_are_accepted(self, store):
        with pytest.raises(ValueError):
            secrets.set_secret("reply_mode", "x")

    def test_an_unavailable_store_reads_empty_and_reports_failed_writes(self, monkeypatch):
        monkeypatch.setattr(secrets, "_backend", MemoryStore(fail=True))
        assert secrets.get_secret("llm_api_key") == ""
        assert secrets.set_secret("llm_api_key", "sk") is False
        assert secrets.available() is False


@pytest.mark.unit
class TestDropStoredPlaintext:
    """Writers of config.json remove a plaintext key only once the store holds it."""

    def test_a_key_the_store_holds_leaves_config(self, store):
        secrets.set_secret("llm_api_key", "sk-123")
        config = {"llm_api_key": "sk-123", "llm_chat_model": "m"}
        secrets.drop_stored_plaintext(config, "llm_api_key")
        assert config == {"llm_chat_model": "m"}

    def test_without_a_store_the_plaintext_key_stays(self, monkeypatch):
        monkeypatch.setattr(secrets, "_backend", MemoryStore(fail=True))
        config = {"brave_search_api_key": "plain"}
        secrets.drop_stored_plaintext(config, "brave_search_api_key")
        assert config == {"brave_search_api_key": "plain"}

    def test_an_empty_value_is_tidied_away(self, monkeypatch):
        monkeypatch.setattr(secrets, "_backend", MemoryStore(fail=True))
        config = {"embedding_api_key": " "}
        secrets.drop_stored_plaintext(config, "embedding_api_key")
        assert config == {}


@pytest.mark.unit
class TestMigration:
    def test_plaintext_keys_move_out_of_config_into_the_store(self, store, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 6, "llm_api_key": "sk-llm",
                                             "embedding_api_key": "sk-emb", "brave_search_api_key": "brave",
                                             "other": 1})
        cfg = load_settings()
        on_disk = json.loads(path.read_text())
        assert not any(k in on_disk for k in KEYS) and on_disk["other"] == 1
        assert {k: store.items[("jarvis", k)] for k in KEYS} == {
            "llm_api_key": "sk-llm", "embedding_api_key": "sk-emb", "brave_search_api_key": "brave"}
        assert (cfg.llm_api_key, cfg.embedding_api_key, cfg.brave_search_api_key) == ("sk-llm", "sk-emb", "brave")
        assert "sk-llm" not in path.read_text()

    def test_keys_already_in_the_store_are_read_without_any_config_value(self, store, tmp_path, monkeypatch):
        store.items[("jarvis", "brave_search_api_key")] = "brave"
        write(tmp_path, monkeypatch, {"_config_version": 6})
        assert load_settings().brave_search_api_key == "brave"
        assert load_config()["brave_search_api_key"] == ""

    def test_a_key_is_never_lost_when_the_store_is_unavailable(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(secrets, "_backend", MemoryStore(fail=True))
        path = write(tmp_path, monkeypatch, {"_config_version": 6, "llm_api_key": "sk-llm"})
        assert load_settings().llm_api_key == "sk-llm"
        assert json.loads(path.read_text())["llm_api_key"] == "sk-llm"
        out = capsys.readouterr().out
        assert "credential" in out.lower() and "sk-llm" not in out

    def test_the_missing_store_is_reported_once_per_run(self, tmp_path, monkeypatch, capsys):
        import jarvis.config as config
        monkeypatch.setattr(config, "_unstored_warned", set())
        monkeypatch.setattr(secrets, "_backend", MemoryStore(fail=True))
        write(tmp_path, monkeypatch, {"_config_version": 6, "llm_api_key": "sk-llm"})
        load_settings()
        assert "credential" in capsys.readouterr().out.lower()
        for _ in range(3):
            assert load_settings().llm_api_key == "sk-llm"
        assert "credential" not in capsys.readouterr().out.lower()

    def test_moving_is_idempotent_and_keeps_a_stored_key_when_config_is_blank(self, store, tmp_path, monkeypatch):
        store.items[("jarvis", "llm_api_key")] = "kept"
        path = write(tmp_path, monkeypatch, {"_config_version": 6, "llm_api_key": ""})
        assert load_settings().llm_api_key == "kept"
        load_settings()
        assert store.items[("jarvis", "llm_api_key")] == "kept" and "llm_api_key" not in json.loads(path.read_text())


@pytest.mark.unit
class TestNoSecretsInConfigWrites:
    def test_the_minimal_config_writer_refuses_secret_names(self, store, tmp_path, monkeypatch):
        from jarvis.config import update_config_values
        write(tmp_path, monkeypatch, {"_config_version": 6})
        with pytest.raises(ValueError):
            update_config_values({"llm_api_key": "sk"})

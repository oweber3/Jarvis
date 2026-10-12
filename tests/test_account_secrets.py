"""Calendar addresses, email passwords and sign-in tokens live in the credential store, per account."""
import pytest

from jarvis import credentials as secrets


class LimitedStore:
    """A keyring-like store that, like Windows Credential Manager, refuses long values."""

    LIMIT = 1280

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        if len(password) > self.LIMIT:
            raise OSError("value too long for the credential store")
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        del self.items[(service, username)]


@pytest.fixture
def store(monkeypatch):
    s = LimitedStore()
    monkeypatch.setattr(secrets, "_backend", s)
    return s


def test_each_account_has_its_own_secret(store):
    work = secrets.account_key("calendar_url", "Work")
    home = secrets.account_key("calendar_url", "Home")
    assert secrets.set_secret(work, "https://example.test/work.ics")
    assert secrets.set_secret(home, "https://example.test/home.ics")
    assert secrets.get_secret(work) == "https://example.test/work.ics"
    assert secrets.get_secret(home) == "https://example.test/home.ics"
    assert secrets.delete_secret(work) and secrets.get_secret(work) == "" and secrets.has_secret(home)


def test_a_value_longer_than_the_store_allows_is_kept_whole_and_removed_whole(store):
    key = secrets.account_key("email_graph_token", "Outlook.com")
    token = "x" * 4000 + "end"
    assert secrets.set_secret(key, token)
    assert secrets.get_secret(key) == token
    assert secrets.set_secret(key, "short")
    assert secrets.get_secret(key) == "short"
    assert secrets.delete_secret(key)
    assert store.items == {}


def test_only_known_account_secret_kinds_are_accepted(store):
    with pytest.raises(ValueError):
        secrets.account_key("anything", "Work")
    with pytest.raises(ValueError):
        secrets.get_secret("calendar_url")
    with pytest.raises(ValueError):
        secrets.account_key("calendar_url", "  ")

"""Calendar and email settings: off by default, entries validated, secrets never kept from config."""
import json

from jarvis.config import get_default_config, load_settings


def _load(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return load_settings()


def test_everything_is_off_and_unshared_by_default(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, {})
    assert (cfg.calendar_enabled, cfg.email_enabled) == (False, False)
    assert (cfg.calendar_share_with_cloud, cfg.email_share_with_cloud) == (False, False)
    assert cfg.calendar_sources == [] and cfg.email_accounts == []
    defaults = get_default_config()
    assert defaults["calendar_cache_minutes"] == cfg.calendar_cache_minutes


def test_entries_are_validated_and_only_known_keys_kept(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, {
        "calendar_enabled": True,
        "calendar_sources": [
            {"name": " Work ", "type": "ics", "url": "https://leaked.example.test/secret.ics"},
            {"name": "work", "type": "ics"},          # duplicate name
            {"name": "Desk", "type": "outlook"},
            {"name": "", "type": "ics"},
            {"name": "Odd", "type": "caldav"},
            "not an entry",
        ],
        "email_accounts": [
            {"name": "Gmail", "type": "imap", "host": "imap.gmail.com", "port": "993", "username": "me@gmail.test",
             "password": "should-not-stay"},
            {"name": "Live", "type": "graph", "login": "microsoft-1a2b3c4d", "user": "me@x.test", "token": "x"},
            {"name": "Bad port", "type": "imap", "host": "h", "port": 70000},
        ],
        "email_share_with_cloud": "true",
    })
    assert cfg.calendar_enabled is True
    assert cfg.calendar_sources == [{"name": "Work", "type": "ics"}, {"name": "Desk", "type": "outlook"}]
    assert cfg.email_accounts == [
        {"name": "Gmail", "type": "imap", "host": "imap.gmail.com", "port": 993, "username": "me@gmail.test"},
        {"name": "Live", "type": "graph", "login": "microsoft-1a2b3c4d", "user": "me@x.test"},
        {"name": "Bad port", "type": "imap", "host": "h"},
    ]
    assert cfg.email_share_with_cloud is False  # only a real true shares


def test_the_switches_sit_on_the_calendar_and_email_page_with_the_accounts():
    from desktop_app import settings_window
    fields = {field.key: field for field in settings_window.FIELD_METADATA}
    for key in ("calendar_enabled", "calendar_cache_minutes", "calendar_share_with_cloud",
                "email_enabled", "email_share_with_cloud"):
        assert fields[key].category == settings_window.ACCOUNTS_CATEGORY, key
    assert (settings_window.ACCOUNTS_CATEGORY, "Calendar & Email") in settings_window.CATEGORIES
    assert fields["calendar_share_with_cloud"].shown_when is not None
    # The OAuth client IDs are for whoever builds Jarvis, not a setting people change.
    assert not any(key.endswith(("_oauth_client_id", "_oauth_client_secret")) for key in fields)


def test_oauth_client_ids_default_to_the_built_in_ones_and_config_overrides_them(tmp_path, monkeypatch):
    from jarvis.accounts import clients
    monkeypatch.setitem(clients.BUILT_IN_OAUTH_CLIENTS, "microsoft_oauth_client_id", "built-in-id")
    defaults = get_default_config()
    for key in clients.BUILT_IN_OAUTH_CLIENTS:
        assert key in defaults
    cfg = _load(tmp_path, monkeypatch, {"google_oauth_client_id": "  my-own-id  "})
    from jarvis.accounts import oauth
    assert oauth.client_for("microsoft", cfg).client_id == "built-in-id"
    assert cfg.google_oauth_client_id == "my-own-id"
    assert oauth.client_for("google", cfg).client_id == "my-own-id"

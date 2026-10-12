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
            {"name": "Live", "type": "graph", "client_id": "abc", "tenant": "consumers", "token": "x"},
            {"name": "Bad port", "type": "imap", "host": "h", "port": 70000},
        ],
        "email_share_with_cloud": "true",
    })
    assert cfg.calendar_enabled is True
    assert cfg.calendar_sources == [{"name": "Work", "type": "ics"}, {"name": "Desk", "type": "outlook"}]
    assert cfg.email_accounts == [
        {"name": "Gmail", "type": "imap", "host": "imap.gmail.com", "port": 993, "username": "me@gmail.test"},
        {"name": "Live", "type": "graph", "client_id": "abc", "tenant": "consumers"},
        {"name": "Bad port", "type": "imap", "host": "h"},
    ]
    assert cfg.email_share_with_cloud is False  # only a real true shares


def test_the_switches_are_in_settings_and_the_accounts_are_named_as_config_only():
    from desktop_app import settings_window
    fields = {field.key: field for field in settings_window.FIELD_METADATA}
    for key in ("calendar_enabled", "calendar_cache_minutes", "calendar_share_with_cloud",
                "email_enabled", "email_share_with_cloud"):
        assert key in fields, key
    # Accounts hold structured entries; the setup command adds them (with their secrets).
    assert "python -m jarvis.agenda setup" in fields["calendar_enabled"].description
    assert "python -m jarvis.mail setup" in fields["email_enabled"].description
    assert fields["calendar_share_with_cloud"].shown_when is not None

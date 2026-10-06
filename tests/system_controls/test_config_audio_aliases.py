"""windows_audio_aliases: safe default, validation, and preservation of the user's file."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(values or {}))
    monkeypatch.setenv('JARVIS_CONFIG_PATH', str(path))
    return load_settings()


def test_no_audio_aliases_by_default(tmp_path, monkeypatch):
    assert get_default_config()['windows_audio_aliases'] == {}
    assert load(tmp_path, monkeypatch).windows_audio_aliases == {}


def test_aliases_name_playback_devices(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, {'windows_audio_aliases': {'my headphones': 'Headset (Razer Kraken)'}})
    assert cfg.windows_audio_aliases == {'my headphones': 'Headset (Razer Kraken)'}


def test_invalid_entries_are_ignored(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, {'windows_audio_aliases': {
        'tv': 'LG', '': 'x', 'blank': '  ', 'number': 3, 'none': None}})
    assert cfg.windows_audio_aliases == {'tv': 'LG'}


@pytest.mark.parametrize('bad', ['tv', None, 3, ['tv']])
def test_a_non_mapping_means_none(tmp_path, monkeypatch, bad):
    assert load(tmp_path, monkeypatch, {'windows_audio_aliases': bad}).windows_audio_aliases == {}

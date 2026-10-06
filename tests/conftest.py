import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Robustly locate repository root (directory containing src/jarvis)
_this_file = Path(__file__).resolve()
ROOT = None
for parent in _this_file.parents:
    if (parent / "src" / "jarvis").exists():
        ROOT = parent
        break
if ROOT is None:
    # Fallback to two levels up
    ROOT = _this_file.parent.parent

SRC = ROOT / "src"
# Both ROOT and SRC are on sys.path so tests can write either
#   ``from src.jarvis.x import ...``  (older style, ``src.`` prefix)
# or
#   ``from jarvis.x import ...``      (newer style, no prefix)
# CAUTION: those two import paths resolve to *distinct module instances*.
# A monkeypatch on ``src.jarvis.memory.conversation.X`` does NOT take
# effect on ``jarvis.memory.conversation.X`` and vice versa. When a test
# stubs out a symbol the production code calls, you MUST patch the same
# module instance the production code resolves at runtime. Production code
# in ``src/`` imports without the ``src.`` prefix (e.g. inside endpoint
# handlers it's ``from jarvis.memory.conversation import ...``), so a test
# that monkeypatches a symbol used by production should also import
# without the prefix. This is the convention going forward; the older
# ``from src.X`` style is left in place to avoid a churn-only sweep, but
# do not adopt it for new tests that monkeypatch.
# Add repository root so that 'src' is a package prefix.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Also add the src directory (optional, for backwards compatibility with direct 'jarvis' imports)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass
class MockConfig:
    """Minimal config object for unit tests that need a config."""
    # Provider-aware fields. Default to Ollama at localhost so tests
    # that don't care about providers keep the historical behaviour.
    llm_provider: str = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    # ``llm_chat_model`` defaults to empty so tests that pin
    # ``ollama_chat_model = "gpt-oss:20b"`` to exercise the LARGE-model
    # branch get the legacy alias promoted into ``llm_chat_model`` by
    # ``__post_init__`` — same shape ``load_settings()`` produces.
    llm_chat_model: str = ""
    whisper_model: str = "small"
    embedding_provider: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "nomic-embed-text"
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "gemma4:e2b"
    ollama_embed_model: str = "nomic-embed-text"
    db_path: str = ":memory:"
    sqlite_vss_path: Optional[str] = None
    voice_debug: bool = True
    tts_enabled: bool = False
    tts_engine: str = "piper"
    tts_voice: Optional[str] = None
    tts_rate: int = 200
    tts_piper_model_path: Optional[str] = None
    tts_piper_speaker: Optional[int] = None
    tts_piper_length_scale: float = 1.0
    tts_piper_noise_scale: float = 0.667
    tts_piper_noise_w: float = 0.8
    tts_piper_sentence_silence: float = 0.2
    tts_chatterbox_device: str = "cpu"
    tts_chatterbox_audio_prompt: Optional[str] = None
    tts_chatterbox_exaggeration: float = 0.5
    tts_chatterbox_cfg_weight: float = 0.5
    web_search_enabled: bool = True
    brave_search_api_key: str = ""
    wikipedia_fallback_enabled: bool = True
    windows_tools_enabled: bool = True
    windows_app_aliases: Dict[str, str] = field(default_factory=dict)
    windows_monitor_aliases: Dict[str, str] = field(default_factory=dict)
    windows_audio_aliases: Dict[str, str] = field(default_factory=dict)
    windows_window_zones: Dict[str, Dict[str, list]] = field(default_factory=dict)
    windows_workspaces: Dict[str, dict] = field(default_factory=dict)
    routines: Dict[str, dict] = field(default_factory=dict)
    windows_fancyzones_enabled: bool = True
    roku_host: str = ""
    tts_pc_speakers_enabled: bool = True
    fast_commands_enabled: bool = True
    fast_commands_locales: List[str] = field(default_factory=lambda: ['en'])
    llm_tools_timeout_sec: float = 8.0
    llm_embedding_timeout_sec: float = 10.0
    llm_chat_timeout_sec: float = 45.0
    agentic_max_turns: int = 8
    tool_selection_strategy: str = "embedding"
    fast_model: str = ""
    tool_model: str = ""
    memory_enrichment_max_results: int = 5
    memory_enrichment_source: str = "diary"
    location_enabled: bool = True
    location_ip_address: Optional[str] = None
    location_auto_detect: bool = False
    location_cgnat_resolve_public_ip: bool = False
    location_cache_minutes: int = 60
    dialogue_memory_timeout: int = 300
    llm_thinking_enabled: bool = False
    intent_judge_thinking_enabled: bool = False
    dictation_thinking_enabled: bool = False
    dictation_hotkey: str = "ctrl+alt+space"
    mcps: Dict[str, Any] = field(default_factory=dict)
    use_stdin: bool = True

    def __post_init__(self) -> None:
        # Mirror ``load_settings``: when the provider-aware fields are
        # left empty, promote the legacy ``ollama_*`` aliases. Tests can
        # set either pair and end up with consistent reads on either side.
        if not self.llm_chat_model:
            self.llm_chat_model = self.ollama_chat_model
        if not self.llm_base_url:
            self.llm_base_url = self.ollama_base_url
        if not self.embedding_model:
            self.embedding_model = self.ollama_embed_model


@pytest.fixture(autouse=True)
def _isolate_user_config_path(tmp_path_factory, monkeypatch):
    """Redirect ``default_config_path`` to a per-session tempfile so a test
    that calls ``load_settings`` (or any other code path that resolves the
    user's config) cannot read or overwrite ``~/.config/jarvis/config.json``.

    Tests that need to exercise the loader against specific JSON should
    monkey-patch ``_load_json`` (and ``_save_json`` if the migration would
    trigger a write) directly. This fixture is a belt-and-braces guard so
    a half-mocked test cannot reach the real config file.
    """
    sandbox = tmp_path_factory.mktemp("jarvis_config_sandbox")
    monkeypatch.setattr(
        "jarvis.config.default_config_path", lambda: sandbox / "config.json"
    )
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(sandbox / "config.json"))


@pytest.fixture(autouse=True)
def _isolate_routines():
    """Each test starts with no live routines loaded and an empty recent-actions journal."""
    from jarvis.routines import store
    from jarvis.routines.journal import get_journal
    store.reset()
    get_journal().clear()
    yield
    store.reset()
    get_journal().clear()


class _InMemoryCredentialStore:
    """The keyring API over a dict, so no test reads or writes the real credential store."""

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture(autouse=True)
def _isolate_credential_store(monkeypatch):
    """Keep tests away from Windows Credential Manager (API keys live there, not in config)."""
    import jarvis.credentials as credentials
    monkeypatch.setattr(credentials, "_backend", _InMemoryCredentialStore())


@pytest.fixture(autouse=True)
def _never_touch_a_real_tv(request, monkeypatch):
    """Automated runs never press a key on, launch an app on or type into a real TV.

    Unit tests may reach only loopback fakes. ``integration`` tests may also read from a real device
    (GET only). Anything that commands the real TV is ``interactive`` and run by hand."""
    from jarvis.devices import roku

    def guard(method, host):
        if request.node.get_closest_marker("interactive"):
            return
        if host.startswith("127."):
            return
        if method == "GET" and request.node.get_closest_marker("integration"):
            return
        raise AssertionError(f"Automated tests must not send {method} to a real TV ({host})")

    monkeypatch.setattr(roku, "_SEND_GUARD", guard)


@pytest.fixture
def fake_tv(monkeypatch):
    """A fake Roku TV on loopback (``tests/fake_roku.py``) with the client pointed at it.

    Loopback is allowed here only; production accepts private LAN ranges. Configure a test with
    ``mock_config.roku_host = "127.0.0.1"``."""
    import ipaddress
    from fake_roku import FakeRoku
    from jarvis.devices import roku

    device = FakeRoku()
    monkeypatch.setattr(roku, "ECP_PORT", device.port)
    monkeypatch.setattr(roku, "ALLOWED_NETWORKS", roku.ALLOWED_NETWORKS + (ipaddress.ip_network("127.0.0.0/8"),))
    monkeypatch.setattr(roku, "REPEAT_PAUSE_SEC", 0)
    roku.reset_devices()
    yield device
    roku.reset_devices()
    device.close()


@pytest.fixture(autouse=True)
def _isolate_powertoys_files(monkeypatch):
    """Keep tests from reading the developer's real PowerToys FancyZones folder."""
    monkeypatch.setattr("jarvis.platform.windows.displays.fancyzones_directory", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_file_search(request, monkeypatch):
    """Keep unit tests from querying the developer's real Windows Search or Everything index.
    Integration tests query the real index read-only."""
    if request.node.get_closest_marker("integration"):
        return

    def _no_index(query):
        from jarvis.platform.windows.file_search import SearchUnavailable
        raise SearchUnavailable("file search is disabled in unit tests")

    monkeypatch.setattr("jarvis.platform.windows.file_search.find", _no_index)

    # localFiles find: no index answers, so only a scan of a named folder works.
    def _index_offline(sql):
        from jarvis.platform.windows.file_search import SearchUnavailable
        raise SearchUnavailable("the Windows Search index is disabled in unit tests")

    monkeypatch.setattr("jarvis.platform.windows.file_search._everything_running", lambda: False)
    monkeypatch.setattr("jarvis.platform.windows.file_search._execute_windows_search", _index_offline)


@pytest.fixture(autouse=True)
def _isolate_orb_state_file(tmp_path, monkeypatch):
    """Keep tests from writing the state file a running Jarvis's orb reads."""
    try:
        import desktop_app.face_widget as face_widget
    except Exception:
        return
    monkeypatch.setattr(face_widget, "_get_jarvis_state_file", lambda: str(tmp_path / "jarvis_state"))
    monkeypatch.setattr(face_widget, "_jarvis_state_instance", None)


@pytest.fixture
def mock_config():
    """Provide a mock configuration for unit tests."""
    return MockConfig()


@pytest.fixture
def db():
    """Provide an in-memory database for unit tests."""
    from jarvis.memory.db import Database
    database = Database(":memory:", sqlite_vss_path=None)
    yield database
    database.close()


@pytest.fixture
def dialogue_memory():
    """Provide a dialogue memory instance for unit tests."""
    from jarvis.memory.conversation import DialogueMemory
    return DialogueMemory(inactivity_timeout=300, max_interactions=20)


@pytest.fixture(scope="session")
def _qapplication():
    """Keep one QApplication alive for the entire test process.

    Qt requires exactly one QApplication per process.  Re-uses an existing
    instance when present so repeated test runs inside a single session
    don't error.  The offscreen platform is set as a default so headless CI
    (no DISPLAY, no xvfb) can construct a real QApplication instead of
    aborting; machines with a real display are unaffected because the
    platform is only set when it is not already configured.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def qapp(_qapplication):
    """Dispose each test's widgets while the shared application is still alive."""
    from PyQt6 import sip
    from PyQt6.QtCore import QCoreApplication, QEvent

    existing = set(_qapplication.topLevelWidgets())
    yield _qapplication
    for widget in _qapplication.topLevelWidgets():
        if widget not in existing and not sip.isdeleted(widget):
            widget.deleteLater()
    # Flush deferred destruction, not unrelated timers or application callbacks.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

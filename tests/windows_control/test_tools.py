"""Behaviour of the Windows control tool adapters and their registration."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.platform.windows import audio, media, telemetry
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.windows.media_control import MediaControlTool
from jarvis.tools.builtin.windows.system_info import SystemInfoTool
from jarvis.tools.builtin.windows.system_volume import SystemVolumeTool

from .test_audio import FakeEndpoint
from .test_media import FakeBackend, _session
from .test_telemetry import MB, FakeProc, _fake_psutil


def _ctx(enabled=True):
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    ctx.cfg = SimpleNamespace(windows_tools_enabled=enabled)
    return ctx


@pytest.fixture
def endpoint(monkeypatch):
    fake = FakeEndpoint(scalar=0.5)
    monkeypatch.setattr(audio, "open_default_endpoint", lambda: fake)
    return fake


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend(_session("playing"))
    monkeypatch.setattr(media, "create_backend", lambda: fake)
    return fake


@pytest.fixture
def system(monkeypatch):
    items = [
        FakeProc(1, "chrome.exe", 900, 6.0),
        FakeProc(2, "code.exe", 700, 20.0),
        FakeProc(3, "tiny.exe", 5, 0.0),
    ]
    monkeypatch.setattr(telemetry, "psutil", _fake_psutil(items))
    monkeypatch.setattr(telemetry, "CPU_SAMPLE_SEC", 0)
    monkeypatch.setattr(telemetry, "_working_set_snapshot", lambda: None)
    monkeypatch.setattr(
        telemetry,
        "_run_nvidia_smi",
        lambda: "NVIDIA GeForce RTX 4060, 7, 4219, 12227, 52\n",
    )


# --- systemVolume -----------------------------------------------------------


@pytest.mark.unit
def test_volume_get(endpoint):
    res = SystemVolumeTool().run({"action": "get"}, _ctx())
    assert res.success and "50%" in res.reply_text


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [(30, 30), ("30", 30), ("30%", 30), (42.4, 42)])
def test_volume_set_accepts_loosely_typed_percent(endpoint, raw, expected):
    res = SystemVolumeTool().run({"action": "set", "percent": raw}, _ctx())
    assert res.success
    assert endpoint.scalar == pytest.approx(expected / 100)
    assert f"{expected}%" in res.reply_text


@pytest.mark.unit
@pytest.mark.parametrize("args", [
    {"action": "set"},
    {"action": "set", "percent": "loud"},
    {"action": "set", "percent": 140},
    {"action": "set", "percent": -5},
])
def test_volume_set_rejects_missing_or_invalid_percent(endpoint, args):
    res = SystemVolumeTool().run(args, _ctx())
    assert not res.success
    assert endpoint.scalar == 0.5


@pytest.mark.unit
def test_volume_up_and_down_use_amount_or_a_default_step(endpoint):
    tool = SystemVolumeTool()
    up = tool.run({"action": "up", "amount": 15}, _ctx())
    assert up.success and endpoint.scalar == pytest.approx(0.65)
    down = tool.run({"action": "down"}, _ctx())
    assert down.success and endpoint.scalar < 0.65


@pytest.mark.unit
def test_volume_mute_and_unmute(endpoint):
    tool = SystemVolumeTool()
    assert tool.run({"action": "mute"}, _ctx()).success and endpoint.muted
    assert tool.run({"action": "unmute"}, _ctx()).success and not endpoint.muted


@pytest.mark.unit
def test_volume_unknown_action_fails(endpoint):
    assert not SystemVolumeTool().run({"action": "explode"}, _ctx()).success


@pytest.mark.unit
def test_volume_device_error_is_reported(monkeypatch):
    def boom():
        raise OSError("gone")

    monkeypatch.setattr(audio, "open_default_endpoint", boom)
    res = SystemVolumeTool().run({"action": "get"}, _ctx())
    assert not res.success


# --- mediaControl -----------------------------------------------------------


@pytest.mark.unit
def test_media_now_playing_names_title_and_artist(backend):
    res = MediaControlTool().run({"action": "now_playing"}, _ctx())
    assert res.success
    assert "Song" in res.reply_text and "Artist" in res.reply_text


@pytest.mark.unit
def test_media_now_playing_without_session_is_honest(monkeypatch):
    monkeypatch.setattr(media, "create_backend", lambda: FakeBackend(None))
    res = MediaControlTool().run({"action": "now_playing"}, _ctx())
    assert res.success
    assert "Song" not in res.reply_text


@pytest.mark.unit
def test_media_pause_when_already_paused_does_not_start_playback(monkeypatch):
    fake = FakeBackend(_session("paused"))
    monkeypatch.setattr(media, "create_backend", lambda: fake)
    res = MediaControlTool().run({"action": "pause"}, _ctx())
    assert res.success
    assert fake.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("action,call", [
    ("pause", "pause"), ("play_pause", "toggle"), ("next", "next"), ("previous", "previous"),
])
def test_media_actions_reach_the_session(backend, action, call):
    res = MediaControlTool().run({"action": action}, _ctx())
    assert res.success
    assert backend.calls == [call]


@pytest.mark.unit
def test_media_no_session_is_a_failure_the_model_can_relay(monkeypatch):
    monkeypatch.setattr(media, "create_backend", lambda: FakeBackend(None))
    res = MediaControlTool().run({"action": "next"}, _ctx())
    assert not res.success


@pytest.mark.unit
def test_media_unknown_action_fails(backend):
    assert not MediaControlTool().run({"action": "rewind"}, _ctx()).success


# --- systemInfo -------------------------------------------------------------


@pytest.mark.unit
def test_system_info_cpu_ram_disk(system):
    tool = SystemInfoTool()
    assert f"{37.5:.0f}%" in tool.run({"action": "cpu"}, _ctx()).reply_text
    assert "25" in tool.run({"action": "ram"}, _ctx()).reply_text
    assert "C:" in tool.run({"action": "disk"}, _ctx()).reply_text


@pytest.mark.unit
def test_system_info_gpu_reports_all_four_readings(system):
    res = SystemInfoTool().run({"action": "gpu"}, _ctx())
    assert res.success
    for fragment in ("RTX 4060", "7%", "4219", "12227", "52"):
        assert fragment in res.reply_text


@pytest.mark.unit
def test_system_info_gpu_unavailable_is_reported_not_invented(monkeypatch):
    monkeypatch.setattr(telemetry, "_run_nvidia_smi", lambda: None)
    res = SystemInfoTool().run({"action": "gpu"}, _ctx())
    assert res.success
    assert "not available" in res.reply_text.lower()


@pytest.mark.unit
def test_system_info_top_memory_and_cpu_rank_correctly(system):
    tool = SystemInfoTool()
    mem = tool.run({"action": "top_memory", "limit": 2}, _ctx()).reply_text
    assert mem.index("chrome.exe") < mem.index("code.exe")
    assert "tiny.exe" not in mem
    cpu = tool.run({"action": "top_cpu", "limit": 2}, _ctx()).reply_text
    assert cpu.index("code.exe") < cpu.index("chrome.exe")


@pytest.mark.unit
def test_system_info_processes_lists_count_and_names(system):
    res = SystemInfoTool().run({"action": "processes"}, _ctx())
    assert res.success
    assert "3" in res.reply_text
    assert "chrome.exe" in res.reply_text


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, -3, "many"])
def test_system_info_bad_limit_falls_back_to_default(system, limit):
    res = SystemInfoTool().run({"action": "top_memory", "limit": limit}, _ctx())
    assert res.success and "chrome.exe" in res.reply_text


@pytest.mark.unit
def test_system_info_unknown_action_fails(system):
    assert not SystemInfoTool().run({"action": "weather"}, _ctx()).success


@pytest.mark.unit
@pytest.mark.parametrize('action,operation', [
    ('cpu', 'cpu_usage'), ('ram', 'memory_usage'), ('disk', 'disk_usage'),
    ('gpu', 'gpu_stats'), ('processes', 'running_processes'),
    ('top_memory', 'top_processes'), ('top_cpu', 'top_processes'),
])
def test_stalled_system_read_returns_within_deadline(action, operation, monkeypatch):
    import threading
    import time
    finished = threading.Event()
    def stalled(*args, **kwargs):
        finished.wait(.5)
        raise OSError('Unavailable resource')
    monkeypatch.setattr(telemetry, operation, stalled)
    monkeypatch.setattr(SystemInfoTool, 'timeout_sec', .02, raising=False)
    context = _ctx()
    start = time.monotonic()
    try:
        result = SystemInfoTool().run({'action': action}, context)
        assert not result.success
        assert time.monotonic() - start < .3
    finally:
        finished.set()


@pytest.mark.unit
def test_system_info_output_excludes_command_lines_and_titles(system):
    res = SystemInfoTool().run({"action": "top_memory"}, _ctx())
    assert "cmdline" not in res.reply_text.lower()


# --- shared behaviour -------------------------------------------------------


@pytest.mark.unit
def test_disabled_setting_blocks_every_tool_without_touching_the_system(endpoint, backend, system):
    ctx = _ctx(enabled=False)
    for tool, args in (
        (SystemVolumeTool(), {"action": "mute"}),
        (MediaControlTool(), {"action": "pause"}),
        (SystemInfoTool(), {"action": "cpu"}),
    ):
        res = tool.run(args, ctx)
        assert not res.success
        assert "windows_tools_enabled" in (res.reply_text or res.error_message)
    assert endpoint.muted is False
    assert backend.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("tool", [SystemVolumeTool(), MediaControlTool(), SystemInfoTool()])
def test_schema_requires_a_closed_action_enum(tool):
    schema = tool.inputSchema
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"]
    assert tool.name and tool.description


@pytest.mark.unit
def test_tool_modules_import_without_windows_libraries():
    """Adapters must import on any OS: OS libraries load lazily at call time."""
    code = (
        "import sys\n"
        "for m in ('pycaw','pycaw.pycaw','comtypes','winrt','psutil'):\n"
        "    sys.modules[m] = None\n"
        "import jarvis.tools.builtin.windows\n"
        "import jarvis.platform.windows.audio, jarvis.platform.windows.media\n"
    )
    root = __file__.rsplit("tests", 1)[0]
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root, capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": root + "src"},
    )
    assert res.returncode == 0, res.stderr


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Windows tools register on win32 only")
def test_tools_are_registered_in_the_builtin_registry():
    from jarvis.tools.registry import BUILTIN_TOOLS

    for name in ("systemVolume", "mediaControl", "systemInfo"):
        assert name in BUILTIN_TOOLS


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Windows tools register on win32 only")
def test_registry_dispatch_runs_a_windows_tool(endpoint):
    from jarvis.tools.registry import run_tool_with_retries

    cfg = SimpleNamespace(windows_tools_enabled=True, voice_debug=True)
    res = run_tool_with_retries(
        None, cfg, "systemVolume", {"action": "set", "percent": 20}, "", "", ""
    )
    assert res.success
    assert endpoint.scalar == pytest.approx(0.2)


@pytest.mark.unit
def test_windows_tools_enabled_config_default_and_override(tmp_path, monkeypatch):
    import json

    from jarvis.config import get_default_config, load_settings

    assert get_default_config()["windows_tools_enabled"] is True
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"windows_tools_enabled": False}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    assert load_settings().windows_tools_enabled is False


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Windows tools register on win32 only")
def test_config_switch_removes_and_restores_the_tools_in_the_catalogue():
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_windows_tools

    names = ("systemVolume", "mediaControl", "systemInfo")
    try:
        configure_windows_tools(SimpleNamespace(windows_tools_enabled=False), start_index=False)
        assert not any(n in BUILTIN_TOOLS for n in names)
        configure_windows_tools(SimpleNamespace(windows_tools_enabled=True), start_index=False)
        assert all(n in BUILTIN_TOOLS for n in names)
    finally:
        configure_windows_tools(SimpleNamespace(windows_tools_enabled=True), start_index=False)


@pytest.mark.unit
def test_non_windows_platform_registers_none_of_the_tools():
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_windows_tools

    try:
        configure_windows_tools(
            SimpleNamespace(windows_tools_enabled=True), platform="linux", start_index=False
        )
        assert not any(n in BUILTIN_TOOLS for n in ("systemVolume", "mediaControl", "systemInfo"))
    finally:
        if sys.platform == "win32":
            configure_windows_tools(SimpleNamespace(windows_tools_enabled=True), start_index=False)

"""Behaviour of the telemetry layer, driven through fake psutil and nvidia-smi."""

import sys
from types import SimpleNamespace

import pytest

from jarvis.platform.windows import telemetry

MB = 1024 * 1024


class _Denied(Exception):
    pass


class FakeProc:
    def __init__(self, pid, name, rss_mb, cpu):
        self.info = {"pid": pid, "name": name}
        self._rss = rss_mb * MB
        self._cpu = cpu

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)

    def cpu_percent(self, interval=None):
        return self._cpu


class DeniedProc(FakeProc):
    def memory_info(self):
        raise _Denied()


def _fake_psutil(procs, cores=4):
    return SimpleNamespace(
        process_iter=lambda attrs=None: iter(procs),
        cpu_count=lambda logical=True: cores if logical else cores // 2,
        cpu_percent=lambda interval=None: 37.5,
        virtual_memory=lambda: SimpleNamespace(
            used=8 * 1024 * MB, total=32 * 1024 * MB, percent=25.0
        ),
        disk_partitions=lambda all=False: [
            SimpleNamespace(mountpoint="C:\\", fstype="NTFS", opts="rw,fixed"),
            SimpleNamespace(mountpoint="D:\\", fstype="", opts="cdrom"),
        ],
        disk_usage=lambda path: SimpleNamespace(
            used=100 * 1024 * MB, total=500 * 1024 * MB, percent=20.0
        ),
        AccessDenied=_Denied,
        NoSuchProcess=LookupError,
    )


@pytest.fixture
def procs(monkeypatch):
    items = [
        FakeProc(0, "System Idle Process", 0, 90.0),
        FakeProc(1, "chrome.exe", 400, 8.0),
        FakeProc(2, "Chrome.exe", 600, 4.0),
        FakeProc(3, "code.exe", 700, 20.0),
        FakeProc(4, "tiny.exe", 5, 0.0),
    ]
    monkeypatch.setattr(telemetry, "psutil", _fake_psutil(items))
    monkeypatch.setattr(telemetry, "CPU_SAMPLE_SEC", 0)
    # These tests exercise the per-process psutil path; the system-wide
    # snapshot has its own tests below.
    monkeypatch.setattr(telemetry, "_working_set_snapshot", lambda: None)
    return items


@pytest.mark.unit
def test_cpu_usage_reports_percent_and_cores(procs):
    cpu = telemetry.cpu_usage()
    assert cpu.percent == 37.5
    assert cpu.logical_cores == 4


@pytest.mark.unit
def test_memory_usage(procs):
    mem = telemetry.memory_usage()
    assert mem.percent == 25.0
    assert mem.used_bytes < mem.total_bytes


@pytest.mark.unit
def test_disk_usage_skips_removable_and_unreadable_drives(procs):
    disks = telemetry.disk_usage()
    assert [d.mount for d in disks] == ["C:\\"]
    assert disks[0].percent == 20.0


@pytest.mark.unit
def test_top_by_memory_groups_same_name_case_insensitively(procs):
    top = telemetry.top_processes("memory", limit=3)
    # Two chrome processes (400 + 600 MB) outrank the single 700 MB process.
    assert [g.name.lower() for g in top] == ["chrome.exe", "code.exe", "tiny.exe"]
    assert top[0].count == 2
    assert top[0].memory_bytes == 1000 * MB


@pytest.mark.unit
def test_top_by_cpu_normalises_by_cores_and_excludes_idle(procs):
    top = telemetry.top_processes("cpu", limit=2)
    assert top[0].name == "code.exe"
    assert top[0].cpu_percent == pytest.approx(20.0 / 4)
    assert all(g.name != "System Idle Process" for g in top)


@pytest.mark.unit
def test_top_processes_rejects_unknown_ranking(procs):
    with pytest.raises(ValueError):
        telemetry.top_processes("network")


@pytest.mark.unit
def test_inaccessible_processes_are_skipped_not_fatal(monkeypatch, procs):
    items = procs + [DeniedProc(9, "protected.exe", 50, 1.0)]
    monkeypatch.setattr(telemetry, "psutil", _fake_psutil(items))
    names = {g.name.lower() for g in telemetry.top_processes("memory", limit=10)}
    assert "protected.exe" not in names
    assert "code.exe" in names


@pytest.mark.unit
def test_running_processes_counts_and_orders_by_memory(procs):
    snap = telemetry.running_processes(limit=2)
    assert snap.total_processes == 4  # idle placeholder excluded
    assert len(snap.groups) == 2
    assert snap.groups[0].memory_bytes >= snap.groups[1].memory_bytes


class SlowProtectedProc(FakeProc):
    """A protected service whose per-process memory query is very slow."""

    def memory_info(self):
        raise AssertionError("memory ranking must not query processes one by one")


@pytest.fixture
def snapshot(monkeypatch):
    items = [
        SlowProtectedProc(0, "System Idle Process", 0, 0.0),
        SlowProtectedProc(1, "chrome.exe", 1, 0.0),
    ]
    monkeypatch.setattr(telemetry, "psutil", _fake_psutil(items))
    working_sets = {
        0: ("", 0),
        4: ("", 9 * MB),
        11: ("chrome.exe", 400 * MB),
        12: ("Chrome.exe", 600 * MB),
        13: ("svchost.exe", 700 * MB),
        14: ("tiny.exe", 5 * MB),
    }
    monkeypatch.setattr(telemetry, "_working_set_snapshot", lambda: dict(working_sets))
    return working_sets


@pytest.mark.unit
def test_memory_ranking_reads_one_system_snapshot(snapshot):
    top = telemetry.top_processes("memory", limit=3)
    assert [g.name.lower() for g in top] == ["chrome.exe", "svchost.exe", "system"]
    assert top[0].count == 2
    assert top[0].memory_bytes == 1000 * MB


@pytest.mark.unit
def test_process_listing_counts_snapshot_processes_without_idle(snapshot):
    snap = telemetry.running_processes(limit=10)
    assert snap.total_processes == len(snapshot) - 1
    assert {g.name.lower() for g in snap.groups} == {"chrome.exe", "svchost.exe", "tiny.exe", "system"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system snapshot")
def test_real_snapshot_reports_this_process_quickly():
    import os
    import time

    psutil = pytest.importorskip("psutil")
    started = time.perf_counter()
    working_sets = telemetry._working_set_snapshot()
    elapsed = time.perf_counter() - started
    assert working_sets is not None
    name, working_set = working_sets[os.getpid()]
    assert name.lower() == psutil.Process().name().lower()
    rss = psutil.Process().memory_info().rss
    assert 0.5 * rss <= working_set <= 2 * rss
    assert len(working_sets) > 10
    assert elapsed < 1.0


@pytest.mark.unit
def test_gpu_stats_parse_nvidia_smi_csv(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "_run_nvidia_smi",
        lambda: "NVIDIA GeForce RTX 4060, 7, 4219, 12227, 52\n",
    )
    gpus = telemetry.gpu_stats()
    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "NVIDIA GeForce RTX 4060"
    assert (g.utilization_percent, g.vram_used_mb, g.vram_total_mb, g.temperature_c) == (
        7,
        4219,
        12227,
        52,
    )


@pytest.mark.unit
def test_gpu_stats_treat_not_available_fields_as_unknown(monkeypatch):
    monkeypatch.setattr(
        telemetry, "_run_nvidia_smi", lambda: "Some GPU, [N/A], 100, 200, [N/A]\n"
    )
    g = telemetry.gpu_stats()[0]
    assert g.utilization_percent is None
    assert g.temperature_c is None
    assert g.vram_used_mb == 100


@pytest.mark.unit
@pytest.mark.parametrize("raw", [None, "", "garbage line"])
def test_gpu_stats_empty_when_unavailable_or_unparseable(monkeypatch, raw):
    monkeypatch.setattr(telemetry, "_run_nvidia_smi", lambda: raw)
    assert telemetry.gpu_stats() == []

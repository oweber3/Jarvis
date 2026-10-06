"""Display brightness: DDC/CI for external monitors, WMI for internal panels.

External monitors are addressed through the Monitor Configuration API (``dxva2``,
``GetMonitorBrightness`` / ``SetMonitorBrightness``, VCP code 0x10). A monitor that does not
answer DDC/CI is reported as unsupported with the reason; it is never skipped silently and no
other monitor's level is changed in its place. Laptop panels use ``WmiMonitorBrightness``.

Every operation runs on a bounded worker and releases the physical-monitor handles it opened.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
from typing import Callable, Protocol

from ...debug import debug_log
from ._bounded import run_bounded

OPERATION_TIMEOUT_SEC = 10.0


class BrightnessError(RuntimeError):
    """Brightness could not be read or changed."""


class BrightnessUnsupported(BrightnessError):
    """One monitor does not support brightness control."""


@dataclass(frozen=True)
class Reading:
    device: str
    number: int
    method: str
    percent: int | None = None
    verified: bool = True
    error: str = ''


class Panel(Protocol):
    device: str
    number: int
    method: str

    def get(self) -> int: ...
    def set(self, percent: int) -> None: ...
    def close(self) -> None: ...


def raw_to_percent(value: int, low: int, high: int) -> int:
    if high <= low:
        return 0
    return max(0, min(100, round((value - low) * 100 / (high - low))))


def percent_to_raw(percent: int, low: int, high: int) -> int:
    return low + round(percent * (high - low) / 100)


class _PhysicalMonitor(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('handle', ctypes.c_void_p), ('description', ctypes.c_wchar * 128)]


def _dxva2():
    dll = ctypes.WinDLL('dxva2', use_last_error=True)
    dword_p = ctypes.POINTER(ctypes.c_ulong)
    signatures = {
        'GetNumberOfPhysicalMonitorsFromHMONITOR': ([ctypes.c_void_p, dword_p], ctypes.c_int),
        'GetPhysicalMonitorsFromHMONITOR': ([ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_PhysicalMonitor)],
                                            ctypes.c_int),
        'DestroyPhysicalMonitors': ([ctypes.c_ulong, ctypes.POINTER(_PhysicalMonitor)], ctypes.c_int),
        'GetMonitorBrightness': ([ctypes.c_void_p, dword_p, dword_p, dword_p], ctypes.c_int),
        'SetMonitorBrightness': ([ctypes.c_void_p, ctypes.c_ulong], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


class DdcPanel:
    """One physical monitor reached through DDC/CI."""
    method = 'ddc'

    def __init__(self, device: str, number: int, handle: int, release: Callable[[], None] = lambda: None):
        self.device, self.number = device, number
        self._handle = ctypes.c_void_p(handle)
        self._release = release

    def _range(self) -> tuple[int, int, int]:
        low, current, high = (ctypes.c_ulong() for _ in range(3))
        if not _dxva2().GetMonitorBrightness(self._handle, ctypes.byref(low), ctypes.byref(current),
                                              ctypes.byref(high)):
            raise BrightnessUnsupported('This monitor does not support DDC/CI brightness control. '
                                        'Enable DDC/CI in its on-screen menu, or use its own controls.')
        return low.value, current.value, high.value

    def get(self) -> int:
        low, current, high = self._range()
        return raw_to_percent(current, low, high)

    def set(self, percent: int) -> None:
        low, _current, high = self._range()
        if not _dxva2().SetMonitorBrightness(self._handle, percent_to_raw(percent, low, high)):
            raise BrightnessUnsupported('This monitor rejected the brightness change.')

    def close(self) -> None:
        self._release()


class WmiPanel:
    """An internal panel, reached through WMI."""
    method = 'wmi'

    def __init__(self, device: str, number: int, read: Callable[[], int], write: Callable[[int], None]):
        self.device, self.number = device, number
        self._read, self._write = read, write

    def get(self) -> int:
        return int(self._read())

    def set(self, percent: int) -> None:
        self._write(percent)

    def close(self) -> None:
        pass


def _wmi_panels(without_ddc: list[tuple[str, int]]) -> list[Panel]:
    """Internal panels from WMI, attached in order to the displays DDC/CI could not drive."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return []
    pythoncom.CoInitialize()
    try:
        service = win32com.client.GetObject(r'winmgmts:\\.\root\wmi')
        levels = list(service.ExecQuery('SELECT * FROM WmiMonitorBrightness'))
        methods = list(service.ExecQuery('SELECT * FROM WmiMonitorBrightnessMethods'))
    except Exception as exc:  # noqa: BLE001 - no internal panel is the normal desktop case
        debug_log(f'WMI brightness unavailable ({type(exc).__name__}).', 'windows')
        return []
    panels: list[Panel] = []
    for index, (level, method) in enumerate(zip(levels, methods)):
        device, number = without_ddc[index] if index < len(without_ddc) else ('internal', index + 1)
        panels.append(WmiPanel(device, number, lambda level=level: level.CurrentBrightness,
                               lambda percent, method=method: method.WmiSetBrightness(1, percent)))
    return panels


def _physical_panels(dxva2, monitor, hmonitor: int) -> list[DdcPanel]:
    """The physical monitors behind one display handle (usually one), released together."""
    handle = ctypes.c_void_p(hmonitor)
    count = ctypes.c_ulong()
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(handle, ctypes.byref(count)) or not count.value:
        return []
    array = (_PhysicalMonitor * count.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(handle, count.value, array):
        return []
    total = count.value
    release = lambda: dxva2.DestroyPhysicalMonitors(total, array)  # noqa: E731 - first panel owns the array
    return [DdcPanel(monitor.device, monitor.number, array[index].handle, release if index == 0 else lambda: None)
            for index in range(total)]


def _open_panels() -> list[Panel]:
    """A panel per DDC/CI-capable physical monitor, and WMI panels for displays DDC/CI cannot drive."""
    from . import displays
    dxva2 = _dxva2()
    panels: list[Panel] = []
    without_ddc: list[tuple[str, int]] = []
    for monitor, hmonitor in displays.monitor_handles():
        physical = _physical_panels(dxva2, monitor, hmonitor)
        try:
            if not physical:
                raise BrightnessUnsupported('No physical monitor was reported.')
            physical[0].get()
        except BrightnessUnsupported:
            without_ddc.append((monitor.device, monitor.number))
        panels.extend(physical)
    internal = _wmi_panels(without_ddc)
    if internal:
        # An internal panel answers through WMI, so its failed DDC/CI entry is replaced.
        replaced = {panel.device for panel in internal}
        for panel in [panel for panel in panels if panel.device in replaced]:
            panel.close()
            panels.remove(panel)
    return panels + internal


def _run(operation: Callable[[Panel], Reading], device: str | None) -> list[Reading]:
    panels = _open_panels()
    try:
        if not panels:
            raise BrightnessError('No monitor reported brightness control.')
        selected = panels
        if device is not None:
            selected = [panel for panel in panels if panel.device.casefold() == device.casefold()]
            if not selected:
                raise ValueError('That display has no brightness control.')
        results = []
        for panel in selected:
            try:
                results.append(operation(panel))
            except BrightnessUnsupported as exc:
                results.append(Reading(panel.device, panel.number, panel.method, None, False, str(exc)))
        return results
    finally:
        for panel in panels:
            try:
                panel.close()
            except Exception as exc:  # noqa: BLE001 - releasing is best effort
                debug_log(f'Releasing a monitor handle failed ({type(exc).__name__}).', 'windows')


def _bounded(operation: Callable[[Panel], Reading], device: str | None) -> list[Reading]:
    try:
        return run_bounded(lambda: _run(operation, device), OPERATION_TIMEOUT_SEC)
    except TimeoutError as exc:
        raise BrightnessError('The monitor did not respond in time.') from exc


def _checked_percent(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('A brightness percentage between 0 and 100 is required.')
    if not 0 <= value <= 100:
        raise ValueError(f'Brightness must be between 0 and 100, not {round(value)}.')
    return round(value)


def read_levels(device: str | None = None) -> list[Reading]:
    return _bounded(lambda panel: Reading(panel.device, panel.number, panel.method, panel.get()), device)


def _write(panel: Panel, wanted: int) -> Reading:
    panel.set(wanted)
    try:
        actual = panel.get()
    except BrightnessError:
        return Reading(panel.device, panel.number, panel.method, wanted, False)
    return Reading(panel.device, panel.number, panel.method, actual, abs(actual - wanted) <= 1)


def set_levels(percent, device: str | None = None) -> list[Reading]:
    wanted = _checked_percent(percent)
    debug_log('Brightness set requested.', 'windows')
    return _bounded(lambda panel: _write(panel, wanted), device)


def adjust_levels(delta: int, device: str | None = None) -> list[Reading]:
    debug_log('Brightness adjustment requested.', 'windows')
    return _bounded(lambda panel: _write(panel, max(0, min(100, panel.get() + delta))), device)

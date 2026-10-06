"""Monitor discovery and zone geometry for window placement.

Rectangles are ``(left, top, right, bottom)`` in physical pixels in the virtual
desktop, so secondary monitors can have negative coordinates. The geometry
functions are pure; OS libraries load only inside the native helpers.
"""
from __future__ import annotations

import contextlib
import ctypes
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import re
from typing import Sequence
import uuid

from ...debug import debug_log
from . import fancyzones
from .fancyzones import ZoneSet

Rectangle = tuple[int, int, int, int]

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, passed as a pseudo handle.
PER_MONITOR_AWARE_V2 = -4
_MONITORINFOF_PRIMARY = 1
_MONITOR_DEFAULTTONEAREST = 2
# Zones narrower than this fraction of a work area are rounding noise, not a layout.
_MIN_ZONE_FRACTION = 1e-3


@dataclass(frozen=True)
class Monitor:
    """A connected display. The hardware identity is what PowerToys knows it by; it is
    empty when Windows reports none."""
    device: str
    bounds: Rectangle
    work_area: Rectangle
    primary: bool
    hardware_id: str = ''
    instance_id: str = ''
    serial: str = ''

    @property
    def number(self) -> int:
        """The display number at the end of the device name (DISPLAY2 is 2), or 0."""
        digits = re.search(r'(\d+)$', self.device)
        return int(digits.group(1)) if digits else 0


def _real_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalise_zone(zone) -> tuple[float, float, float, float]:
    """Validate ``[x, y, width, height]`` as fractions of a work area."""
    if not isinstance(zone, (list, tuple)) or len(zone) != 4 or not all(_real_number(v) for v in zone):
        raise ValueError('A zone is four finite numbers: x, y, width and height.')
    x, y, width, height = (float(v) for v in zone)
    if width < _MIN_ZONE_FRACTION or height < _MIN_ZONE_FRACTION:
        raise ValueError('A zone needs a positive width and height.')
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError('A zone origin must lie within the work area (0 to 1).')
    epsilon = 1e-9
    if x + width > 1 + epsilon or y + height > 1 + epsilon:
        raise ValueError('A zone must lie wholly within the work area.')
    return x, y, width, height


def zone_rectangle(monitor: Monitor, zone) -> Rectangle:
    """Map a normalised zone onto the monitor's work area (taskbar excluded)."""
    x, y, width, height = normalise_zone(zone)
    left, top, right, bottom = monitor.work_area
    span_x, span_y = right - left, bottom - top
    # Rounding the edges, not the sizes, lets neighbouring zones tile exactly.
    rectangle = (left + round(x * span_x), top + round(y * span_y),
                 left + round(min(x + width, 1.0) * span_x), top + round(min(y + height, 1.0) * span_y))
    if rectangle[2] <= rectangle[0] or rectangle[3] <= rectangle[1]:
        raise ValueError('That zone is too small for this display.')
    return rectangle


def _display_order(monitors: list[Monitor]) -> list[Monitor]:
    """Connected displays in Windows display-number order, the order Windows Settings numbers them."""
    return sorted(monitors, key=lambda monitor: (monitor.number, monitor.device.casefold()))


def _by_reference(query: str, target: str, monitors: list[Monitor]) -> Monitor | None:
    """Resolve a built-in reference: a display number, ``primary``, ``left`` or ``right``.

    These are tool-argument vocabulary, like an action name, not phrases matched in speech.
    Returns ``None`` when the text is not a reference at all."""
    if query.isascii() and query.isdigit():
        ordered = _display_order(monitors)
        return ordered[int(query) - 1] if 1 <= int(query) <= len(ordered) else None
    if query == 'primary':
        primaries = [monitor for monitor in monitors if monitor.primary]
        if len(primaries) != 1:
            raise ValueError(f'Ambiguous display: {target}')
        return primaries[0]
    if query in ('left', 'right'):
        if len(monitors) < 2:
            raise ValueError('Left and right need more than one connected display.')
        centres = [(monitor.bounds[0] + monitor.bounds[2], monitor) for monitor in monitors]
        pick = min if query == 'left' else max
        edge = pick(centre for centre, _ in centres)
        winners = [monitor for centre, monitor in centres if centre == edge]
        if len(winners) != 1:
            raise ValueError(f'Ambiguous display: {target}')
        return winners[0]
    return None


def resolve_monitor(target: str, monitors: list[Monitor], aliases: dict[str, str]) -> Monitor:
    """Resolve a device identifier, configured alias or built-in reference to one connected monitor.

    Identifiers and configured aliases come first. A number counts connected displays in
    Windows display-number order; ``primary`` names the primary display; ``left`` and
    ``right`` name the display furthest in that direction (by centre). Nothing falls back to
    another display: an unknown label, a disconnected device or a label that could mean two
    displays is an error.
    """
    query = target.strip().casefold() if isinstance(target, str) else ''
    if not query:
        raise ValueError('A monitor identifier or alias is required.')
    by_device: dict[str, list[Monitor]] = {}
    for monitor in monitors:
        by_device.setdefault(monitor.device.casefold(), []).append(monitor)
    candidates = {device for device in by_device if device == query}
    alias_devices = {value.strip().casefold() for key, value in aliases.items()
                     if isinstance(key, str) and isinstance(value, str) and key.strip().casefold() == query}
    if alias_devices:
        connected = {device for device in alias_devices if device in by_device}
        if not connected:
            raise ValueError(f'The display for alias "{target}" is not connected.')
        candidates |= connected
    if not candidates:
        referenced = _by_reference(query, target, monitors)
        if referenced is None:
            raise ValueError(f'No connected display matches: {target}')
        return referenced
    matches = [monitor for device in candidates for monitor in by_device[device]]
    if len(matches) != 1:
        raise ValueError(f'Ambiguous display: {target}')
    return matches[0]


def configured_zones(zones: dict, monitor: Monitor) -> dict[str, list]:
    """The named zones configured for this monitor (identifiers compare case-insensitively)."""
    device = monitor.device.casefold()
    return {label: zone for key, named in zones.items() if isinstance(key, str) and key.casefold() == device
            for label, zone in named.items()}


def _zone_catalogue(zones: dict, monitor: Monitor, fancy: ZoneSet | None) -> list[tuple[str, Rectangle]]:
    """Every zone name usable on this display with its rectangle: configured zones first, then
    FancyZones zones under the names the configuration does not already use."""
    catalogue = [(label, zone_rectangle(monitor, zone)) for label, zone in configured_zones(zones, monitor).items()]
    taken = {label.casefold() for label, _ in catalogue}
    if fancy is not None:
        for names, rectangle in zip(fancy.names, fancy.rectangles):
            catalogue.extend((name, rectangle) for name in names if name.casefold() not in taken)
    return catalogue


def resolve_zone(zones: dict, monitor: Monitor, label: str, fancy: ZoneSet | None = None) -> tuple[str, Rectangle]:
    """Look up a zone by label, case-insensitively, returning its canonical label and rectangle.

    Configured zones win over FancyZones zones of the same name. A near match is never guessed."""
    catalogue = _zone_catalogue(zones, monitor, fancy)
    wanted = label.strip().casefold() if isinstance(label, str) else ''
    matches = {entry for entry in catalogue if entry[0].casefold() == wanted}
    if len(matches) != 1 or not wanted:
        names = ', '.join(name for name, _ in catalogue) or 'none configured'
        raise ValueError(f'Unknown zone for that display. Available zones: {names}')
    return next(iter(matches))


def describe_displays(monitors: list[Monitor], aliases: dict[str, str], zones: dict,
                      fancyzones_by_device: dict[str, ZoneSet] | None = None) -> dict:
    """Live displays with the configured labels and zones, and any FancyZones layout, that apply.

    ``number`` is the value that selects a display as a monitor reference. Labels for
    disconnected devices are omitted, and so is everything else in the config."""
    entries = []
    ordered = _display_order(monitors)
    for monitor in monitors:
        device = monitor.device.casefold()
        labels = sorted(key for key, value in aliases.items()
                        if isinstance(key, str) and isinstance(value, str) and value.strip().casefold() == device)
        entry = {'device': monitor.device, 'number': ordered.index(monitor) + 1, 'primary': monitor.primary,
                 'bounds': list(monitor.bounds), 'work_area': list(monitor.work_area), 'aliases': labels,
                 'zones': {label: list(zone_rectangle(monitor, zone))
                           for label, zone in configured_zones(zones, monitor).items()}}
        fancy = (fancyzones_by_device or {}).get(monitor.device)
        if fancy is not None:
            taken = {label.casefold() for label in entry['zones']}
            listed = [{'names': [name for name in names if name.casefold() not in taken],
                       'rectangle': list(rectangle)} for names, rectangle in zip(fancy.names, fancy.rectangles)]
            entry['fancyzones'] = {'layout': fancy.layout, 'zones': [zone for zone in listed if zone['names']]}
        entries.append(entry)
    return {'displays': entries}


# --- FancyZones data -----------------------------------------------------------------

def fancyzones_directory() -> Path | None:
    """Where PowerToys keeps its FancyZones files, or ``None`` without a local app-data folder."""
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'Microsoft' / 'PowerToys' / 'FancyZones' if base else None


def parse_interface_name(interface: str) -> tuple[str, str]:
    r"""Split a display interface name (``\\?\DISPLAY#GSM5CE4#5&18296720&0&UID4353#{guid}``) into the
    monitor id and instance id, or empty strings when it has no such parts."""
    parts = interface.split('#') if isinstance(interface, str) else []
    return (parts[1], parts[2]) if len(parts) >= 3 and parts[1] and parts[2] else ('', '')


def parse_edid_serial(blob) -> str:
    """The serial-number text descriptor of an EDID block, or an empty string if it has none."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 126:
        return ''
    for offset in (54, 72, 90, 108):
        block = bytes(blob[offset:offset + 18])
        if block[:3] == b'\x00\x00\x00' and block[3] == 0xFF:
            return block[5:].split(b'\x0a')[0].decode('ascii', errors='ignore').strip()
    return ''


def guid_from_registry(raw) -> uuid.UUID | None:
    """A GUID stored as sixteen registry bytes (little-endian fields)."""
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 16:
        return uuid.UUID(bytes_le=bytes(raw[:16]))
    return None


def _registry_bytes(kind: str, name: str):
    """A binary value from Explorer's virtual-desktop keys, or ``None``."""
    try:
        import winreg
        path = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer'
        if kind == 'SessionInfo':
            session = ctypes.c_ulong()
            if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
                return None
            path += rf'\SessionInfo\{session.value}\VirtualDesktops'
        else:
            path += r'\VirtualDesktops'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return bytes(value) if isinstance(value, (bytes, bytearray)) else None
    except (ImportError, AttributeError, OSError):
        return None


def current_virtual_desktop() -> uuid.UUID:
    """The current virtual desktop, found the way PowerToys finds it. A machine with no
    virtual-desktop data has one unnamed desktop (the null GUID)."""
    current = (guid_from_registry(_registry_bytes('VirtualDesktops', 'CurrentVirtualDesktop'))
               or guid_from_registry(_registry_bytes('SessionInfo', 'CurrentVirtualDesktop'))
               or guid_from_registry(_registry_bytes('VirtualDesktops', 'VirtualDesktopIDs')))
    return current or uuid.UUID(int=0)


_DEFAULT = object()


def fancyzone_sets(monitors: list[Monitor], languages: Sequence[str] = ('en',), *,
                   directory=_DEFAULT, desktop=_DEFAULT) -> dict[str, ZoneSet]:
    """FancyZones zones for each connected display on the current virtual desktop, by device.

    PowerToys is optional and its files are only read. Anything missing, unreadable or
    unexpected yields no zones, logged without paths or identifiers."""
    try:
        folder = fancyzones_directory() if directory is _DEFAULT else directory
        data = fancyzones.load_data(folder) if folder else None
        if data is None:
            return {}
        infos = [fancyzones.DisplayInfo(
            fancyzones.DisplayIdentity(monitor.device, monitor.number, monitor.hardware_id,
                                       monitor.instance_id, monitor.serial),
            monitor.bounds, monitor.work_area) for monitor in monitors]
        return fancyzones.zone_sets(infos, data, current_virtual_desktop() if desktop is _DEFAULT else desktop,
                                    fancyzones.load_zone_labels(languages))
    except Exception as exc:  # PowerToys' files are foreign input: placement must work without them
        debug_log(f'FancyZones zones are unavailable ({type(exc).__name__}).', 'windows')
        return {}


# --- native helpers ----------------------------------------------------------------

def _user32():
    from ctypes import wintypes
    dll = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'EnumDisplayMonitors': ([wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPARAM], wintypes.BOOL),
        'EnumDisplayDevicesW': ([wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'GetMonitorInfoW': ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
        'MonitorFromWindow': ([wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
        'SetThreadDpiAwarenessContext': ([ctypes.c_void_p], ctypes.c_void_p),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


def _monitor_info_type():
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                    ('dwFlags', wintypes.DWORD), ('szDevice', wintypes.WCHAR * 32)]

    return MONITORINFOEXW


def _read_monitor(user, handle) -> Monitor:
    info = _monitor_info_type()()
    info.cbSize = ctypes.sizeof(info)
    if not user.GetMonitorInfoW(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    bounds, work = info.rcMonitor, info.rcWork
    return Monitor(info.szDevice, (bounds.left, bounds.top, bounds.right, bounds.bottom),
                   (work.left, work.top, work.right, work.bottom),
                   bool(info.dwFlags & _MONITORINFOF_PRIMARY))


def _display_device_type():
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('DeviceName', wintypes.WCHAR * 32),
                    ('DeviceString', wintypes.WCHAR * 128), ('StateFlags', wintypes.DWORD),
                    ('DeviceID', wintypes.WCHAR * 128), ('DeviceKey', wintypes.WCHAR * 128)]

    return DISPLAY_DEVICEW


def _edid_serial(hardware_id: str, instance_id: str) -> str:
    try:
        import winreg
        path = rf'SYSTEM\CurrentControlSet\Enum\DISPLAY\{hardware_id}\{instance_id}\Device Parameters'
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return parse_edid_serial(winreg.QueryValueEx(key, 'EDID')[0])
    except (ImportError, OSError):
        return ''


def _hardware_identity(user, device: str) -> dict:
    """The monitor id, instance id and EDID serial FancyZones identifies this display by.

    Empty when Windows reports no active monitor for it; the device name then stands in."""
    try:
        record = _display_device_type()
        index = 0
        while True:
            info = record()
            info.cb = ctypes.sizeof(info)
            # EDD_GET_DEVICE_INTERFACE_NAME (1) makes DeviceID the interface path with the instance id.
            if not user.EnumDisplayDevicesW(device, index, ctypes.byref(info), 1):
                return {}
            if info.StateFlags & 0x1 and not info.StateFlags & 0x8:  # active, not a mirroring driver
                hardware_id, instance_id = parse_interface_name(info.DeviceID)
                if not hardware_id:
                    return {}
                return {'hardware_id': hardware_id, 'instance_id': instance_id,
                        'serial': _edid_serial(hardware_id, instance_id)}
            index += 1
    except (AttributeError, OSError):
        return {}


def _set_thread_dpi_context(context):
    """Switch this thread's DPI awareness; return the previous value, or ``None``
    when the API is unavailable (before Windows 10 1607)."""
    try:
        previous = _user32().SetThreadDpiAwarenessContext(ctypes.c_void_p(context))
    except (AttributeError, OSError):
        return None
    return previous or None


@contextlib.contextmanager
def per_monitor_dpi():
    """Make this thread report physical pixels without changing the process mode,
    which Qt owns. The previous context is restored on every exit."""
    previous = _set_thread_dpi_context(PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous is not None:
            _set_thread_dpi_context(previous)


def list_monitors() -> list[Monitor]:
    """Return the connected displays with their bounds and work areas."""
    from ctypes import wintypes
    monitors: list[Monitor] = []
    with per_monitor_dpi():
        user = _user32()
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                           ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def visit(handle, _dc, _rect, _param):
            try:
                monitors.append(_read_monitor(user, handle))
            except OSError:
                debug_log('A display could not be read and was skipped.', 'windows')
            return True

        callback = callback_type(visit)
        if not user.EnumDisplayMonitors(None, None, ctypes.cast(callback, ctypes.c_void_p), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        monitors = [replace(monitor, **_hardware_identity(user, monitor.device)) for monitor in monitors]
    monitors.sort(key=lambda monitor: (not monitor.primary, monitor.device))
    debug_log(f'Display enumeration found {len(monitors)} monitor(s).', 'windows')
    return monitors


def monitor_handles() -> list[tuple[Monitor, int]]:
    """Connected displays paired with their ``HMONITOR``, for APIs that address a monitor by handle."""
    from ctypes import wintypes
    found: list[tuple[Monitor, int]] = []
    with per_monitor_dpi():
        user = _user32()
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                           ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def visit(handle, _dc, _rect, _param):
            try:
                found.append((_read_monitor(user, handle), int(handle)))
            except OSError:
                debug_log('A display could not be read and was skipped.', 'windows')
            return True

        callback = callback_type(visit)
        if not user.EnumDisplayMonitors(None, None, ctypes.cast(callback, ctypes.c_void_p), 0):
            raise ctypes.WinError(ctypes.get_last_error())
    found.sort(key=lambda pair: (not pair[0].primary, pair[0].device))
    return found


def monitor_device_for_window(hwnd: int) -> str:
    """Return the identifier of the display showing most of the window."""
    with per_monitor_dpi():
        user = _user32()
        handle = user.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        if not handle:
            raise OSError('The window is not on any display.')
        return _read_monitor(user, handle).device

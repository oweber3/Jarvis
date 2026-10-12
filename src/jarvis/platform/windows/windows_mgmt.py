"""Visible application windows and graceful Win32 window operations."""
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
import time

from .apps import name_tokens
from . import displays
from ...debug import debug_log

# Native frame rounding and DWM shadows make exact pixel agreement unrealistic.
PLACEMENT_TOLERANCE_PX = 2
_PLACEMENT_ATTEMPTS = 3
_PLACEMENT_BUDGET_SEC = 8.0
_SETTLE_SEC = 0.25
# Window classes of the desktop behind every application window.
DESKTOP_WINDOW_CLASSES = frozenset({'Progman', 'WorkerW'})


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    process: str
    pid: int


def _user32():
    """Declare pointer-sized signatures explicitly for 64-bit Windows."""
    dll = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'EnumWindows': ([ctypes.c_void_p, wintypes.LPARAM], wintypes.BOOL),
        'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
        'IsWindow': ([wintypes.HWND], wintypes.BOOL),
        'IsIconic': ([wintypes.HWND], wintypes.BOOL),
        'IsZoomed': ([wintypes.HWND], wintypes.BOOL),
        'GetWindow': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        'GetWindowTextLengthW': ([wintypes.HWND], ctypes.c_int),
        'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        'GetForegroundWindow': ([], wintypes.HWND),
        'GetShellWindow': ([], wintypes.HWND),
        'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        'SetForegroundWindow': ([wintypes.HWND], wintypes.BOOL),
        'ShowWindowAsync': ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        'AttachThreadInput': ([wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
        'GetWindowRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        'SetWindowPos': ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, wintypes.UINT], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


def _dwmapi():
    """Declare pointer-sized signatures for Desktop Window Manager API."""
    dll = ctypes.WinDLL('dwmapi', use_last_error=True)
    dll.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dll.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT
    return dll


def _is_cloaked(hwnd: int) -> bool:
    """Return True if the window is cloaked by Windows 11 / DWM, False otherwise."""
    dwmwa_cloaked = 14
    cloaked = wintypes.DWORD(0)
    try:
        dwm = _dwmapi()
        hr = dwm.DwmGetWindowAttribute(
            hwnd,
            dwmwa_cloaked,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        if hr != 0:
            debug_log(f'DwmGetWindowAttribute failed for hwnd {hwnd} with hr={hr}', 'windows')
            return False
        return bool(cloaked.value)
    except Exception as exc:
        debug_log(f'DWM cloaked check failed for hwnd {hwnd}: {exc}', 'windows')
        return False


def _is_desktop(user, hwnd, shell) -> bool:
    """The shell's desktop (``GetShellWindow``, ``Progman``, ``WorkerW``): titled windows of explorer.exe that
    are not applications. Closing one opens the Shut Down Windows dialog."""
    if shell and int(hwnd) == int(shell):
        return True
    name = ctypes.create_unicode_buffer(64)
    user.GetClassNameW(hwnd, name, len(name))
    return name.value in DESKTOP_WINDOW_CLASSES


def list_windows() -> list[Window]:
    """Visible, unowned, titled, non-cloaked application windows; never the desktop itself."""
    import psutil
    user = _user32()
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    shell = user.GetShellWindow()

    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd) or user.GetWindow(hwnd, 4) or _is_cloaked(hwnd):
            return True
        if _is_desktop(user, hwnd, shell):
            return True
        length = user.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user.GetWindowTextW(hwnd, title, len(title))
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process = Path(psutil.Process(pid.value).name()).stem
        except (psutil.Error, OSError):
            process = ''
        windows.append(Window(int(hwnd), title.value, process, pid.value))
        return True

    callback = callback_type(visit)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetLastError(0)
    if not user.EnumWindows(callback, 0):
        err = ctypes.get_last_error()
        if err != 0:
            raise ctypes.WinError(err)
    return windows


def resolve_window(target: str, windows: list[Window], *, process_only: bool = False) -> Window:
    """Pick one window. ``process_only`` matches the owning process name or a
    handle, never window titles, so a deterministic route cannot hit another
    application whose title merely contains the word."""
    query = target.strip().casefold()
    if not query:
        raise ValueError('An application name or window handle is required.')
    if process_only:
        matches = [window for window in windows
                   if query in (str(window.hwnd), window.process.casefold())]
    else:
        exact = [window for window in windows if query in
                 (str(window.hwnd), window.process.casefold(), window.title.casefold())]
        matches = exact or [window for window in windows if name_tokens(query) and
                           name_tokens(query) <= name_tokens(window.title)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f'No open application window matches: {target}')
    raise ValueError('Multiple windows match; specify a window handle from list: ' +
                     ', '.join(str(window.hwnd) for window in matches))


def _check_window(user, hwnd):
    if not user.IsWindow(hwnd):
        raise OSError('The selected window is no longer open.')


def _show_window(hwnd: int, action: str):
    user = _user32()
    _check_window(user, hwnd)
    commands = {'minimise': 6, 'maximise': 3, 'restore': 9}
    if not user.ShowWindowAsync(hwnd, commands[action]):
        raise OSError('Windows rejected the window state request.')
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        _check_window(user, hwnd)
        iconic, zoomed = bool(user.IsIconic(hwnd)), bool(user.IsZoomed(hwnd))
        if ((action == 'minimise' and iconic) or (action == 'maximise' and zoomed) or
                (action == 'restore' and not iconic and not zoomed)):
            return
        time.sleep(0.02)
    raise OSError('The application did not reach the requested window state.')


def _focus_window(hwnd: int) -> bool:
    user = _user32()
    _check_window(user, hwnd)
    if user.IsIconic(hwnd):
        _show_window(hwnd, 'restore')
    if not user.SetForegroundWindow(hwnd):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        current = kernel.GetCurrentThreadId()
        foreground = user.GetForegroundWindow()
        other = user.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(other and other != current and user.AttachThreadInput(current, other, True))
        try:
            user.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user.AttachThreadInput(current, other, False)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if user.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return False


def _close_window(hwnd: int):
    user = _user32()
    _check_window(user, hwnd)
    if not user.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE, application owns save prompts
        raise ctypes.WinError(ctypes.get_last_error())


def control_window(action: str, target: str = '', *, process_only: bool = False) -> dict:
    if action not in {'list', 'close', 'focus', 'minimise', 'maximise', 'restore'}:
        raise ValueError(f'Unsupported window action: {action}')
    windows = list_windows()
    if action == 'list':
        return {'windows': [asdict(window) for window in windows]}
    window = resolve_window(target, windows, process_only=process_only)
    if action == 'close':
        _close_window(window.hwnd)
    elif action == 'focus':
        if not _focus_window(window.hwnd):
            raise OSError('Windows did not allow this application to take foreground focus.')
    else:
        _show_window(window.hwnd, action)
    debug_log(f'Window {action} request completed.', 'windows')
    return {'action': 'close_requested' if action == 'close' else action, 'hwnd': window.hwnd,
            'process': window.process}


def _window_state(hwnd: int) -> str:
    user = _user32()
    _check_window(user, hwnd)
    if user.IsIconic(hwnd):
        return 'minimised'
    return 'maximised' if user.IsZoomed(hwnd) else 'normal'


def _outer_rect(hwnd: int) -> displays.Rectangle:
    """The window rectangle including its invisible resize borders."""
    user = _user32()
    _check_window(user, hwnd)
    rect = wintypes.RECT()
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.left, rect.top, rect.right, rect.bottom


def _visible_rect(hwnd: int) -> displays.Rectangle:
    """The frame the user sees (DWM extended bounds), falling back to the outer rectangle."""
    rect = wintypes.RECT()
    try:
        hr = _dwmapi().DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    except OSError:
        hr = -1
    if hr == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    return _outer_rect(hwnd)


def _move_outer(hwnd: int, rect: displays.Rectangle) -> None:
    user = _user32()
    _check_window(user, hwnd)
    left, top, right, bottom = rect
    flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
    if not user.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, flags):
        raise OSError('Windows rejected the window position request.')


def _window_monitor(hwnd: int) -> str:
    return displays.monitor_device_for_window(hwnd)


def _close_to(actual, expected, tolerance=PLACEMENT_TOLERANCE_PX) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def validate_placement(monitor: displays.Monitor, rectangle, state: str) -> displays.Rectangle | None:
    """Reject impossible placements before anything is launched or moved."""
    if state not in ('restore', 'maximise'):
        raise ValueError(f'Unsupported placement state: {state}')
    if state == 'maximise' and rectangle is not None:
        raise ValueError('A rectangle cannot be combined with maximise.')
    if rectangle is None:
        return None
    if (not isinstance(rectangle, (list, tuple)) or len(rectangle) != 4 or
            not all(displays._real_number(value) for value in rectangle)):
        raise ValueError('A rectangle is four finite numbers: left, top, right and bottom.')
    left, top, right, bottom = (round(value) for value in rectangle)
    if right <= left or bottom <= top:
        raise ValueError('A rectangle needs a positive width and height.')
    work = monitor.work_area
    margin = PLACEMENT_TOLERANCE_PX
    if left < work[0] - margin or top < work[1] - margin or right > work[2] + margin or bottom > work[3] + margin:
        raise ValueError('The rectangle does not lie within the destination display.')
    return left, top, right, bottom


def _preserved_rectangle(hwnd: int, monitor: displays.Monitor) -> displays.Rectangle:
    """Keep the window's size, clamped into the work area. A window already on
    the destination stays nearby; one arriving from elsewhere is centred."""
    left, top, right, bottom = _visible_rect(hwnd)
    work_left, work_top, work_right, work_bottom = monitor.work_area
    width = min(right - left, work_right - work_left)
    height = min(bottom - top, work_bottom - work_top)
    if _window_monitor(hwnd) == monitor.device:
        x = min(max(left, work_left), work_right - width)
        y = min(max(top, work_top), work_bottom - height)
    else:
        x = work_left + (work_right - work_left - width) // 2
        y = work_top + (work_bottom - work_top - height) // 2
    return x, y, x + width, y + height


def _wait_until(condition, deadline: float, limit: float = _SETTLE_SEC) -> bool:
    """Poll briefly: windows apply moves and DPI changes asynchronously."""
    end = min(deadline, time.monotonic() + limit)
    while True:
        if condition():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _settle_on(hwnd: int, target: displays.Rectangle, deadline: float) -> displays.Rectangle:
    """Move the visible frame onto ``target``, compensating for invisible borders.

    A move between monitors of different DPI makes the window rescale and change
    its borders, so each attempt measures the borders afresh."""
    actual = _visible_rect(hwnd)
    for _ in range(_PLACEMENT_ATTEMPTS):
        if time.monotonic() >= deadline:
            raise TimeoutError('Window placement did not finish in time.')
        visible, outer = _visible_rect(hwnd), _outer_rect(hwnd)
        left, top = visible[0] - outer[0], visible[1] - outer[1]
        right, bottom = outer[2] - visible[2], outer[3] - visible[3]
        _move_outer(hwnd, (target[0] - left, target[1] - top, target[2] + right, target[3] + bottom))
        _wait_until(lambda: _close_to(_visible_rect(hwnd), target), deadline)
        actual = _visible_rect(hwnd)
        if _close_to(actual, target):
            return actual
    if time.monotonic() >= deadline:
        raise TimeoutError('Window placement did not finish in time.')
    size_off = (abs((actual[2] - actual[0]) - (target[2] - target[0])) > PLACEMENT_TOLERANCE_PX or
                abs((actual[3] - actual[1]) - (target[3] - target[1])) > PLACEMENT_TOLERANCE_PX)
    if size_off:
        raise OSError("The application's minimum or maximum size prevents this placement.")
    raise OSError('The application did not reach the requested position.')


def place_window(hwnd: int, monitor: displays.Monitor, rectangle: displays.Rectangle | None = None,
                 state: str = 'restore', *, deadline: float | None = None) -> dict:
    """Move a window onto ``monitor`` and verify where it actually ended up.

    ``rectangle`` is the visible frame in physical pixels inside the work area; without it the
    window keeps its size, clamped into the work area. ``state='maximise'`` fills the work area
    of ``monitor``. ``deadline`` is a ``time.monotonic()`` instant shared with the caller.
    """
    target = validate_placement(monitor, rectangle, state)
    if deadline is None:
        deadline = time.monotonic() + _PLACEMENT_BUDGET_SEC
    with displays.per_monitor_dpi():
        if _window_state(hwnd) != 'normal':
            _show_window(hwnd, 'restore')
        landed = _settle_on(hwnd, target or _preserved_rectangle(hwnd, monitor), deadline)
        if state == 'maximise':
            _show_window(hwnd, 'maximise')
            if not _wait_until(lambda: _close_to(_visible_rect(hwnd), monitor.work_area), deadline):
                raise OSError('The application did not fill the destination display.')
            landed = _visible_rect(hwnd)
        elif _window_state(hwnd) != 'normal':
            raise OSError('The application did not stay in its restored state.')
        if _window_monitor(hwnd) != monitor.device:
            raise OSError('The window did not end up on the requested display.')
    debug_log(f'Window placement verified ({state}).', 'windows')
    return {'hwnd': hwnd, 'monitor': monitor.device, 'rectangle': list(landed), 'state': state}


def place_target_window(target: str, monitor: displays.Monitor, rectangle: displays.Rectangle | None = None,
                        state: str = 'restore', *, process_only: bool = False) -> dict:
    """Resolve one open window like the other window actions, then place it."""
    validate_placement(monitor, rectangle, state)
    window = resolve_window(target, list_windows(), process_only=process_only)
    placed = place_window(window.hwnd, monitor, rectangle, state)
    return {'action': 'placed', 'hwnd': window.hwnd, 'process': window.process, 'pid': window.pid,
            'monitor': placed['monitor'], 'rectangle': placed['rectangle'], 'state': placed['state']}

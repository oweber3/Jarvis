"""Virtual desktops through ``pyvda`` (the undocumented shell COM interfaces).

``pyvda`` carries the interface identifiers for each Windows build, including 24H2 (build 26100
and later). If it is missing or the shell does not answer, every action reports that
virtual-desktop control is unavailable; nothing falls back to sending keyboard shortcuts.
Desktops are numbered from 1, in the order the shell lists them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar

from ...debug import debug_log
from ._bounded import run_bounded

CALL_TIMEOUT_SEC = 5.0
T = TypeVar('T')


class VirtualDesktopError(OSError):
    """Virtual desktops could not be read or changed."""


@dataclass(frozen=True)
class DesktopState:
    count: int
    current: int


class Backend(Protocol):
    def desktop_count(self) -> int: ...
    def current(self) -> int: ...
    def go(self, number: int) -> None: ...
    def create(self) -> int: ...
    def remove(self, number: int) -> None: ...
    def move(self, hwnd: int, number: int) -> None: ...


class _PyvdaBackend:
    def __init__(self):
        try:
            import pyvda
        except ImportError as exc:
            raise VirtualDesktopError('Virtual desktop control needs the pyvda package.') from exc
        self._pyvda = pyvda

    def desktop_count(self) -> int:
        return len(self._pyvda.get_virtual_desktops())

    def current(self) -> int:
        return self._pyvda.VirtualDesktop.current().number

    def go(self, number: int) -> None:
        self._pyvda.VirtualDesktop(number).go()

    def create(self) -> int:
        return self._pyvda.VirtualDesktop.create().number

    def remove(self, number: int) -> None:
        self._pyvda.VirtualDesktop(number).remove()

    def move(self, hwnd: int, number: int) -> None:
        self._pyvda.AppView(hwnd).move(self._pyvda.VirtualDesktop(number))


def _backend() -> Backend:
    return _PyvdaBackend()


def _on_com_thread(action: Callable[[Backend], T]) -> T:
    def call() -> T:
        com = None
        try:
            import comtypes
            comtypes.CoInitialize()
            com = comtypes
        except ImportError:
            pass
        try:
            return action(_backend())
        finally:
            if com is not None:
                import gc
                gc.collect()
                com.CoUninitialize()

    try:
        return run_bounded(call, CALL_TIMEOUT_SEC)
    except TimeoutError as exc:
        raise VirtualDesktopError('The virtual desktop service did not respond in time.') from exc
    except (ValueError, VirtualDesktopError):
        raise
    except Exception as exc:  # noqa: BLE001 - COM raises many types
        debug_log(f'virtual desktop call failed ({type(exc).__name__}).', 'windows')
        raise VirtualDesktopError('Virtual desktop control is not available on this system.') from exc


def _state(backend: Backend) -> DesktopState:
    return DesktopState(backend.desktop_count(), backend.current())


def _resolve(target: str, backend: Backend, *, allow_current: bool = False) -> int:
    """A desktop number, or ``next`` / ``previous`` relative to the current one. Never wraps."""
    count, current = backend.desktop_count(), backend.current()
    wanted = str(target or '').strip().casefold()
    if not wanted and allow_current:
        return current
    if wanted == 'next':
        number = current + 1
    elif wanted == 'previous':
        number = current - 1
    elif wanted.isascii() and wanted.isdigit():
        number = int(wanted)
    else:
        raise ValueError(f'A desktop number from 1 to {count}, or next or previous, is required.')
    if not 1 <= number <= count:
        raise ValueError(f'There is no such desktop. Desktops are numbered 1 to {count}; you are on {current}.')
    return number


def state() -> DesktopState:
    return _on_com_thread(_state)


def switch(target: str) -> DesktopState:
    def go(backend: Backend) -> DesktopState:
        number = _resolve(target, backend)
        if number != backend.current():
            backend.go(number)
        return _state(backend)

    result = _on_com_thread(go)
    debug_log('Virtual desktop switched.', 'windows')
    return result


def create() -> DesktopState:
    """Create a desktop and switch to it, as Windows' own shortcut does."""
    def make(backend: Backend) -> DesktopState:
        backend.go(backend.create())
        return _state(backend)

    result = _on_com_thread(make)
    debug_log('Virtual desktop created.', 'windows')
    return result


def close(target: str = '') -> DesktopState:
    """Close a desktop (the current one without a target). Windows on it move to a neighbour; none is closed."""
    def remove(backend: Backend) -> DesktopState:
        if backend.desktop_count() < 2:
            raise ValueError('The last virtual desktop cannot be closed.')
        backend.remove(_resolve(target, backend, allow_current=True))
        return _state(backend)

    result = _on_com_thread(remove)
    debug_log('Virtual desktop closed.', 'windows')
    return result


def move_window(hwnd: int, target: str) -> dict:
    def move(backend: Backend) -> dict:
        number = _resolve(target, backend)
        backend.move(int(hwnd), number)
        return {'hwnd': int(hwnd), 'desktop': number}

    result = _on_com_thread(move)
    debug_log('Window moved to another virtual desktop.', 'windows')
    return result

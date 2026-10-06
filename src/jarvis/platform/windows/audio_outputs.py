"""Playback devices and the default output.

Lists active render endpoints and switches the default through ``IPolicyConfig`` (via
``pycaw``). The switch sets the console and multimedia roles, which is what Windows' own
"default device" setting changes; the communications role is left alone so call audio does not
move with it. COM is initialised on the bounded worker that uses it.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Protocol, TypeVar

from ...debug import debug_log
from ._bounded import run_bounded

CALL_TIMEOUT_SEC = 6.0
T = TypeVar('T')


class AudioOutputError(RuntimeError):
    """The playback devices could not be read or changed."""


@dataclass(frozen=True)
class OutputDevice:
    id: str
    name: str
    default: bool = False


class Endpoints(Protocol):
    def list(self) -> list[OutputDevice]: ...
    def set_default(self, device_id: str) -> None: ...


class _PycawEndpoints:
    def list(self) -> list[OutputDevice]:
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities
        default = AudioUtilities.GetSpeakers()
        default_id = default.id if default is not None else ''
        devices = AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        return [OutputDevice(device.id, device.FriendlyName or '', device.id == default_id) for device in devices]

    def set_default(self, device_id: str) -> None:
        from pycaw.constants import ERole
        from pycaw.pycaw import AudioUtilities
        AudioUtilities.SetDefaultDevice(device_id, [ERole.eConsole, ERole.eMultimedia])


def _endpoints() -> Endpoints:
    return _PycawEndpoints()


def _on_com_thread(action: Callable[[Endpoints], T]) -> T:
    def call() -> T:
        com = None
        try:
            import comtypes
            comtypes.CoInitialize()
            com = comtypes
        except ImportError:
            pass  # not Windows, or an injected backend
        try:
            return action(_endpoints())
        finally:
            if com is not None:
                import gc
                gc.collect()
                com.CoUninitialize()

    try:
        return run_bounded(call, CALL_TIMEOUT_SEC)
    except TimeoutError as exc:
        raise AudioOutputError('The audio devices did not respond in time.') from exc
    except (ValueError, AudioOutputError):
        raise
    except Exception as exc:  # noqa: BLE001 - COM raises many types
        debug_log(f'audio output call failed ({type(exc).__name__}).', 'windows')
        raise AudioOutputError('Could not access the audio devices.') from exc


def list_outputs() -> list[OutputDevice]:
    return _on_com_thread(lambda endpoints: endpoints.list())


def _words(value: str) -> set[str]:
    return set(re.findall(r'\w+', value.casefold(), re.UNICODE))


def resolve_output(query: str, devices: list[OutputDevice], aliases: dict) -> OutputDevice:
    """Resolve a configured alias, exact name or whole-word subset of one device name.

    Nothing is guessed: an unknown or ambiguous name lists the available devices."""
    wanted = str(query or '').strip().casefold()
    if not wanted:
        raise ValueError('An audio output name is required.')
    alias_map = {key.strip().casefold(): value for key, value in aliases.items()
                 if isinstance(key, str) and isinstance(value, str)}
    wanted = alias_map.get(wanted, wanted).strip().casefold()
    exact = [device for device in devices if device.name.casefold() == wanted]
    loose = [device for device in devices if _words(wanted) and _words(wanted) <= _words(device.name)]
    matches = exact or loose
    available = ', '.join(device.name for device in devices) or 'none'
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f'No active audio output matches that. Available outputs: {available}')
    raise ValueError('Several audio outputs match: ' + ', '.join(device.name for device in matches))


def set_default_output(query: str, aliases: dict) -> OutputDevice:
    """Make one output the default and report the device Windows now uses."""

    def switch(endpoints: Endpoints) -> OutputDevice:
        target = resolve_output(query, endpoints.list(), aliases)
        if not target.default:
            endpoints.set_default(target.id)
        now = next((device for device in endpoints.list() if device.id == target.id), None)
        if now is None or not now.default:
            raise AudioOutputError('Windows did not switch the default audio output.')
        return now

    device = _on_com_thread(switch)
    debug_log('Default audio output changed.', 'windows')
    return device

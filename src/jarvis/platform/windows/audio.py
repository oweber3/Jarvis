"""System volume and mute for the default Windows playback device.

Backed by the Core Audio ``IAudioEndpointVolume`` interface through ``pycaw``.
COM is initialised per worker thread and every call is time-bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar

from ...debug import debug_log
from ._bounded import run_bounded

CALL_TIMEOUT_SEC = 5.0

T = TypeVar("T")


class AudioError(RuntimeError):
    """The audio device could not be read or changed."""


@dataclass(frozen=True)
class VolumeState:
    percent: int
    muted: bool


class VolumeEndpoint(Protocol):
    def get_scalar(self) -> float: ...
    def set_scalar(self, value: float) -> None: ...
    def get_mute(self) -> bool: ...
    def set_mute(self, muted: bool) -> None: ...


class _PycawEndpoint:
    def __init__(self, volume, device=None) -> None:
        self._volume = volume
        self._device = device

    def get_scalar(self) -> float:
        return float(self._volume.GetMasterVolumeLevelScalar())

    def set_scalar(self, value: float) -> None:
        self._volume.SetMasterVolumeLevelScalar(value, None)

    def get_mute(self) -> bool:
        return bool(self._volume.GetMute())

    def set_mute(self, muted: bool) -> None:
        self._volume.SetMute(1 if muted else 0, None)

    def close(self) -> None:
        self._volume = None
        if self._device is not None:
            if hasattr(self._device, "_dev"):
                self._device._dev = None
            self._device = None


def open_default_endpoint() -> VolumeEndpoint:
    """Open the default speakers' volume interface (call on the COM thread)."""
    from pycaw.pycaw import AudioUtilities

    device = AudioUtilities.GetSpeakers()
    return _PycawEndpoint(device.EndpointVolume, device=device)


def _state(endpoint: VolumeEndpoint) -> VolumeState:
    return VolumeState(
        percent=round(endpoint.get_scalar() * 100),
        muted=bool(endpoint.get_mute()),
    )


def _on_device(action: Callable[[VolumeEndpoint], T]) -> T:
    """Run ``action`` against the endpoint on a COM-initialised bounded thread."""

    def _call() -> T:
        com = None
        try:
            import comtypes

            comtypes.CoInitialize()
            com = comtypes
        except ImportError:
            pass  # non-Windows or test environment: endpoint is injected
        endpoint = None
        try:
            endpoint = open_default_endpoint()
            return action(endpoint)
        finally:
            if endpoint is not None and hasattr(endpoint, "close"):
                try:
                    endpoint.close()
                except Exception as exc:
                    debug_log(f"Error closing audio endpoint: {exc}", "windows")
            endpoint = None
            if com is not None:
                import gc

                gc.collect()
                com.CoUninitialize()

    try:
        return run_bounded(_call, CALL_TIMEOUT_SEC)
    except TimeoutError as exc:
        debug_log(f"audio call timed out: {exc}", "windows")
        raise AudioError("The audio device did not respond in time.") from exc
    except Exception as exc:
        debug_log(f"audio call failed: {exc}", "windows")
        raise AudioError(f"Could not access the audio device: {exc}") from exc


def get_volume() -> VolumeState:
    return _on_device(_state)


def set_volume(percent: int) -> VolumeState:
    """Set the master level to ``percent`` (0 to 100 inclusive)."""
    if not 0 <= percent <= 100:
        raise ValueError(f"volume percent must be between 0 and 100, got {percent}")

    def _apply(endpoint: VolumeEndpoint) -> VolumeState:
        endpoint.set_scalar(percent / 100)
        return _state(endpoint)

    return _on_device(_apply)


def change_volume(delta_percent: int) -> VolumeState:
    """Move the master level by ``delta_percent``, clamped to 0 through 100."""

    def _apply(endpoint: VolumeEndpoint) -> VolumeState:
        current = round(endpoint.get_scalar() * 100)
        endpoint.set_scalar(max(0, min(100, current + delta_percent)) / 100)
        return _state(endpoint)

    return _on_device(_apply)


def set_muted(muted: bool) -> VolumeState:
    def _apply(endpoint: VolumeEndpoint) -> VolumeState:
        endpoint.set_mute(muted)
        return _state(endpoint)

    return _on_device(_apply)

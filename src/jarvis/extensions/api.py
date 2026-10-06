"""What a local extension receives: ``ExtensionAPI``, ``SettingField`` and the voice output protocols.

See ``extensions.spec.md``. An extension's ``register(api)`` only declares things; work that touches devices,
threads or the network belongs in ``api.on_start``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

if TYPE_CHECKING:
    from ..assistant_state import AssistantState
    from ..tools.base import Tool

SETTING_TYPES = ("bool", "int", "float", "str", "choice", "list")


@dataclass(frozen=True)
class SettingField:
    """One ``config.json`` key an extension owns, shown on its Settings page."""

    key: str
    label: str
    description: str
    type: str  # one of SETTING_TYPES
    default: Any
    choices: Optional[Sequence[Tuple[str, str]]] = None  # (value, display) for "choice"
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    suffix: Optional[str] = None
    nullable: bool = False


class VoiceSink(Protocol):
    """One reply's audio on its way to a voice output. Calls never block the caller."""

    def write(self, pcm: bytes) -> None: ...

    def end(self) -> None: ...

    def abort(self) -> None: ...


class VoiceOutput(Protocol):
    """An extra place Jarvis's voice plays (``extensions.spec.md``, Voice outputs).

    ``warm()`` is optional; when present it is called once on the thread that starts the voice engine."""

    name: str

    def enabled(self) -> bool: ...

    def lead_ms(self) -> int: ...

    def open(self) -> Optional[VoiceSink]: ...

    def convert(self, samples_int16, rate: int) -> bytes: ...

    def scale(self, pcm: bytes, gains) -> bytes: ...


@dataclass
class Registration:
    """Everything one extension declared in ``register``."""

    name: str
    settings_label: str = ""
    settings_fields: List[SettingField] = field(default_factory=list)
    tools: List["Tool"] = field(default_factory=list)
    phrase_files: List[Path] = field(default_factory=list)
    voice_outputs: List[VoiceOutput] = field(default_factory=list)
    on_start: List[Callable[[], None]] = field(default_factory=list)
    on_stop: List[Callable[[], None]] = field(default_factory=list)
    state_callbacks: List[Callable[["AssistantState"], None]] = field(default_factory=list)


class ExtensionAPI:
    """The only object an extension's ``register(api)`` receives."""

    def __init__(self, name: str, cfg: Any, raw_config: Dict[str, Any], registration: Registration) -> None:
        self.name = name
        self._cfg = cfg
        self._raw = raw_config
        self._reg = registration

    @property
    def config(self) -> Any:
        """The loaded settings Jarvis itself uses (read-only by convention)."""
        return self._cfg

    def setting(self, key: str) -> Any:
        """The value of one of this extension's declared settings, or its default."""
        for declared in self._reg.settings_fields:
            if declared.key == key:
                return self._raw.get(key, declared.default)
        raise KeyError(f"{key} is not a setting this extension declared")

    def add_settings(self, label: str, fields: Sequence[SettingField]) -> None:
        for item in fields:
            if item.type not in SETTING_TYPES:
                raise ValueError(f"setting {item.key} has unknown type {item.type}")
        self._reg.settings_label = label
        self._reg.settings_fields.extend(fields)

    def add_tool(self, tool: "Tool") -> None:
        self._reg.tools.append(tool)

    def add_phrases(self, path) -> None:
        self._reg.phrase_files.append(Path(path))

    def add_voice_output(self, output: VoiceOutput) -> None:
        self._reg.voice_outputs.append(output)

    def on_start(self, callback: Callable[[], None]) -> None:
        self._reg.on_start.append(callback)

    def on_stop(self, callback: Callable[[], None]) -> None:
        self._reg.on_stop.append(callback)

    def subscribe_state(self, callback: Callable[["AssistantState"], None]) -> None:
        """Receive every assistant state change between start and stop (on the caller's thread: be quick)."""
        self._reg.state_callbacks.append(callback)

    def record_device(self, device: str, tool: str, last_action: str) -> None:
        """Make ``device`` (the name Jarvis shows) what a follow-up such as "turn it off" is about."""
        from ..memory.desktop_referents import get_desktop_referents

        get_desktop_referents().record_device(device, tool, last_action)

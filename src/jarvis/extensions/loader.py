"""Finding, importing, installing, starting and stopping local extensions (``extensions.spec.md``)."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..debug import debug_log
from .api import ExtensionAPI, Registration, SettingField, VoiceOutput

_NAMESPACE = "jarvis_local_extensions"
_NAME = re.compile(r"[A-Za-z0-9_]+")
_LANGUAGE = re.compile(r"[a-z]{2,3}")
_RULE_KEYS = ("id", "family", "tool", "args", "phrases", "reply")


def default_extensions_dir() -> Path:
    """``extensions/`` at the root of the Jarvis checkout."""
    return Path(__file__).resolve().parents[3] / "extensions"


@dataclass(frozen=True)
class SettingsPage:
    extension: str
    label: str
    fields: Sequence[SettingField]


def _warn(text: str) -> None:
    print(f"🧩 {text}", flush=True)


class LoadedExtensions:
    """The extensions that registered successfully, in load order."""

    def __init__(self, registrations: List[Registration]) -> None:
        self._registrations = registrations
        self._installed: Dict[str, Any] = {}
        self._unsubscribe: List[Callable[[], None]] = []
        self._started = False
        self._stopped = False

    @property
    def names(self) -> List[str]:
        return [r.name for r in self._registrations]

    def voice_outputs(self) -> List[VoiceOutput]:
        return [output for r in self._registrations for output in r.voice_outputs]

    def settings_pages(self) -> List[SettingsPage]:
        return [SettingsPage(r.name, r.settings_label or r.name, tuple(r.settings_fields))
                for r in self._registrations if r.settings_fields]

    def setting_defaults(self) -> Dict[str, Any]:
        return {f.key: f.default for r in self._registrations for f in r.settings_fields}

    def install(self) -> None:
        """Add the extensions' tools to the registry and their phrases to the fast path."""
        from ..fastpath.matcher import set_extension_rules
        from ..tools.registry import BUILTIN_TOOLS

        rules: Dict[str, list] = {}
        for reg in self._registrations:
            accepted = set()
            for tool in reg.tools:
                name = getattr(tool, "name", None)
                if not isinstance(name, str) or not name or name in BUILTIN_TOOLS:
                    _warn(f"Extension {reg.name}: tool {name} skipped, that name is already taken")
                    debug_log(f"extension {reg.name} tool name clash", "extensions")
                    continue
                BUILTIN_TOOLS[name] = tool
                self._installed[name] = tool
                accepted.add(name)
            for path in reg.phrase_files:
                found = _phrase_rules(reg.name, path, accepted)
                if found is not None:
                    rules.setdefault(found[0], []).extend(found[1])
        if rules:
            set_extension_rules(rules)

    def start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        from .. import assistant_state

        for reg in self._registrations:
            started = True
            for callback in reg.on_start:
                try:
                    callback()
                except Exception as exc:
                    started = False
                    _warn(f"Extension {reg.name} did not start: {type(exc).__name__}")
                    debug_log(f"extension {reg.name} start failed: {type(exc).__name__}", "extensions")
            if not started:
                continue
            for callback in reg.state_callbacks:
                self._unsubscribe.append(assistant_state.subscribe(_guarded(reg.name, callback)))

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._started:
            for reg in reversed(self._registrations):
                for callback in reversed(reg.on_stop):
                    try:
                        callback()
                    except Exception as exc:
                        debug_log(f"extension {reg.name} stop failed: {type(exc).__name__}", "extensions")
        from ..fastpath.matcher import set_extension_rules
        from ..tools.registry import BUILTIN_TOOLS

        for name, tool in self._installed.items():
            if BUILTIN_TOOLS.get(name) is tool:
                del BUILTIN_TOOLS[name]
        self._installed.clear()
        set_extension_rules({})
        debug_log("extensions stopped", "extensions")


def _guarded(name: str, callback):
    def notify(state) -> None:
        try:
            callback(state)
        except Exception as exc:
            debug_log(f"extension {name} state callback failed: {type(exc).__name__}", "extensions")

    return notify


def _phrase_rules(extension: str, path: Path, tools: set):
    """``(language, rules)`` from one phrase file, or None (with a warning) when it cannot be used."""
    from ..fastpath.matcher import rule_problem

    language = path.stem
    try:
        if not _LANGUAGE.fullmatch(language):
            raise ValueError("the file name must be a language code such as en.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data["rules"]
        for rule in rules:
            if any(key not in rule for key in _RULE_KEYS):
                raise ValueError("a rule is missing a field")
            if rule["tool"] not in tools:
                raise ValueError(f"a rule names {rule['tool']}, which this extension did not add")
            if not isinstance(rule["args"], dict):
                raise ValueError("a rule has malformed args")
            problem = rule_problem(language, rule)
            if problem is not None:
                raise ValueError(problem)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        _warn(f"Extension {extension}: phrases in {path.name} ignored ({reason})")
        debug_log(f"extension {extension} phrases ignored: {type(exc).__name__}", "extensions")
        return None
    return language, list(rules)


def _forget(name: str) -> None:
    """Drop an extension's modules, so the next load imports it afresh."""
    package = f"{_NAMESPACE}.{name}"
    for loaded in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[loaded]


def import_extension(name: str, folder: Path):
    """The extension's package, imported once per folder under a private namespace."""
    package = f"{_NAMESPACE}.{name}"
    existing = sys.modules.get(package)
    if existing is not None and list(getattr(existing, "__path__", [])) == [str(folder)]:
        return existing
    _forget(name)
    if _NAMESPACE not in sys.modules:
        namespace = types.ModuleType(_NAMESPACE)
        namespace.__path__ = []
        sys.modules[_NAMESPACE] = namespace
    spec = importlib.util.spec_from_file_location(package, folder / "__init__.py",
                                                  submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        _forget(name)
        raise
    return module


def load_extensions(cfg: Any, raw: Optional[Dict[str, Any]] = None, *, quiet: bool = False) -> LoadedExtensions:
    """Import and register every enabled extension. Nothing is installed or started yet.

    ``raw`` is the merged ``config.json`` contents (loaded when omitted); ``quiet`` skips the summary line."""
    names = list(getattr(cfg, "extensions_enabled", None) or [])
    if not names:
        return LoadedExtensions([])
    if raw is None:
        from ..config import load_config
        raw = load_config()
    folder = Path(getattr(cfg, "extensions_dir", "") or default_extensions_dir()).expanduser()

    registrations: List[Registration] = []
    for name in names:
        reason = None
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            reason = "the name may only use letters, digits and underscores"
        elif name in (r.name for r in registrations):
            reason = "it is listed twice"
        elif not (folder / name / "__init__.py").is_file():
            reason = "no such extension folder"
        if reason is None:
            registration = Registration(name=name)
            try:
                module = import_extension(name, folder / name)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise AttributeError("it has no register(api) function")
                register(ExtensionAPI(name, cfg, raw, registration))
                registrations.append(registration)
                debug_log(f"extension {name} registered", "extensions")
                continue
            except Exception as exc:
                _forget(name)
                reason = str(exc) if isinstance(exc, AttributeError) else type(exc).__name__
                debug_log(f"extension {name} failed to load: {type(exc).__name__}", "extensions")
        _warn(f"Extension {name} not loaded: {reason}")

    if registrations and not quiet:
        print(f"🧩 Extensions: {', '.join(r.name for r in registrations)}", flush=True)
    return LoadedExtensions(registrations)

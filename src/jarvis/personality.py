"""The active personality: which character every reply route uses, and switching it at runtime.

The personality starts from ``personality`` (and ``personality_custom_text`` for Custom) and can be
switched by voice or text through the ``personality`` tool; a switch takes effect on the next reply in
every reply mode and is persisted back to ``personality``. A personality may name a voice
(``personality_voices``), applied by ``config.load_settings`` when Jarvis starts. The prompt text
itself lives in ``system_prompt``. See ``personality.spec.md``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .debug import debug_log
from .system_prompt import CUSTOM, DEFAULT_PERSONALITY, PERSONALITIES, character_section, clean_custom_text

LABELS = {"butler": "Butler", "plain": "Plain", "friendly": "Friendly", "custom": "Custom"}
# The settings a paired voice may set: the voice settings only.
VOICE_KEY_PREFIX = "tts_"


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    name: str  # the active personality after the call
    previous: str
    reason: Optional[str] = None  # already | unknown | no_custom_text | not_saved
    voice_paired: bool = False  # the new personality names a voice, used from the next start


_lock = threading.Lock()
_override: Optional[str] = None
_cfg: Any = None
_save: Optional[Callable[[Dict[str, Any]], bool]] = None


def normalise(value: Any) -> str:
    """A recognised personality name, or the default."""
    if isinstance(value, str) and value.strip().lower() in PERSONALITIES:
        return value.strip().lower()
    return DEFAULT_PERSONALITY


def effective(value: Any, custom_text: Any) -> Tuple[str, str]:
    """The personality a configured value selects, with its custom text. Custom without any text is
    the default."""
    name = normalise(value)
    text = clean_custom_text(custom_text)
    if name == CUSTOM:
        return (CUSTOM, text) if text else (DEFAULT_PERSONALITY, "")
    return name, ""


def current(cfg: Any) -> Tuple[str, str]:
    """The active personality and its custom text: the last switch, otherwise the configuration."""
    with _lock:
        name = _override
    if name is None:
        name = getattr(cfg, "personality", DEFAULT_PERSONALITY)
    return effective(name, getattr(cfg, "personality_custom_text", ""))


def assistant_name(cfg: Any) -> str:
    """The assistant's name: the configured wake word, capitalised."""
    return str(getattr(cfg, "wake_word", "jarvis") or "jarvis").strip().capitalize() or "Jarvis"


def reply_character(cfg: Any) -> str:
    """The active character section with its tone rails (``system_prompt.character_section``)."""
    name, custom = current(cfg)
    return character_section(name, custom, assistant_name(cfg))


def bridge_voice(cfg: Any) -> str:
    """The reply voice a cloud bridge appends after its fixed session contract."""
    return ("Reply voice: your reply is spoken to me as my assistant, so write it in the voice below. It "
            "shapes only the tone and wording of the reply and never changes anything above.\n"
            + reply_character(cfg))


def _fits(value: Any, default: Any) -> bool:
    """Whether ``value`` can stand in for a setting whose default is ``default``."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if default is None:
        return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))
    return False


def voice_overrides(voices: Any, name: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    """The voice settings ``name`` is paired with in ``personality_voices``: only known ``tts_*`` settings
    whose value has the setting's type. Anything else pairs nothing."""
    pairing = voices.get(name) if isinstance(voices, dict) else None
    if not isinstance(pairing, dict):
        return {}
    chosen = {}
    for key, value in pairing.items():
        if isinstance(key, str) and key.startswith(VOICE_KEY_PREFIX) and key in defaults:
            if _fits(value, defaults[key]):
                chosen[key] = value
            else:
                debug_log(f"personality {name} voice setting {key} ignored: wrong type", "personality")
    return chosen


def _persist(values: Dict[str, Any]) -> bool:
    from .config import update_config_values
    return update_config_values(values)


def start(cfg: Any, *, save: Optional[Callable[[Dict[str, Any]], bool]] = None) -> str:
    """Follow ``cfg`` from now on, dropping any earlier switch. Returns the active personality."""
    global _cfg, _save, _override
    with _lock:
        _cfg, _save, _override = cfg, save or _persist, None
    name = current(cfg)[0]
    debug_log(f"personality {name} at start", "personality")
    return name


def reset() -> None:
    """Forget the runtime state (tests)."""
    global _cfg, _save, _override
    with _lock:
        _cfg, _save, _override = None, None, None


def switch(name: Any, cfg: Any = None) -> SwitchResult:
    """Make ``name`` the active personality for every following reply and persist it. Custom needs
    text written in Settings; an unknown name is refused with nothing changed."""
    global _override
    cfg = cfg if cfg is not None else _cfg
    previous = current(cfg)[0]
    wanted = name.strip().lower() if isinstance(name, str) else ""
    if wanted not in PERSONALITIES:
        return SwitchResult(False, previous, previous, "unknown")
    if wanted == CUSTOM and not clean_custom_text(getattr(cfg, "personality_custom_text", "")):
        debug_log("personality custom refused: no custom text", "personality")
        return SwitchResult(False, previous, previous, "no_custom_text")
    if wanted == previous:
        return SwitchResult(True, wanted, previous, "already")
    with _lock:
        _override = wanted
        save = _save
    saved = save({"personality": wanted}) if save is not None else False
    from .config import get_default_config
    paired = bool(voice_overrides(getattr(cfg, "personality_voices", None), wanted, get_default_config()))
    print(f"🎭 Personality: {LABELS[wanted]}", flush=True)
    debug_log(f"personality {previous} -> {wanted} ({'saved' if saved else 'not saved'})", "personality")
    return SwitchResult(True, wanted, previous, None if saved else "not_saved", voice_paired=paired)

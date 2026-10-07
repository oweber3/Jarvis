"""The models and effort levels the Codex and Claude runtimes report, cleaned for the model picker.

The runtimes' lists are untrusted data: they are bounded, identifiers must be safe, hidden models are
left out and nothing is invented when a field is missing. See ``bridge.spec.md``, Cloud models.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

MAX_MODELS = 128
MAX_EFFORTS = 12
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 200
_DESCRIPTION_SEPARATOR = " · "
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]/]{0,199}$")


@dataclass(frozen=True)
class Effort:
    id: str
    is_default: bool = False
    description: str = ""


@dataclass(frozen=True)
class CloudModel:
    id: str
    name: str
    efforts: Tuple[Effort, ...] = ()
    is_default: bool = False
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "is_default": self.is_default, "description": self.description,
                "efforts": [{"id": e.id, "is_default": e.is_default, "description": e.description}
                            for e in self.efforts]}


def _safe_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _SAFE_ID.match(value) else None


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())
    return cleaned[:limit]


def _records(raw: Any) -> List[Dict[str, Any]]:
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def codex_models(raw: Any) -> List[CloudModel]:
    """Models from Codex's ``model/list``: hidden ones left out, efforts from ``supportedReasoningEfforts``."""
    models: Dict[str, CloudModel] = {}
    for record in _records(raw):
        if record.get("hidden") is True:
            continue
        model_id = _safe_id(record.get("model")) or _safe_id(record.get("id"))
        if model_id is None or model_id in models:
            continue
        default_effort = record.get("defaultReasoningEffort")
        efforts: Dict[str, Effort] = {}
        for entry in record.get("supportedReasoningEfforts") or []:
            effort_id = _safe_id(entry.get("reasoningEffort")) if isinstance(entry, dict) else None
            if effort_id is None or effort_id in efforts or len(efforts) >= MAX_EFFORTS:
                continue
            efforts[effort_id] = Effort(effort_id, is_default=effort_id == default_effort,
                                        description=_clean_text(entry.get("description"), MAX_NAME_CHARS))
        models[model_id] = CloudModel(model_id, _clean_text(record.get("displayName"), MAX_NAME_CHARS) or model_id,
                                      tuple(efforts.values()), is_default=record.get("isDefault") is True,
                                      description=_clean_text(record.get("description"), MAX_DESCRIPTION_CHARS))
        if len(models) >= MAX_MODELS:
            break
    return list(models.values())


def _claude_name(display: str, description: str) -> Tuple[str, str]:
    """Claude Code names a model by its alias ("Sonnet") and puts the version in the description
    ("Sonnet 5.5 · Efficient for routine tasks"). The name is the alias with that version, and the rest of
    the description is kept as the picker's secondary text."""
    head, separator, rest = description.partition(_DESCRIPTION_SEPARATOR)
    if not separator or not head:
        return display, description
    if head.lower().startswith(display.lower()):
        return head, rest
    return f"{display}{_DESCRIPTION_SEPARATOR}{head}", rest


def claude_models(raw: Any) -> List[CloudModel]:
    """Models from Claude Code's ``initialize`` answer: effort levels only for a model that supports effort."""
    models: Dict[str, CloudModel] = {}
    for record in _records(raw):
        model_id = _safe_id(record.get("value"))
        if model_id is None or model_id in models:
            continue
        levels = record.get("supportedEffortLevels")
        efforts: List[Effort] = []
        if record.get("supportsEffort") is not False and isinstance(levels, list):
            for level in levels:
                level_id = _safe_id(level)
                if level_id is not None and all(e.id != level_id for e in efforts) and len(efforts) < MAX_EFFORTS:
                    efforts.append(Effort(level_id))
        name, description = _claude_name(_clean_text(record.get("displayName"), MAX_NAME_CHARS) or model_id,
                                         _clean_text(record.get("description"), MAX_DESCRIPTION_CHARS))
        models[model_id] = CloudModel(model_id, name, tuple(efforts), description=description)
        if len(models) >= MAX_MODELS:
            break
    return list(models.values())


def choose_effort(model: CloudModel, preferred: Optional[str]) -> Optional[str]:
    """The effort to use on ``model``: ``preferred`` when the model offers it, else its default, else its first."""
    if not model.efforts:
        return None
    for effort in model.efforts:
        if effort.id == preferred:
            return effort.id
    return next((e.id for e in model.efforts if e.is_default), model.efforts[0].id)

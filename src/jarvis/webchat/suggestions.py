"""The start screen's suggestion cards. See ``webchat.spec.md``, Start screen.

Cards are built from names only: the owner's routines and workspaces first, then a few generic
examples. The wording of an owner card comes from the fast path's locale file (``suggestions`` block of
``fastpath/phrases/<language>.json``), written so the fast path runs it as a whole-utterance command.
Nothing here matches language patterns, and no model is involved.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..debug import debug_log

MAX_CARDS = 6
MAX_OWNER_CARDS = 4
MAX_ROUTINE_CARDS = 3
MAX_NAME_CHARS = 60
FALLBACK_LANGUAGE = "en"
PHRASES_DIR = Path(__file__).resolve().parents[1] / "fastpath" / "phrases"

# Generic capability examples: interface text, filled into the composer for the owner to adjust and send.
EXAMPLES = (
    "What's using the most RAM?",
    "Set the volume to 30%",
    "Search the web for today's top news",
    "What time is it?",
    "Open a new browser window",
    "What can you do?",
)


def load_templates(languages: Iterable[str]) -> Dict[str, str]:
    """The card wording of the first language with a ``suggestions`` block, else English."""
    for language in [*languages, FALLBACK_LANGUAGE]:
        code = str(language).casefold().replace("_", "-").split("-")[0]
        path = PHRASES_DIR / f"{code}.json"
        if not re.fullmatch(r"[a-z]{2,3}", code) or not path.is_file():
            continue
        try:
            found = json.loads(path.read_text(encoding="utf-8")).get("suggestions")
        except (OSError, ValueError):
            continue
        if isinstance(found, dict) and all(isinstance(found.get(k), str) and "{name}" in found[k]
                                           for k in ("routine", "workspace")):
            return {"routine": found["routine"], "workspace": found["workspace"]}
    debug_log("webchat: no suggestion wording for this language", "webchat")
    return {}


def _usable(name: Any) -> bool:
    return (isinstance(name, str) and bool(name.strip()) and len(name) <= MAX_NAME_CHARS
            and name.isprintable())


def _owner_cards(kind: str, names: Iterable[Any], template: str, limit: int) -> List[Dict[str, str]]:
    found = []
    for name in names:
        if len(found) >= limit:
            break
        if _usable(name):
            found.append({"kind": kind, "text": template.replace("{name}", name.strip()), "action": "send"})
    return found


def build_cards(sources: Dict[str, Any], templates: Dict[str, str], examples: Iterable[str] = EXAMPLES,
                ) -> List[Dict[str, str]]:
    """Routine cards, then workspace cards, then generic examples, at most ``MAX_CARDS`` in all.

    Routine and workspace cards are complete commands and are sent as they stand; examples may need a
    detail, so they only fill the composer."""
    cards: List[Dict[str, str]] = []
    if templates.get("routine"):
        cards += _owner_cards("routine", sources.get("routines", ()), templates["routine"], MAX_ROUTINE_CARDS)
    if templates.get("workspace") and sources.get("workspaces_available"):
        cards += _owner_cards("workspace", sources.get("workspaces", ()), templates["workspace"],
                              MAX_OWNER_CARDS - len(cards))
    owner = len(cards)
    for text in examples:
        if len(cards) >= MAX_CARDS:
            break
        cards.append({"kind": "example", "text": text, "action": "fill"})
    debug_log(f"webchat: start screen with {owner} owner cards and {len(cards) - owner} examples", "webchat")
    return cards

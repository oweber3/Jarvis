"""Names and aliases of the user's own named things (workspaces, routines).

Names compare case-insensitively after Unicode (NFKC) normalisation, and a name or alias that two
definitions both claim is offered for neither. Pure, no I/O.
"""
from __future__ import annotations

import unicodedata
from typing import Mapping, Sequence


def key(text: str) -> str:
    """The comparison form of a name: NFKC-normalised, case-folded, without surrounding space."""
    return unicodedata.normalize('NFKC', text).casefold().strip()


def drop_contested(claims: Mapping[str, Sequence[str]]) -> tuple[dict[str, list[str]], int]:
    """Keep only the names and aliases that one definition claims.

    ``claims`` maps each name to its aliases. A definition whose own name is contested is left out; a
    contested alias is removed from the definition that remains. Returns the kept names with their
    kept aliases, and how many names and aliases were left out."""
    counts: dict[str, int] = {}
    for name, aliases in claims.items():
        for claimed in {key(name), *(key(alias) for alias in aliases)}:
            counts[claimed] = counts.get(claimed, 0) + 1
    contested = {claimed for claimed, count in counts.items() if count > 1}
    kept: dict[str, list[str]] = {}
    dropped = 0
    for name, aliases in claims.items():
        if key(name) in contested:
            dropped += 1
            continue
        kept[name] = [alias for alias in aliases if key(alias) not in contested]
        dropped += len(aliases) - len(kept[name])
    return kept, dropped


def resolve(query, claims: Mapping[str, Sequence[str]]) -> str | None:
    """The canonical name that ``query`` names or is an alias of, or ``None``."""
    wanted = key(query) if isinstance(query, str) else ''
    if not wanted:
        return None
    matches = [name for name, aliases in claims.items() if wanted in {key(name), *(key(alias) for alias in aliases)}]
    return matches[0] if len(matches) == 1 else None

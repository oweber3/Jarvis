"""Windows Settings deep links, from a data file.

Only slugs in ``settings_pages.json`` open; raw URIs and paths are never launched, so a
misheard or injected argument cannot start anything else.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Callable

from ...debug import debug_log


@lru_cache(maxsize=1)
def load_pages() -> dict[str, str]:
    """Slug to ``ms-settings:`` URI."""
    pages = json.loads((Path(__file__).parent / 'settings_pages.json').read_text(encoding='utf-8'))
    return {slug: uri for slug, uri in pages.items() if uri.startswith('ms-settings:')}


def normalise_slug(value: str) -> str:
    return '_'.join(str(value or '').strip().casefold().replace('-', ' ').split())


def open_page(page: str, launch: Callable[[str], None] | None = None) -> dict:
    """Open one settings page through the shell. Success means Windows accepted the request."""
    pages = load_pages()
    slug = normalise_slug(page)
    if slug not in pages:
        raise ValueError('Unknown settings page. Available pages: ' + ', '.join(sorted(pages)))
    (launch or os.startfile)(pages[slug])
    debug_log(f'Settings page opened: {slug}.', 'windows')
    return {'action': 'settings_page_opened', 'page': slug}

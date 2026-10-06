"""Open one web address, optionally in a new browser window placed on a display and zone.

Builds on the workspace browser launcher (``workspaces.open_browser_window``). Imports nothing from
tools, replies or models. See ``apps_paths.spec.md`` (Websites).
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlsplit

from . import workspaces
from ...debug import debug_log

# One deadline covers launch, window discovery and placement, inside the tools' twelve-second limit.
PLACE_BUDGET_SEC = 10.0


def normalise_url(text) -> str:
    """An ``http(s)`` address as given, or a bare host such as ``youtube.com`` as ``https``.

    Every other scheme, local path, address with spaces or credentials, and bare host without a dot
    is rejected, so only web pages are ever opened."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('A web address is required.')
    text = text.strip()
    if any(char.isspace() for char in text) or '\\' in text:
        raise ValueError('That is not a web address.')
    if '://' in text:
        address = text
    elif ':' in text.split('/', 1)[0]:
        # A scheme such as javascript:, data: or mailto:, a drive letter, or a host with a port.
        raise ValueError('Only http and https web addresses can be opened.')
    else:
        address = 'https://' + text
    try:
        parsed = urlsplit(address)
        parsed.port  # an invalid port raises
    except ValueError:
        raise ValueError('That is not a web address.') from None
    if parsed.scheme.casefold() not in ('http', 'https'):
        raise ValueError('Only http and https web addresses can be opened.')
    host = parsed.hostname or ''
    if not host or '@' in parsed.netloc or ('://' not in text and '.' not in host.strip('.')):
        raise ValueError('That is not a web address.')
    return address


def open_website(url: str, browser: str | None = None, placement=None) -> dict:
    """Open ``url``. ``placement`` is ``(monitor, rectangle, state)`` for a new placed window.

    Without placement the address goes to the named browser, or to Windows' association for it (the
    user's default browser). With placement the destination is validated, then one new browser window
    is launched and placed; ``workspaces.BrowserWindowError`` reports an accepted launch that was not
    placed. Results and logs never contain the address."""
    from . import windows_mgmt as wm
    address = normalise_url(url)
    if placement is None:
        if browser:
            chosen = workspaces.find_browser(browser)
            workspaces._spawn([chosen.executable, address])
            name = chosen.name
        else:
            os.startfile(address)
            name = 'default'
        debug_log(f'Website open requested ({name} browser).', 'windows')
        return {'action': 'website_opened', 'browser': name}
    monitor, rectangle, state = placement
    wm.validate_placement(monitor, rectangle, state)
    chosen = workspaces.find_browser(browser)
    if not workspaces._launch_lock.acquire(blocking=False):
        raise ValueError('A browser window is already opening. Wait for it to finish.')
    try:
        debug_log(f'Website window launch requested ({chosen.name}); waiting to place it.', 'windows')
        placed = workspaces.open_browser_window(chosen, [address], monitor, rectangle, state,
                                                time.monotonic() + PLACE_BUDGET_SEC)
    except workspaces.BrowserWindowError as exc:
        debug_log(f'Website window not placed ({exc.data["placement"]}).', 'windows')
        exc.data['browser'] = chosen.name
        raise
    finally:
        workspaces._launch_lock.release()
    debug_log('Website window opened and placed.', 'windows')
    return {'action': 'website_opened_and_placed', 'browser': chosen.name, 'hwnd': placed['hwnd'],
            'process': placed['process'], 'monitor': placed['monitor'], 'rectangle': placed['rectangle'],
            'state': placed['state']}

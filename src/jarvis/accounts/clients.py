"""The OAuth apps this build of Jarvis signs in with ("Sign in with Microsoft" and "Sign in with Google").

These are public desktop clients: an installed app cannot keep a secret, so the IDs (and Google's
installed-app client secret, which Google documents as not confidential) ship with Jarvis and every
sign-in is protected by PKCE instead. Each value can be overridden in ``config.json`` under the same key.
An empty value turns that sign-in button off. See ``docs/OAUTH_SETUP.md`` for how to register the apps.
"""
from __future__ import annotations

from typing import Dict

# ╔══════════════════════════════════════════════════════════════════════════════════════════════╗
# ║ BUILT-IN OAUTH CLIENTS: paste the registered IDs here (docs/OAUTH_SETUP.md). Empty = button off ║
# ╚══════════════════════════════════════════════════════════════════════════════════════════════╝
BUILT_IN_OAUTH_CLIENTS: Dict[str, str] = {
    # Microsoft Entra app registration: Application (client) ID.
    "microsoft_oauth_client_id": "",
    # Google Cloud OAuth client of type "Desktop app": client ID and its (non-confidential) client secret.
    "google_oauth_client_id": "",
    "google_oauth_client_secret": "",
}

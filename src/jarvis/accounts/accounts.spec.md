# Connected accounts (sign-in for calendar and email)

How the user connects the calendars and mailboxes that the read-only `calendar` and `email` tools read (`agenda/agenda.spec.md`, `mail/mail.spec.md`), without a terminal and without pasting secrets where it can be avoided: **Sign in with Microsoft**, **Sign in with Google**, Gmail with an app password, any IMAP mailbox, a pasted calendar link, and classic Outlook on this PC. The page that offers them is Settings > Calendar & Email (`desktop_app/settings_window.spec.md`); the setup commands use the same operations.

## Boundaries

- Explicitly configured integrations only: nothing is contacted until the user signs in or adds an account, and the offline baseline (classic Outlook, saved calendar copies) needs no cloud service.
- Read only. The Microsoft sign-in asks for `offline_access User.Read` plus `Calendars.Read` and/or `Mail.Read` (only what the user ticked); the Google sign-in asks for `https://www.googleapis.com/auth/calendar.readonly` only. Gmail is never requested through the Google sign-in (a restricted scope); Gmail uses an app password over IMAP.
- Secrets live only in Windows Credential Manager: sign-ins as `oauth_login:<login id>`, IMAP passwords as `email_password:<name>`, calendar links as `calendar_url:<name>` (`credentials.py`). `config.json` holds entries that refer to them, plus the signed-in address for display.
- No telemetry. Debug logs (tag `accounts`) carry provider, kinds, counts, reason codes and HTTP statuses only: never codes, tokens, addresses, account names, event or message text.

## Layout

| Module | Responsibility |
|--------|----------------|
| `accounts/clients.py` | `BUILT_IN_OAUTH_CLIENTS`: the one marked constant holding this build's Microsoft client ID and Google client ID and secret |
| `accounts/oauth.py` | Providers, scopes, PKCE, the loopback receiver, `authorize` (browser sign-in) and `refresh` |
| `accounts/logins.py` | Stored sign-in records and the shared per-sign-in session that hands out access tokens |
| `accounts/http.py` | HTTPS transport (`(status, json)`, status 0 for a network failure), `bearer_get`, the paging same-origin check, API base addresses |
| `accounts/manage.py` | `AccountStore`: list, connect, test and remove, used by Settings and the setup commands |
| `desktop_app/accounts_page.py` | The Settings page and its add dialogs |

## OAuth clients

`microsoft_oauth_client_id`, `google_oauth_client_id` and `google_oauth_client_secret` default to `BUILT_IN_OAUTH_CLIENTS` and can be overridden in `config.json` (a power user's own app registration). They have no Settings rows. An empty client ID turns that sign-in off: its button is disabled and says the sign-in is not set up in this build. These are public desktop clients; Google documents an installed app's client secret as not confidential, so it ships like the ID. Registering the apps is described in `docs/OAUTH_SETUP.md`.

## Browser sign-in

OAuth 2.0 authorisation code with PKCE (RFC 7636, `S256`) through the system browser and a loopback redirect (RFC 8252):

1. A fresh code verifier (64 URL-safe characters), its S256 challenge and a 256-bit `state`.
2. A one-shot HTTP receiver binds `127.0.0.1` on a random free port, with `SO_EXCLUSIVEADDRUSE` on Windows and no address reuse, so no other program can share the port. The redirect address is `http://localhost:<port>` for Microsoft (matching the registered `http://localhost`; Microsoft ignores the port for loopback) and `http://127.0.0.1:<port>` for Google.
3. The authorisation page opens in the default browser: Microsoft's multi-tenant `common` authority (personal, work and school accounts) with `prompt=select_account`; Google with `access_type=offline` and `prompt=consent` so a refresh token is issued.
4. Each connection is served on its own daemon thread, so a slow or silent client never holds up the wait, Cancel or the time limit (a connection that stops sending is dropped after 5 seconds). Requests to other paths get 404. On `/`, `state` is compared in constant time: a request without the expected state (forged, stale, or sent by another program or a web page) gets 400 and is otherwise ignored, so it can neither inject a code nor cut the sign-in short. The first request with the right state decides: a code, a provider error (`access_denied` is "declined") or no code. Pages are fixed text (signed in, or not) with `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `nosniff` and a `default-src 'none'` content security policy; nothing from the request is echoed. The request line is never logged.
5. Waiting ends with that decision, on Cancel (checked at least every 0.1 s) or after 3 minutes (reported as `state_mismatch` when only wrong-state requests arrived, else `timeout`); the receiver is then shut down and the port freed. Closing Settings cancels a sign-in that is still waiting.
6. The code is exchanged with the verifier and the same redirect address (Google also sends the client secret). A response without a refresh token is refused. When the provider lists the granted scopes, every data scope asked for must be there (Google lets the user untick one), else `scope_missing`.

Failures are `OAuthError` codes: `not_configured`, `browser`, `cancelled`, `timeout`, `state_mismatch`, `denied`, `provider_error`, `no_code`, `unreachable`, `token_refused`, `scope_missing`; each has a plain-language message (`manage.SIGN_IN_ERRORS`).

## Stored sign-ins and access tokens

- A sign-in record holds the provider, the client ID (and Google's client secret) it was issued to, the scopes, the tenant and the refresh token, as JSON under `oauth_login:<provider>-<8 hex>`. Keeping the client means a sign-in keeps working if a later build ships a different client ID.
- `logins.session_for(login_id)` is shared by every calendar and mailbox of that sign-in. It keeps the access token in memory only, refreshes it 60 seconds before it expires, one refresh at a time, and stores a rotated refresh token at once.
- A refresh refused by the provider (400 or 401: revoked, expired, password changed, or a sign-in policy that needs the user) is `not_signed_in`; the tools then say to sign in again in Settings > Calendar & Email. No network is `unreachable` and a provider fault is `server_error`; neither touches the stored sign-in.
- A rotated refresh token is stored only while the record still holds the token the refresh started from, so a sign-in removed or replaced meanwhile (Settings and the daemon are separate processes) never comes back.
- `bearer_get` sends the access token only to the API it was issued for. A 401 drops the cached token and retries once with a fresh one; a second 401 is `not_signed_in`. A 403 or 429 (rate limits, no mailbox licence, access denied for one resource) is `server_error` and keeps the token. Paging links are followed only when they have the API base's scheme, host and port.

## Connecting and removing

Every add tests the account before anything is kept. A secret is stored first (the test reads it from the store), removed again when the test fails or anything unexpected happens, and the config entry is written, with `calendar_enabled` or `email_enabled` turned on, only after the test passes.

| Option | What it adds | Test |
|--------|--------------|------|
| Sign in with Microsoft (Calendar and Email ticked by default) | Calendar `{"type": "microsoft", "login", "user"}` and mailbox `{"type": "graph", "login", "user"}`, named `Microsoft` (`Microsoft 2`, ...) | The next 7 days of the calendar and the newest inbox message |
| Sign in with Google | Calendar `{"type": "google", "login", "user"}`, named `Google` | The next 7 days |
| Add Gmail | IMAP entry for `imap.gmail.com:993` with the address, password stored as `email_password:<name>`, named `Gmail` | Newest inbox message |
| Other email (IMAP) | IMAP entry for the server, port and address given, named `Email` | Newest inbox message |
| Paste a calendar link | ICS entry with the name given (default `Calendar`; unique, no `#`), link stored as `calendar_url:<name>` | A fresh download (not the saved copy) |
| Use Outlook on this PC (shown only when `outlook.install_state()` is `classic`) | `outlook` calendar and mailbox entries, each only if not already connected | Reading each through COM |

- The signed-in address (`user`) comes from Graph `/me` (`mail`, else `userPrincipalName`) or Google's primary calendar ID. It is shown in the account list and used to recognise the same account; it never reaches tool results or logs.
- Signing in again to an account already connected (same provider and address) replaces its sign-in and connects exactly what is ticked now, keeping the existing names; the old record is deleted. Adding the same IMAP mailbox again updates its password.
- Removing an entry deletes its secret: an IMAP password, a calendar link and its saved copy, or a sign-in record once no other entry uses it. Removing the last calendar or mailbox turns `calendar_enabled` or `email_enabled` off. Access granted at Microsoft or Google can additionally be withdrawn at account.microsoft.com or myaccount.google.com/permissions.
- Changes take effect when Jarvis restarts (the daemon reads its settings once). Settings offers the restart when it closes after any account change, even when the window itself is cancelled.
- The Settings page runs every add, test and sign-in on a worker thread and receives the result on the GUI thread; while one runs, the other buttons are disabled and a sign-in shows a Cancel button.

## Verification contract

Automated tests run against loopback fakes only (`tests/fake_accounts_server.py` plays the identity endpoints, Graph, Google Calendar and the user's browser):

- PKCE: S256 of a fresh verifier.
- Sign-in: read-only scopes per tick, `S256`, a long `state`, the redirect host per provider, the verifier and (Google) the client secret in the exchange, offline access for Google; a forged redirect refused with nothing echoed and the real one still signing in; only forged redirects end as a state mismatch and are never exchanged; declined consent; an unticked scope; cancel closes the port quickly, also with a silent connection open; time limit; the receiver listens on 127.0.0.1 only, ignores other paths and frees its port; no browser; no client ID; client IDs from config over the built-in ones.
- Stored sign-ins: refresh token only in the credential store; access tokens reused and rotated tokens saved; one session per sign-in; revoked means sign in again; unreachable keeps the sign-in; a sign-in removed during a refresh stays removed; Google refreshes with its secret.
- Store: one Microsoft sign-in connects calendar and inbox with the address and no token in the config; unticking email asks for less; Graph paging, cancelled and all-day events; Google's shown calendars only, cancelled events left out; signing in again replaces without duplicates; a second account gets its own name; a failed read or an unexpected error keeps nothing; a revoked sign-in says to sign in again in Test and in both tools; a missing client ID explains itself; removing keeps a sign-in while it is in use; Gmail and calendar links tested before they are kept; Outlook only when classic is installed; logs without addresses or tokens.
- Settings page (offscreen Qt with a stub store): sign-in buttons off with the reason when no client ID; Outlook only when classic is installed; ticks passed to the sign-in; nothing ticked starts nothing; Google is calendar only; busy state and Cancel; Test and a confirmed Remove; the Gmail, IMAP and link dialogs save only after a passing test; worker results always reach the GUI thread; closing cancels a waiting sign-in; the window follows an added calendar and Save keeps it; the restart offer after an account change even when cancelled.

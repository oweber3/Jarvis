# Phone Access Specification

Jarvis can be reached from the user's phone through a small web app served by the daemon on the PC. The phone shows the live orb, the shared conversation and pending desktop confirmations, and sends typed requests through the same path as the desktop chat window. Nothing is hosted elsewhere: the phone talks straight to the PC over the home network or a private VPN the owner chooses (for example Tailscale, Headscale or WireGuard). How the phone reaches the PC is the owner's choice and outside Jarvis.

## Principles

- **Off by default.** `remote_access_enabled` is `false`. When it is off, no socket is opened and nothing in this package runs.
- **Local and self-hosted.** The daemon serves everything itself with the Python standard library. The page loads no remote fonts, scripts or images, and the server contacts nothing. No telemetry.
- **Private networks only.** A request is answered only when the client address is loopback, a private IPv4 range (`10/8`, `172.16/12`, `192.168/16`), link-local, the shared address space VPNs such as Tailscale use (`100.64.0.0/10`), or an IPv6 unique-local address (`fc00::/7`, which covers Tailscale's IPv6). Anything else gets `403` before authentication is looked at, so an accidental port forward exposes nothing.
- **Paired devices only.** Every API call except pairing needs a device token. Tokens are random, issued once at pairing and stored on the PC only as SHA-256 hashes. A revoked device stops working on its next request.
- **One conversation.** The phone is another view of the daemon's single dialogue memory, like the chat window (`src/desktop_app/chat_window.spec.md`). Voice turns, desktop chat turns and phone turns all appear in it, and a phone request becomes part of the same conversation and diary.
- **Same safety path.** Phone requests run through `jarvis.daemon.submit_text_query`, so redaction, the fast path, the reply mode, the one-query-at-a-time lock and central tool safety all apply unchanged. The phone never runs tools itself. Phone requests carry the origin `phone`, so the window in front of the PC is never taken as what the request is about (`memory/desktop_referents.spec.md`, Foreground window).
- **Nothing written to disk except devices.** The phone transcript is held in memory and lost when the daemon stops. The only files are the paired-device list and a short-lived pairing code, both hashed.

## Components

| Module | Responsibility |
|--------|----------------|
| `remote/networks.py` | `is_allowed_client(address)`: the private-network allowlist above |
| `remote/pairing.py` | `DeviceStore`: pairing codes, device tokens (hashed), revocation, last-seen |
| `remote/hub.py` | `RemoteHub`: in-memory transcript mirrored from the dialogue memory, phone query tracking, assistant state, pending confirmation, change notification for long polls |
| `remote/backend.py` | `DaemonBackend`: the only module that touches `jarvis.daemon`, the confirmation store, reply modes and system statistics. Tests replace it with a fake |
| `remote/server.py` | `RemoteServer`: threaded HTTP server, routing, authentication, static files |
| `remote/runtime.py` | `start(cfg)` / `stop()`: called by the daemon; starts nothing when disabled |
| `remote/__main__.py` | `python -m jarvis.remote pair` / `devices` / `revoke <id>`: pairing and device management from a terminal |
| `remote/static/` | The phone web app (HTML, CSS, JavaScript, manifest, icon) |

`jarvis.remote` knows nothing about `desktop_app`. The desktop app's `Phone Access` dialog uses `DeviceStore` directly through the shared files below, so pairing works whether the daemon runs in-process or as a subprocess.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `remote_access_enabled` | `false` | Serve the phone app. Restart Jarvis after changing it |
| `remote_access_host` | `"0.0.0.0"` | Address to listen on. `0.0.0.0` listens on every interface (the client allowlist still applies); a specific address, such as the PC's Tailscale address, limits it to that network |
| `remote_access_port` | `8765` | TCP port, 1024 to 65535 |
| `remote_access_allow_confirm` | `true` | Let a paired phone approve or deny the desktop confirmation dialog |
| `remote_access_quick_actions` | see below | Commands shown as one-tap buttons on the phone. Each is sent as a typed request |

The default quick actions are fast-path commands: "Pause the music", "Play the music", "Next song", "Volume up", "Volume down" and "What's using the most RAM?". An empty list hides the row.

A port or host that cannot be bound prints one warning at start-up and phone access stays off; the rest of Jarvis is unaffected.

## Pairing and devices

Files live next to the database (the directory of `db_path`):

- `remote_devices.json`: `{"devices": [{"id", "name", "token_hash", "created_at", "last_seen"}]}`.
- `remote_pairing.json`: the active pairing code as `{"salt", "code_hash", "expires_at", "attempts"}`.

Both are written atomically (temporary file, then replace). `DeviceStore` re-reads `remote_devices.json` when its modification time changes, so a revoke from the desktop dialog or the terminal reaches a running daemon.

Pairing:

1. On the PC, the user opens `Phone Access` from the tray and presses **Pair a phone**, or runs `python -m jarvis.remote pair`. A six-digit code is created, valid for five minutes. Starting a new code replaces the old one.
2. On the phone, the user opens `http://<PC address>:<port>/`. The page asks for the code and a device name.
3. `POST /api/pair` checks the code in constant time. A correct code issues a 256-bit token, stores its hash with the device, and deletes the pairing file, so a code works once. Each wrong code counts; the fifth wrong attempt deletes the code. An expired code is deleted when it is checked.
4. The phone keeps the token in its browser storage and sends it as `Authorization: Bearer <token>`.

Device names are trimmed to 40 characters. A device's `last_seen` is updated in memory on every authenticated request and written to the file at most once every five minutes.

## HTTP API

Static files (`/`, `/app.js`, `/app.css`, `/manifest.webmanifest`, `/icon.svg`) need no token; they hold no data. Every response carries `Cache-Control: no-store` for the API, a `Content-Security-Policy` of `default-src 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. Request bodies over 16 KiB are refused with `413`.

| Method and path | Auth | Body | Result |
|-----------------|------|------|--------|
| `POST /api/pair` | none | `{"code", "name"}` | `200 {"token", "device_id"}`, `403` for a wrong or expired code |
| `GET /api/poll?after=<entry id>&rev=<rev>` | token | | Long poll, below |
| `POST /api/chat` | token | `{"text"}` | `202 {"query_id"}`, `409` when Jarvis is busy, `503` when the daemon is not ready, `400` for empty or over-long text (4000 characters) |
| `POST /api/stop` | token | | Cancels the query in flight (`jarvis.daemon.cancel_active_chat_query`) |
| `POST /api/confirm` | token | `{"id", "approve"}` | Answers the pending desktop confirmation; `403` when `remote_access_allow_confirm` is off, `409` when the request is no longer pending |
| `POST /api/unpair` | token | | Revokes the calling device |

A missing or unknown token gets `401`. The page then forgets its token and returns to pairing.

### Long poll

`GET /api/poll` returns at once when the hub's revision differs from `rev` or there are transcript entries after `after`; otherwise it waits up to 25 seconds for a change and then returns the current snapshot. The snapshot is:

```json
{
  "rev": 12,
  "state": "idle",
  "busy": false,
  "entries": [{"id": 7, "role": "user", "text": "...", "ts": 1760000000.0}],
  "queries": {"3": {"status": "done", "reply": "..."}},
  "confirmation": {"id": "...", "tool": "...", "action": "...", "target": "...", "consequence": "...", "expires_in": 41},
  "mode": {"mode": "local", "enabled": ["local"]},
  "pc": {"cpu": 12.0, "ram": 41.5},
  "quick_actions": ["Pause the music"],
  "allow_confirm": true
}
```

- `state` is the assistant state (`assistant_state.current()`), except that it reads `thinking` while a phone query is in flight, since text queries do not change the voice state.
- `entries` are transcript entries with `id > after`, oldest first, at most the newest 200.
- `queries` reports the phone's recent queries (the last 20): `pending`, `done` (with the reply text), `busy`, `failed` or `stopped`.
- `confirmation` is present only while a `CONFIRM_DIALOG` request is pending and `remote_access_allow_confirm` is on. It names the tool, action, target and consequence so the user knows what they approve.
- `pc` is CPU and memory use in percent, or absent when `psutil` is unavailable.

## Transcript

The hub mirrors the dialogue memory through `DialogueMemory.messages_after(ts)`, which returns stored turns newer than a timestamp. Timestamps are unique and increasing, so each turn is mirrored once, including turns from voice and the desktop chat, the outcome of a confirmed action and turns kept out of the diary. The mirror is a separate in-memory log (the newest 200 entries), so a reply stays on the phone after the dialogue memory forgets it at the end of a conversation. A rewind in the desktop chat does not remove entries the phone has already shown; the regenerated turns appear after them.

The hub syncs every half second, on every assistant state change, and immediately when a phone query completes, so the reply is on the phone before the query is reported done. Content is already redacted, because redaction runs before a turn enters the dialogue memory.

Phone query outcomes add local entries (role `notice`), never stored in the dialogue memory:

- Busy: "Jarvis is busy with another request."
- No reply from the engine: the redacted request as a `user` entry, then "Jarvis couldn't answer that."
- Daemon not ready (the query never started): "Jarvis isn't ready yet."
- Stopped: "Stopped."

The daemon side is `jarvis.daemon.get_dialogue_memory()` and `jarvis.daemon.is_query_running()` (the shared one-query lock), read only through `DaemonBackend`.

The phone shows its own message straight away as a pending bubble and drops it when its query leaves `pending`; the conversation entry from the mirror replaces it.

## The phone app

A single page in the HUD look of the desktop orb and chat window. Its colours are the orb's `ORB_PALETTE` values, kept in `static/app.css` as custom properties; a test checks they match `desktop_app.themes.ORB_PALETTE`.

- **Orb.** A canvas port of the desktop orb (`src/desktop_app/orb_widget.spec.md`): the same state looks (offline, idle, listening, thinking, speaking, dictating), rings, bars, scanning arcs, core and ripples, and the synthetic speaking envelope (the phone has no audio level, so listening uses a gentle synthetic level too). It animates with `requestAnimationFrame` only while the page is visible, honours reduced motion, and reads `offline` with a "can't reach the PC" label when the PC cannot be reached. It shrinks while the keyboard is open or a confirmation is pending, so the conversation keeps its room. Tapping it scrolls to the newest message.
- **Chat.** One continuous thread: user bubbles on the right, Jarvis on the left, notices centred, each with a time. Enter (the keyboard's send key) sends; Shift+Enter adds a new line on hardware keyboards. While a phone query is in flight, a typing indicator shows and Stop replaces Send. A message that could not be sent goes back into the input box.
- **Confirmation card.** When a desktop confirmation is pending, a card shows what Jarvis wants to do with **Approve** and **Deny** and a countdown. Answering on the phone or on the desktop resolves the same request; whichever comes first wins and the other is closed.
- **Quick actions.** A row of buttons from `remote_access_quick_actions`.
- **Status line.** Connection state, reply mode (local, Claude or Codex) and the PC's CPU and memory use.
- **Read replies aloud.** An opt-in switch that speaks replies to the phone's own requests with the browser's built-in speech synthesis on the phone. It is off by default and remembered in the phone's browser storage.
- **Install.** A web app manifest and an icon, so the page can be added to the home screen.
- **Unpair.** Forgets the token on the phone and revokes the device on the PC.

The page polls with the long-poll endpoint and backs off (1 s, doubling to 15 s) while the PC is unreachable. It works over plain HTTP on the user's network; features that need HTTPS in browsers (microphone, service workers) are not used.

## Desktop dialog

A `Phone Access` tray entry (with a phone line icon) opens a dialog (shared HUD theme, plain text, no emoji) that:

- says whether phone access is on and, when it is off, offers **Turn on**, which writes `remote_access_enabled: true` and asks for a restart;
- lists the addresses to open on the phone: `http://<address>:<port>/` for each private IPv4 address of this PC and its host name;
- **Pair a phone** shows a new six-digit code and its remaining time;
- lists paired devices with when they were added and last seen, each with **Remove**.

## Logging

Debug logs (`remote` tag) record start, stop, bind failures, pairing success and failure counts, device revocation, refused client addresses and rejected tokens. They never contain tokens, codes, message text or device names.

## What it does not do

- No voice from the phone. Browsers allow the microphone only over HTTPS, and phone-side speech recognition would send audio to the phone vendor.
- No access from the public internet. The user reaches the PC over their home network or their own VPN.
- No HTTPS termination. On a VPN such as Tailscale the traffic is already encrypted end to end; on the home network it is plain HTTP like other local devices.
- No push notifications while the page is closed.

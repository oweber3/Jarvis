# Phone Access Specification

Jarvis can be reached from the user's phone through a web app served by the daemon on the PC. The phone shows the same chat as the web chat (`webchat/webchat.spec.md`: thread, tool steps, projects, model picker, start screen), a full-screen voice mode with the desktop's particle-sphere orb (`src/desktop_app/orb_widget.spec.md`), pending desktop confirmations and quick actions, and the user can talk to Jarvis from the phone and hear the reply on the phone in Jarvis's own voice. Nothing is hosted elsewhere: the phone talks straight to the PC over the home network or a private VPN the owner chooses (for example Tailscale, Headscale or WireGuard). How the phone reaches the PC is the owner's choice and outside Jarvis.

## Principles

- **Off by default.** `remote_access_enabled` is `false`. When it is off, no socket is opened and nothing in this package runs.
- **Local and self-hosted.** The daemon serves everything itself with the Python standard library. The page loads no remote fonts, scripts or images, and the server contacts nothing. No telemetry. Speech recognition and speech synthesis run on the PC (Whisper and the configured Piper or Kokoro voice); there is no cloud speech service and no browser speech service, ever.
- **Private networks only.** A request is answered only when the client address is loopback, a private IPv4 range (`10/8`, `172.16/12`, `192.168/16`), link-local, the shared address space VPNs such as Tailscale use (`100.64.0.0/10`), or an IPv6 unique-local address (`fc00::/7`, which covers Tailscale's IPv6). Anything else gets `403` before authentication is looked at, so an accidental port forward exposes nothing.
- **Only the phone app, not other web pages.** A browser on the network may also be showing someone else's page, which could try to reach phone access by address or through a DNS name rebound to the PC. Every request passes the shared checks in `jarvis.utils.local_guard` after the client address check, and any refusal is `403`:
  - The `Host` must be one the app is opened by (`networks.is_own_host`): `localhost`, a loopback or private-network address (the client allowlist above), the certificate's own name (`remote_access_tls_host`), or this PC's host name bare, under a local DNS suffix (`.local`, `.lan`, `.home`, `.home.arpa`, `.internal`, `.localdomain`) or as a Tailscale MagicDNS name (`<pc name>.<tailnet>.ts.net`). Any other name is refused.
  - An `Origin`, when sent, must match the `Host`.
  - API requests (`/api/...`) with `Sec-Fetch-Site: cross-site` are refused. The app's own pages are still served to a link from another site.
  - A `POST` that carries a body, and every `POST /api/pair`, must declare `Content-Type: application/json`, which another site cannot send without a CORS preflight the server never answers; anything else gets `415` before the route runs, so a refused pairing never spends an attempt. The one exception is the audio upload (`POST /api/voice`) and the attachment upload (`POST /api/attachments`), which take `application/octet-stream`; another site cannot send that cross-origin either, and both are checked by exactly the same address, Host, Origin, fetch-site and device-token rules first.
- **Paired devices only.** Every API call except pairing needs a device token. Tokens are random, issued once at pairing and stored on the PC only as SHA-256 hashes. A revoked device stops working on its next request.
- **One conversation.** The phone is another view of the web chat's hub and store (`webchat/hub.py`, `memory/chat_store.py`): one open chat, the same projects, the same shared dialogue memory as voice at the PC and the desktop chat. A phone request becomes part of the same conversation and diary.
- **Same safety path.** Phone requests run through `jarvis.daemon.submit_text_query` with the origin `phone`, so redaction, the fast path, the reply mode, the one-query-at-a-time lock and central tool safety all apply unchanged. The phone never runs tools itself. The origin `phone` means the window in front of the PC is never taken as what the request is about (`memory/desktop_referents.spec.md`, Foreground window), and the webcam is never turned on for gesture snapping (`gestures/gestures.spec.md`, Turning it on and off; stopping it works). A spoken phone request takes the same path as a typed one once it is text.
- **Nothing written to disk except devices and the chat store.** Phone audio never touches the disk (see **Privacy of audio**). The only files this package writes are the paired-device list and a short-lived pairing code, both hashed; the conversation is stored by the web chat's `ChatStore` exactly as for the web chat.

## Components

| Module | Responsibility |
|--------|----------------|
| `remote/networks.py` | `is_allowed_client(address)`: the private-network allowlist above; `is_own_host(name, pc_name, extra_hosts)`: the `Host` names the app is opened by |
| `remote/pairing.py` | `DeviceStore`: pairing codes, device tokens (hashed), revocation, last-seen |
| `remote/tls.py` | `TLSConfig` and `ReloadingContext`: the server certificate and key, checked at start, reloaded when either file changes |
| `remote/tailscale.py` | `issue_certificate`: the `python -m jarvis.remote cert` logic around the locally installed `tailscale` program |
| `remote/voice.py` | `PhoneVoice`: audio validation, transcription, synthesis of replies, the per-device audio status; `AudioStore`: one-shot, short-lived, in-memory reply audio; `wav_bytes` |
| `remote/phone.py` | `PhoneState`: what the phone shows beside the chat (pending confirmation, PC statistics, quick actions, voice availability, audio status) and its change notification |
| `remote/backend.py` | `DaemonBackend`: the only module of this package that touches `jarvis.daemon`, the confirmation store, system statistics, the dictation engine's transcription and the voice engine's synthesis. Tests replace it with a fake |
| `remote/server.py` | `RemoteServer`: threaded HTTP or HTTPS server, routing, authentication, static files; the chat routes are `webchat/api.py`'s `ChatApi` |
| `remote/runtime.py` | `start(cfg)` / `stop()`: called by the daemon; starts nothing when disabled |
| `remote/__main__.py` | `python -m jarvis.remote pair` / `devices` / `revoke <id>` / `cert`: pairing, device management and the HTTPS certificate from a terminal |
| `remote/static/` | The built phone app (the React app's `phone` build, `webchat-ui/`), committed so a source checkout needs no Node |
| `webchat/api.py` | `ChatApi`: the chat, project, model, attachment and poll routes, shared with the web chat's server |
| `webchat/runtime.py` | `acquire_hub(cfg)` / `release_hub()`: the one `ChatHub` and `ChatStore` that the web chat and phone access share |

`jarvis.remote` knows nothing about `desktop_app`. The desktop app's `Phone Access` dialog uses `DeviceStore` directly through the shared files below, so pairing works whether the daemon runs in-process or as a subprocess.

### One hub, two servers

The web chat's hub (`ChatHub`) owns the open chat, the mirror of the dialogue memory into it and the query tracking. Phone access uses that same hub, whether or not `web_chat_enabled` is on:

- `webchat.runtime.acquire_hub(cfg)` creates the hub and its `ChatStore` on first use, starts it, and counts users. The web chat's runtime and phone access each acquire it at start and release it at stop; the last release stops the hub and closes the store. Starting phone access with the web chat off therefore starts a hub and no web chat server, and the other way round.
- The web chat's loopback server stays loopback only and has no device tokens. Phone access never opens a route on it, and the phone's routes are served only by `RemoteServer`, behind the checks above and a device token.
- Phone requests are submitted with the origin `phone` and the source `typed` or `voice`, so the stored turn shows as typed or spoken in every view of the chat.
- The hub accepts extra change signals (`ChatHub.watch`), so a confirmation appearing or resolving, or reply audio becoming ready, wakes the phone's long poll within half a second.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `remote_access_enabled` | `false` | Serve the phone app. Restart Jarvis after changing it |
| `remote_access_host` | `"0.0.0.0"` | Address to listen on. `0.0.0.0` listens on every interface (the client allowlist still applies); a specific address, such as the PC's Tailscale address, limits it to that network |
| `remote_access_port` | `8765` | TCP port, 1024 to 65535 |
| `remote_access_allow_confirm` | `true` | Let a paired phone approve or deny the desktop confirmation dialog |
| `remote_access_quick_actions` | see below | Commands shown as one-tap buttons on the phone. Each is sent as a typed request |
| `remote_access_tls_cert` | `""` | Path to a PEM certificate chain. With the key, the same port serves HTTPS |
| `remote_access_tls_key` | `""` | Path to the PEM private key of that certificate |
| `remote_access_tls_host` | `""` | The certificate's DNS name (for example `pc.tail1234.ts.net`). Written by `python -m jarvis.remote cert`; it is an allowed `Host` and the address shown in the Phone Access dialog |

The default quick actions are fast-path commands: "Pause the music", "Play the music", "Next song", "Volume up", "Volume down" and "What's using the most RAM?". An empty list hides the row.

A port or host that cannot be bound prints one warning at start-up and phone access stays off; the rest of Jarvis is unaffected.

## HTTPS

Browsers allow the microphone only in a secure context, so voice needs HTTPS. When `remote_access_tls_cert` and `remote_access_tls_key` are both set, `RemoteServer` serves HTTPS on the configured port with the standard library's `ssl` (TLS 1.2 or later, the server's own certificate chain, no client certificates). When either is empty the same port serves plain HTTP exactly as without the keys, and voice is reported unavailable with the reason `no_https`.

- Each connection is handshaken on its own thread with a timeout, so a client that stalls in the handshake never blocks other clients. A failed handshake is dropped silently (a debug log records only that one failed).
- The certificate and key are loaded when the server starts. A file that is missing, unreadable, not PEM or does not match makes the start fail with one warning that names what is wrong and says to run `python -m jarvis.remote cert`; phone access then stays off and never falls back to plain HTTP when the owner asked for HTTPS. A key that needs a passphrase is refused like any unusable key (it never prompts on the console). A running server reloads the pair when either file changes (checked per connection), so renewing the certificate needs no restart.
- Every guard above applies unchanged to HTTPS. No `Strict-Transport-Security` header is sent, because the same app is also reached by plain address.
- The Phone Access dialog and `python -m jarvis.remote pair` show the `https://` address of `remote_access_tls_host` (and no plain address, which the HTTPS port cannot serve) when a certificate is configured.

### `python -m jarvis.remote cert`

Gets a certificate from the locally installed Tailscale and configures it. Nothing leaves the PC except what `tailscale cert` itself does (Tailscale's own certificate request for the PC's `ts.net` name).

1. Finds the `tailscale` program (`PATH`, then `C:\Program Files\Tailscale\tailscale.exe`). Missing: says to install Tailscale from tailscale.com/download and sign in.
2. Reads the PC's name with `tailscale status --json` (`Self.DNSName` without the trailing dot, and `CertDomains`). Tailscale stopped or signed out: says to start Tailscale and sign in. No `CertDomains`: says exactly what to do, "Open the Tailscale admin console, DNS page, scroll to HTTPS Certificates and press Enable HTTPS, then run this command again".
3. Runs `tailscale cert --cert-file <config folder>/remote_access.crt --key-file <config folder>/remote_access.key <name>` and checks both files exist. `tailscale cert` failing is reported with its own message.
4. Writes `remote_access_tls_cert`, `remote_access_tls_key` and `remote_access_tls_host` to `config.json` (only those keys, other settings untouched) and prints the `https://<name>:<port>/` address, the certificate's roughly 90-day lifetime ("run this command again to renew; no restart needed") and that Jarvis must be restarted once if phone access was already running without HTTPS.

The config folder is the folder holding `config.json`. The command prints emoji lines with indentation, per the project's command line convention, and never prints the key. No other certificate source is supported; an owner with their own certificate sets the two keys by hand.

## Pairing and devices

Files live next to the database (the directory of `db_path`):

- `remote_devices.json`: `{"devices": [{"id", "name", "token_hash", "created_at", "last_seen"}]}`.
- `remote_pairing.json`: the active pairing code as `{"salt", "code_hash", "expires_at", "attempts"}`.

Both are written atomically (temporary file, then replace). `DeviceStore` re-reads `remote_devices.json` when its modification time changes, so a revoke from the desktop dialog or the terminal reaches a running daemon.

Pairing:

1. On the PC, the user opens `Phone Access` from the tray and presses **Pair a phone**, or runs `python -m jarvis.remote pair`. A six-digit code is created, valid for five minutes. Starting a new code replaces the old one.
2. On the phone, the user opens the address the dialog shows. The page asks for the code and a device name.
3. `POST /api/pair` checks the code in constant time. A correct code issues a 256-bit token, stores its hash with the device, and deletes the pairing file, so a code works once. Each wrong code counts; the fifth wrong attempt deletes the code. An expired code is deleted when it is checked.
4. The phone keeps the token in its browser storage and sends it as `Authorization: Bearer <token>`.

Device names are trimmed to 40 characters. A device's `last_seen` is updated in memory on every authenticated request and written to the file at most once every five minutes.

## HTTP API

Static files need no token; they hold no data: `/` and `/index.html`, `/assets/<built file>`, `/manifest.webmanifest`, `/icon.svg`, `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` and `/pcm-worklet.js` (the audio worklet that hands microphone samples to the page). Nothing else on disk is reachable. Every response carries `Cache-Control: no-store` for the API, a `Content-Security-Policy` of `default-src 'self'` (images may also be `data:` and styles inline), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and `Permissions-Policy: microphone=(self), camera=(), geolocation=()` (only the phone's own page may ask for the microphone). JSON request bodies over 64 KiB are refused with `413`, an attachment upload over 10 MiB and an audio upload over the cap below likewise. A body over 64 KiB is read only from a paired phone on a private network: any other client declaring one is answered at once (`403`, or `401` without a valid token) and the body is never read, so an unpaired client cannot make the server hold megabytes. Every method that carries a body, not only `POST`, must declare `application/json` (`415` otherwise).

| Method and path | Auth | Body | Result |
|-----------------|------|------|--------|
| `POST /api/pair` | none | `{"code", "name"}` (JSON) | `200 {"token", "device_id"}`, `403` for a wrong or expired code, `415` without a JSON body |
| `GET /api/poll?after=<message id>&rev=<rev>` | token | | Long poll, below |
| `POST /api/chat` | token | `{"text", "attachments"?, "speak"?}` | As the web chat's `POST /api/chat`; with `"speak": true` the reply is also synthesised for this phone (**Voice**) |
| `POST /api/stop` | token | | Cancels the query in flight (`jarvis.daemon.cancel_active_chat_query`) |
| `POST /api/confirm` | token | `{"id", "approve"}` | Answers the pending desktop confirmation; `403` when `remote_access_allow_confirm` is off, `409` when the request is no longer pending |
| `POST /api/unpair` | token | | Revokes the calling device |
| `POST /api/voice` | token | raw audio (`application/octet-stream`) | A spoken request (**Voice**) |
| `GET /api/audio/<token>` | device token and audio token | | One reply's audio, once (**Voice**) |
| The chat routes | token | | `GET /api/state`, `/api/models`, `/api/suggestions`, `/api/library`, `/api/search`, `POST /api/projects`, `PATCH`/`DELETE /api/projects/<id>`, `POST /api/chats`, `GET`/`PATCH`/`DELETE /api/chats/<id>`, `POST /api/chats/<id>/open`, `POST /api/attachments`, `DELETE /api/attachments/<id>`, `GET /api/attachments/support`, `POST /api/model`: exactly as in `webchat/webchat.spec.md` (HTTP API), served by the same `ChatApi` |

Not served to a phone, answered `404`: `POST /api/clear` (deleting everything is a desktop action), `POST /api/attachments/screenshot` (the screen of the PC is not for a phone to ask for), `GET /api/identity` and the `/api/mic/*` routes (the PC's microphone; a phone records with its own).

A missing or unknown token gets `401`. The page then forgets its token and returns to pairing.

### Long poll

`GET /api/poll` returns at once when the hub's revision differs from `rev` or the open chat has messages after `after`; otherwise it waits up to 25 seconds for a change and then returns the current snapshot. The snapshot is the web chat's (`rev`, `library_rev`, `ready`, `state`, `busy`, `busy_query`, `active_chat_id`, `chat`, `messages`, `notices`, `mode`, `model`, `cloud`) plus a `phone` object:

```json
{
  "phone": {
    "confirmation": {"id": "...", "tool": "...", "action": "...", "target": "...", "consequence": "...", "expires_in": 41},
    "pc": {"cpu": 12.0, "ram": 41.5},
    "quick_actions": ["Pause the music"],
    "allow_confirm": true,
    "voice": {"available": true, "reason": null, "audio": {"7": {"status": "ready", "url": "/api/audio/3f9c..."}}}
  }
}
```

- `state` is the assistant state, except that it reads `thinking` while a request from the web chat or a phone is in flight, since text queries do not change the voice state.
- `confirmation` is present only while a `CONFIRM_DIALOG` request is pending and `remote_access_allow_confirm` is on, otherwise `null`. It names the tool, action, target and consequence so the user knows what they approve.
- `pc` is CPU and memory use in percent, or absent when `psutil` is unavailable.
- `voice.available` and `voice.reason` say whether a spoken request can work now: reasons `no_https` (the server is plain HTTP), `disabled` (dictation is off in Settings, which the speech model is shared with), `unavailable` (no dictation engine) and `not_ready` (the speech model is still loading).
- `voice.audio` holds the reply audio of this device's requests that asked for it, keyed by query id: `pending` while it is being synthesised, `ready` with the one-shot `url`, `none` when there is nothing to play (the request failed or was stopped, the reply was empty, no voice could speak it, or the audio has already been fetched). An entry lives as long as its clip (two minutes from when the audio was made), and reads `none` once its clip is gone.

## Voice

The phone records with its own microphone, the PC transcribes and speaks. The page follows `webchat-ui/src/phone/` and asks for the microphone only when the owner taps the microphone button, opens voice mode or taps the orb in voice mode.

### A spoken request: `POST /api/voice`

- **Audio.** Raw 16 kHz, mono, signed 16-bit little-endian PCM, no header. The page captures with an AudioWorklet and converts itself, so iOS and Android never differ in container or codec. The body is at most `MAX_VOICE_BYTES` (120 s of audio and one second of grace, 3,872,000 bytes; `413` beyond) and at least 0.3 s (`400 too_short`); an odd byte count is `400 bad_audio`; another content type is `415 octet_stream_required`. This is the one exception, with attachments, to the JSON body rule (**Principles**).
- **Refusals before any work**, in this order: no device token `401`; the body's size and shape (`413`, `400`, `415` above); a plain-HTTP server `503 no_https`; the voice not available now `503` with the reason above; Jarvis busy (a query running, Jarvis listening or speaking at the PC, or another spoken request being transcribed) `409 busy`. Nothing is transcribed for a refused request.
- **Transcription.** The audio is passed to the daemon's dictation engine (`DictationEngine.transcribe_samples`), which uses the Whisper model Jarvis already loaded with the same silence gate, hallucination filter, custom dictionary and filler removal as hold-to-dictate (`dictation/dictation.spec.md`, Transcribing audio for a caller). It does not pause the PC's listener, play a beep or change the face.
- **Hung up or stopped.** A request the phone gave up on never starts: when the phone has closed the connection, or Stop (`POST /api/stop`) was pressed, while the audio was being transcribed, the words are dropped (`400 cancelled`, or `200 {"status": "stopped", "text": ""}`) and nothing is sent to the reply engine.
- **Nothing heard.** An empty transcript answers `200 {"status": "nothing_heard", "text": ""}` and starts no request.
- **Submit.** The text goes through `ChatHub.submit` with the origin `phone` and the source `voice`, so redaction, the fast path, the reply mode, the query lock and central safety apply and the turns land in the open chat. The answer is `202 {"query_id", "text", "status": "accepted"}` (`text` is the transcript, returned to this phone only), `409 busy` when the lock was taken meanwhile or `503 unavailable` when the daemon is not ready. The reply is spoken to the phone: its audio status appears in `phone.voice.audio[query_id]` (`pending` as soon as the request is accepted).

### Reply audio

- **Which voice.** The reply is synthesised with the engine Jarvis speaks with at the PC (`output/tts.spec.md`): Piper, Kokoro with its fall-back to Piper, rendered by the live engine's `render`. When the active engine cannot render (Chatterbox), a Piper voice renders it. Synthesis works on the reply text with the same markdown and link clean-up as speech at the PC, capped at 3,000 characters (cut at a sentence end), one reply at a time on a worker thread.
- **Never on the PC speakers.** A phone request runs with the text path's `tts=None` and the audio is rendered into memory, never played: nothing is spoken at the PC for a request from a phone, typed or spoken, and the PC's voice outputs and extension voice outputs are not fed.
- **Format.** WAV (PCM, 16-bit mono at the engine's rate) built in memory by `wav_bytes`.
- **One-shot URL.** The audio waits in `AudioStore` under a random 128-bit token for the device that asked. `GET /api/audio/<token>` needs that device's token as well, returns the WAV once (`Cache-Control: no-store`) and forgets it; a second request, another device's request, an expired or unknown token is `404`. Unfetched audio expires after two minutes. The store keeps at most 8 clips per device and 32 MiB in all, dropping the oldest.
- **Typed requests.** `POST /api/chat` with `"speak": true` (the phone's "Read replies aloud" switch) is synthesised the same way and ends in the same audio status. Browser speech synthesis is not used anywhere.

### The page's voice behaviour

- **Start.** A tap on the microphone button, on the voice mode button (which starts listening as voice mode opens) or on the orb in voice mode asks for the microphone (or reuses the open stream), unlocks audio playback in the same gesture (the page creates and resumes its `AudioContext` on the first tap, as iOS requires), and records. Refusals are said in plain words, as the web chat's microphone does: Jarvis is busy, the speech model is not ready, voice is off in Settings, the page is not on HTTPS, the microphone was denied or is missing.
- **End.** Recording ends on silence (a level-based end-point: the room's noise floor is measured first, speech must rise above it, and about 1.2 s below it after speech ends the request), on a second tap, or at 120 s. If no speech is heard for about 8 s it ends without sending anything.
- **Upload and wait.** The page sends the audio, shows the transcript as its own message at once, shows that the PC is working (the orb thinking in voice mode, the indicator above the message box otherwise), then fetches the reply audio with its device token and plays it through an `AnalyserNode`.
- **Barge-in.** While Jarvis speaks to the phone, a tap on the orb or the microphone button stops playback. In conversation mode the page starts listening in that same tap and conversation mode stays on; otherwise the reply simply ends.
- **Conversation mode.** On in voice mode, off outside it: after Jarvis finishes speaking the page listens again by itself, until silence with no speech, a refusal, an error or a tap that stops a request in flight ends it. While it is on, the microphone stream stays open between turns; turning it off, closing voice mode, leaving the page or backgrounding it releases the microphone.
- **Orb levels.** While listening the orb follows the phone microphone's level (and so does the small level bar above the message box); while speaking it follows the reply audio's level (`AnalyserNode`); otherwise it uses the synthetic envelope.

## Privacy of audio

These are requirements, not defaults:

- Phone audio goes only to the PC, over the connection the page opened (HTTPS when voice is available). It is never sent to a speech service, a cloud model, a browser speech API or anywhere else.
- It stays in memory: the request body is decoded to samples, transcribed and dropped. No uploaded audio and no reply audio is ever written to disk, in a temporary file or a cache, and reply audio is gone after its one fetch or two minutes.
- Neither the audio nor the transcript is logged. Debug logs (`remote` tag) record only the number of bytes and seconds of an upload, the number of characters of a transcript or a reply, how long transcription and synthesis took, and refusal reasons. They contain no text, no token, no URL path with a token and no device name.
- The transcript is returned only in the response to the request that carried the audio. It enters the conversation only as the redacted turn the dialogue memory stores, like any typed message.
- Reply audio is fetched with the device's token and a one-shot URL; another paired device cannot fetch it.

## Transcript

The conversation is the web chat's: `ChatHub` mirrors the dialogue memory into the open chat in `ChatStore` (`webchat/webchat.spec.md`, Chats and the shared memory), so voice at the PC, the desktop chat, the web chat and the phone all land in one chat with one history. Content is already redacted, because redaction runs before a turn enters the dialogue memory. Local notices (busy, no reply, stopped, not ready) are shown but never stored.

## The phone app

The phone app is the web chat's React app built in its `phone` mode (`webchat-ui/`, `npm run build:phone`), so the phone and the desktop share one set of components: thread, tool steps, projects sidebar (in a drawer), model picker, start screen, timestamps, search, attachments and light and dark. Its colours are the HUD palettes generated from `desktop_app/themes.py`; the orb's colours are generated from `ORB_PALETTE` (`webchat-ui/src/generated/orbPalette.ts`), and a test fails when the committed file is stale.

The phone mode lays the page out as a phone chat app, chat first. What it changes:

- **Pairing screen.** With no token the page shows the pairing form (code and device name, `POST /api/pair`) and stores the token in the browser's storage when it allows. Every request then carries `Authorization: Bearer <token>`; a `401` forgets the token and returns to the form. The web chat's loopback identity is not used.
- **Main screen.** A slim header, the conversation in all the remaining height, and the message box pinned to the bottom above the keyboard and the safe area, on every chat including an empty one. There is no orb on this screen.
- **Header.** The chats button, the chat's title, a connection dot and the settings button. The dot is green while the PC is connected and free, amber while Jarvis is busy (a request running) or starting, and red when the PC cannot be reached; tapping it shows the status details: the connection, the reply mode (local, Claude or Codex) and the PC's CPU and memory use.
- **Chats drawer.** The projects sidebar in a full-height drawer that slides in from the start edge over a backdrop. It closes when a chat is chosen, on a tap on the backdrop, on Esc, with its close button and with a swipe back towards its edge.
- **Message box.** The attachment button and, when the PC has quick actions, a lightning button on the left; the microphone button, the voice mode button and Send on the right. The model picker is in the settings sheet.
- **Dock.** A stack just above the message box, inside the conversation's sticky footer, holds the voice indicator, the confirmation card and the toast. The conversation scrolls behind it, and it never covers the message box or the keyboard.
- **Microphone button.** One spoken request without voice mode (conversation mode is off outside voice mode). While it works, a small indicator in the dock says what is happening (listening, with a level bar that follows the microphone, sending, working, speaking); a refusal shows there in plain words until the next request or until it is dismissed.
- **Voice mode.** The voice mode button opens a full-screen modal dialog and starts listening in the same tap, with conversation mode on. It shows the orb large and centred with its state caption (listening, thinking, speaking, and refusals in plain words), the latest thing the owner said and Jarvis's reply as short text under it, a pending confirmation and the toast above its buttons, a **Conversation** switch that turns conversation mode off and on, and a large close button. Tapping the orb talks, ends a recording and sends it, stops a request in flight or interrupts Jarvis speaking, as **The page's voice behaviour** describes. A reply already being read aloud when voice mode opens plays on, and listening follows it. Closing voice mode (the close button or Esc), leaving the page or backgrounding it ends voice mode, stops recording and playback and releases the microphone; a request the PC already took carries on and its reply lands in the chat. Focus moves into the dialog when it opens and returns to the voice mode button when it closes. Voice mode uses the page's theme; the orb keeps its own dark backdrop in both, as on the desktop.
- **Orb.** A canvas port of the desktop orb (`orb_widget.spec.md`, the sphere of light), shown only in voice mode: the same points, depth, turning, voice-driven surface, thinking band, wake flash, state colours and labels, drawn with additive light. It animates with `requestAnimationFrame` only while the page and the orb are visible and rests at a lower rate when idle, honours reduced motion (a still sphere with the state colour), and reads `offline` with a "can't reach the PC" label when the PC cannot be reached. The point count is lower than on the desktop (3,200) to spare the phone's battery.
- **Confirmation card.** When a desktop confirmation is pending, a card in the dock (and in voice mode, while it is open) shows what Jarvis wants to do with **Approve** and **Deny** and a countdown; while the keyboard is up it drops the consequence line to keep the conversation's room. Answering on the phone or on the desktop resolves the same request; whichever comes first wins and the other is closed. It is absent when `remote_access_allow_confirm` is off.
- **Quick actions.** The buttons from `remote_access_quick_actions`, each sent as a typed request: chips on an empty chat, under the start screen's suggestions, and a bottom sheet behind the lightning button by the message box. There is no permanent row.
- **Settings sheet.** A bottom sheet behind the header's settings button: who answers and which model (the model picker), light and dark, **Read replies aloud** (an opt-in switch, off by default, remembered in the browser's storage), whether voice can work now and why not, and **Unpair** (forgets the token and revokes the device on the PC).
- **Toasts.** In the dock, above the message box, or above voice mode's buttons while it is open.
- **Install.** A web app manifest, icons (an SVG and PNG icons, including an apple touch icon) and `theme-color`, so the page can be added to the home screen.
- **Touch and safe areas.** Controls are at least 44 px and text fields use 16 px text (so iOS does not zoom into them). The page, the drawer, the sheets and voice mode use the device's safe-area insets and the visual viewport's height, so the message box stays above the keyboard; while the keyboard is up the bottom inset is dropped, since the keyboard covers it. Reduced motion stills the animations.
- **Left out.** The PC-microphone button, the Screenshot button, "Delete everything" and the keyboard-shortcut help: the first two act on the PC, the third is a desktop action and the fourth is for hardware keyboards.

The page polls with the long-poll endpoint and backs off (1 s, doubling to 15 s) while the PC is unreachable. It uses no service worker, no web socket and no event stream.

## Desktop dialog

A `Phone Access` tray entry (with a phone line icon) opens a dialog (shared HUD theme, plain text, no emoji) that:

- says whether phone access is on and, when it is off, offers **Turn on**, which writes `remote_access_enabled: true` and asks for a restart;
- lists the addresses to open on the phone: with a certificate configured, `https://<remote_access_tls_host>:<port>/`; otherwise `http://<address>:<port>/` for each private IPv4 address of this PC and its host name, with a note that voice needs HTTPS and the command that sets it up (`python -m jarvis.remote cert`);
- **Pair a phone** shows a new six-digit code and its remaining time;
- lists paired devices with when they were added and last seen, each with **Remove**.

## Logging

Debug logs (`remote` tag) record start, stop, bind failures, whether the server is HTTPS, each connection that failed before its request (never the peer's data), pairing success and failure counts, device revocation, refused client addresses, rejected tokens, and the voice events listed under **Privacy of audio**. They never contain tokens, codes, message text, transcripts, audio, reply text or device names.

## What it does not do

- No access from the public internet. The user reaches the PC over their home network or their own VPN.
- No other certificate source than the local Tailscale in `python -m jarvis.remote cert`.
- No browser or cloud speech services, on either side.
- No push notifications while the page is closed, and no recording while the page is in the background.
- No recording on the PC for a phone request, and nothing spoken at the PC for one.

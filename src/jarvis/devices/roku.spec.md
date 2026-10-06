# Roku TV control

Jarvis controls a Roku TV (including Roku TV models such as onn) on the home network through the Roku External Control Protocol (ECP): plain HTTP on port 8060, no account and no cloud. Two layers: `jarvis.devices.roku` (this module knows nothing about tools, the reply engine or the LLM) and the `tvControl` tool adapter in `jarvis.tools.builtin.tv_control`.

## Boundaries

- **Local network only, no cloud.** The only traffic is HTTP to the configured TV and, for discovery, one SSDP search on the local network. Nothing is sent to any other host and nothing depends on a vendor service.
- **The address is configuration.** `roku_host` (default empty) is the TV's IP address. No address appears in code, defaults, tests, logs or tool results. Empty means the tool is not registered, the fast-path TV phrases are unavailable and the router never sees it.
- **Private addresses only.** `validate_host` accepts an IP literal inside `ALLOWED_NETWORKS`: RFC 1918 (`10/8`, `172.16/12`, `192.168/16`), IPv4 link-local (`169.254/16`) and IPv6 link-local (`fe80::/10`). Hostnames, ports, URLs, zone identifiers, loopback, multicast, public and IPv4-mapped addresses are rejected. The client re-validates in its constructor, so the tool can never be pointed at the internet. A configured value that fails validation is ignored at start-up with a console warning and the tool is not registered.
- **Hardened transport.** Redirects are never followed, system proxy settings are ignored, every request has a 2 second timeout and an overall read deadline, responses are capped at 256 KB, and XML containing a DOCTYPE or entity declaration is refused. App ids are validated before they reach a URL path.
- `windows_tools_enabled` does not affect the TV: the module has no OS dependency and works on any platform.

## Client

`RokuClient` wraps one TV: `device_info`, `apps`, `active_app` (GET) and `key`, `launch`, `type_text` (POST).

- **Keys** are a validated list (`KEYS`: Home, Back, Select, the arrows, Play, Rev, Fwd, InstantReplay, Info, Search, Enter, Backspace, ChannelUp/Down, VolumeUp/Down/Mute, PowerOn/Off, InputTuner, InputHDMI1 to 4, InputAV1), matched case-insensitively. An unknown key sends nothing. `repeat` presses a key several times, capped at `MAX_REPEAT` (10). The protocol has only toggles for play/pause and mute, so `Play` and `VolumeMute` are toggles and results say so.
- **Text** is sent one character at a time as `Lit_<percent-encoded character>` keypresses, at most 100 plain characters. The text is never logged and never echoed in results.
- **Apps** are always read from the TV (`/query/apps`); ids are never hardcoded. Names are tidied of non-breaking spaces. Inputs (HDMI, tuner) appear in the same list and launch the same way.
- **App name resolution** (`resolve_app`) is exact, then unique: an exact name (or exact id) wins; otherwise a name containing every spoken word, if exactly one does. Several matches return the candidates and nothing is launched; no match returns nothing. Spelling is never guessed.

## Device object

`get_device(host)` returns one `RokuDevice` per host. It keeps the TV's serial number, the last app list (`cached_apps`, read without network access so fast-path matching never waits) and recovers when the address moves: if the TV does not answer and its serial is known, an SSDP `roku:ecp` search runs (at most once a minute) and the same serial at a new address is used from then on. A different Roku is never adopted, and without a known serial nothing is searched. The tool tells the user once that the TV moved and that `roku_host` should be updated, without printing the new address. `warm_up` reads identity and apps with GET requests only; the tool starts it on a background thread at registration.

`python -m jarvis.devices.roku --discover` lists Rokus on the network (read-only) so the user can find the address. Reserving the TV's address in the router stops it changing.

## tvControl

One tool with an `action` enum, so the router's catalogue grows by one entry.

| Action | Behaviour |
|--------|-----------|
| `key` | Presses `key`, `repeat` times (default 1) |
| `launch` | Resolves `app` against the TV's list and launches the single match. Ambiguity fails with the candidates; no match fails listing installed apps |
| `type` | Types `text` into the focused on-screen keyboard |
| `status` | Device name and model, power mode, active app and installed apps (GET only) |

- The description starts with the TV and names `systemVolume` and `mediaControl` as the tools for the computer, so "turn the TV volume down" and "turn the volume down" route differently.
- Every action is routine and `SAFE`, power off included: no confirmation. Central safety, retries and redaction apply as for every tool.
- Results are raw facts. Failures are honest and never replaced by success:
  - **Control by mobile apps limited or disabled.** The TV answers 403. The message says where the setting is: Settings > System > Advanced system settings > Control by mobile apps > Network access, set to Default or Permissive.
  - **Unreachable.** The TV is off (a Roku TV listens only when on or in standby), or its address changed.
  - **Unusable `roku_host`.** Not a private network address.
- In Codex or Claude reply modes the tool is offered like the other local tools; its results (power state, app names) are visible to that model only because the user chose that mode.
- Every successful action records the TV as a desktop referent (`memory/desktop_referents.spec.md`) with the action, and the key name for `key`, so "turn it off" or "make it louder" after a TV command reaches the TV. Typed text and app names are never recorded.

## Fast path

Locale phrases in `fastpath/phrases/<language>.json` (family `tv`) route whole utterances that name the TV: turn the TV on or off, TV volume up or down (several steps per phrase, `repeat` in the rule), mute or unmute the TV (one toggle), pause, play or resume the TV (one toggle), TV home, and "put on {tv_app}", "open {tv_app} on the TV" and the like. Phrases without "TV" keep going to `systemVolume`, `mediaControl` and `appControl`. The `tv_app` slot resolves against the cached app list (`tv_apps`), with the same similarity rules as application names; a name that is not cached, or is ambiguous, falls through to the model path. The route exists only while `tvControl` is registered.

## Testing

All behaviour is tested against a fake ECP server on loopback (`tests/fake_roku.py`, the `fake_tv` fixture). Automated runs never send a keypress, launch or text to a real TV: `tests/conftest.py` refuses any non-loopback request, and permits only GET to a real device in `integration` tests. Checks that press keys on the real TV are `interactive` (`tests/test_roku_real_device.py`), with the address given by `JARVIS_TEST_ROKU_HOST`.

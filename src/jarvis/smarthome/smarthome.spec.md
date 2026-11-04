# Smart home

Jarvis controls lights, plugs, fans, blinds, thermostats, locks, sensors and home-made devices through standard, self-hostable hubs rather than through each manufacturer. A device reaches Jarvis through one of two connections:

- **Home Assistant** (`smarthome_ha_*`): the user's own Home Assistant server on the home network. Anything Home Assistant controls (Govee, Hue, Tuya and Feit, Kasa, Matter, Zigbee, ESPHome boards and thousands more) becomes available to Jarvis in one step.
- **MQTT** (`smarthome_mqtt_*`): the user's own MQTT broker. Devices that announce themselves in the Home Assistant MQTT discovery format (ESPHome, Tasmota, WLED, Zigbee2MQTT and most hobby firmware) appear without Home Assistant.

Both feed one device model and one tool, `homeControl`, so "turn off the kitchen lights" works the same whatever is behind it. Roku TVs keep their own direct control (`devices/roku.spec.md`).

Four layers, each knowing nothing of the layers above it:

1. `jarvis.smarthome.model`: the shared device model (`Device`, `Capability`, the registry). No network code.
2. `jarvis.smarthome.home_assistant` and `jarvis.smarthome.mqtt`: one connector each. They turn hub data into `Device` objects and `Device` commands into hub calls. No tool, reply engine or LLM knowledge.
3. `jarvis.tools.builtin.home_control`: the `homeControl` tool adapter.
4. Settings page, fast-path family and `python -m jarvis.smarthome`.

## Boundaries

- **Off by default.** `smarthome_enabled` is false. While it is false no connector starts, no socket opens, `homeControl` is not registered, the fast-path family is unavailable and the router never sees it.
- **Local network only, no cloud.** Jarvis talks only to the configured Home Assistant server and MQTT broker. The address of each must be loopback, RFC 1918, IPv4 link-local or IPv6 link-local (the `ALLOWED_NETWORKS` rule of the Roku spec, with loopback added because a Home Assistant virtual machine may be reached through a forwarded port). A hostname such as `homeassistant.local` is accepted only if every address it resolves to is allowed, and the connection is made to the resolved address so a later DNS change cannot point it elsewhere. Public addresses, remote-access URLs and proxies are refused, and a refused setting leaves the connector off with a console warning.
- Whether a device itself uses a vendor cloud is decided by Home Assistant, not by Jarvis. Jarvis never contacts a manufacturer.
- **Secrets in Credential Manager only.** The Home Assistant long-lived access token and the MQTT password are stored with `jarvis.credentials` (`smarthome.ha_token`, `smarthome.mqtt_password`). They never appear in `config.json`, logs, tool results, issue reports or the web chat.
- **The user decides what Jarvis sees.** From Home Assistant, Jarvis uses only entities the user has exposed to voice assistants in Home Assistant (Settings > Voice assistants > Expose). From MQTT, every discovered device is visible unless listed in `smarthome_mqtt_hidden`. Hidden or unexposed devices are never listed, named or controlled.
- **No telemetry.** Jarvis sends commands and reads states. It never uploads device lists or history anywhere.
- **Home turns are private.** Every `homeControl` call marks the turn private (the `activity_log.spec.md` mechanism, as calendar, email and document search do): device names, rooms and states stay out of the diary, the graph and the console log.
- **Bounded.** Every request has a timeout (5 seconds for a command, 10 for the initial device list). At most 2,000 devices are kept. Names are trimmed to 100 characters. Reconnects back off from 2 seconds to 5 minutes.

## Device model

A `Device` has a stable `id` (`ha:<entity_id>` or `mqtt:<unique_id>`), a display `name`, optional `area` (room), optional `aliases` (from Home Assistant voice aliases), a `kind`, its current `state` and a set of `Capability` values.

| Kind | Capabilities |
|------|--------------|
| `light` | on/off, brightness (0 to 100 %), colour (named colour or RGB), colour temperature (warm to cool) |
| `switch` (plugs, relays) | on/off |
| `fan` | on/off, speed (0 to 100 %) |
| `cover` (blinds, curtains, garage doors, gates) | open, close, stop, position (0 to 100 %) |
| `climate` | target temperature, mode (heat, cool, auto, off) |
| `lock` | lock, unlock |
| `alarm` | arm, disarm |
| `sensor` / `binary_sensor` | read only (temperature, humidity, door open, motion, battery, ...) |
| `scene` | activate |
| `script` | run |
| `button` | press |

Anything else a hub offers is ignored. Media players are not included: TVs and speakers stay with `tvControl` and `mediaControl` so requests do not split between tools.

States come from the hub's live updates (Home Assistant `state_changed` events, MQTT retained state topics), so reading a state never waits on the network. A device whose hub is disconnected reports `unavailable`.

### Name resolution

`resolve(target, area)` turns spoken words into devices with the same exact-then-unique rule as Roku apps: an exact name, alias or id wins; otherwise a device whose name contains every spoken word, if exactly one does. An area alone ("the kitchen") with a kind ("lights") means every device of that kind in that area. Several candidates without an area return the candidates and nothing is changed. Nothing is guessed, and words are compared after Unicode case folding, never against a list of words in one language.

## Connectors

### Home Assistant

- Settings: `smarthome_ha_url` (for example `http://192.168.1.50:8123`) and the token in Credential Manager. The token must belong to an administrator: Home Assistant lists exposed entities only to administrators, and Jarvis says so when the token is not one.
- One WebSocket connection to `/api/websocket` (`websocket-client`, imported lazily). After `auth`, it reads `get_states`, the entity, device and area registries and the exposed-entity list, then subscribes to `state_changed`. Commands are `call_service` messages (`light.turn_on`, `cover.open_cover`, ...).
- The connection runs on one daemon thread. Commands from the tool wait on a reply for at most 5 seconds. The reader thread never runs tools. Reading the device list after connecting is bounded to 10 seconds in all. After 60 seconds of silence Jarvis sends a ping; two unanswered pings drop the connection and the reconnect loop takes over.
- Jarvis also subscribes to `entity_registry_updated` and reads the device list again when it fires (exposing or un-exposing an entity changes its registry options), so a change in Home Assistant's Expose page reaches Jarvis without a reconnect. Before each command it checks the exposed-entity list again: an entity no longer exposed is removed and the command is refused.
- Hostnames are resolved by the connector on each connection attempt, so a name that does not resolve yet (Home Assistant still starting at sign-in) is retried with the reconnect backoff and reported as "could not be found yet", not refused. Starting Jarvis only checks the address's syntax and refuses a literal public address. The socket is opened to the resolved address (TLS checked against the name the user typed), never through a proxy.
- Redirects are refused. HTTPS with a self-signed certificate is accepted only if `smarthome_ha_verify_tls` is false, which the Settings page explains.
- Honest failures: wrong token ("Home Assistant refused the access token, create a new one"), a token that is not an administrator's, unreachable server, address not found yet, device not exposed, Home Assistant rejected the command (its message is passed on, trimmed and fenced as untrusted data).

### MQTT

- Settings: `smarthome_mqtt_host`, `smarthome_mqtt_port` (default 1883), `smarthome_mqtt_username`, the password in Credential Manager, `smarthome_mqtt_tls` (default false) and `smarthome_mqtt_discovery_prefix` (default `homeassistant`).
- `paho-mqtt`, imported lazily, on its own network thread. The broker's name is resolved on a background thread that retries a name that does not resolve yet; the client then connects to that address (a socket override, never a proxy), and paho's own reconnects reuse it. The port is always `smarthome_mqtt_port` (1883 unless set), the same value the Settings test uses; a broker with TLS usually listens on 8883, which the Settings hint says. A connection test counts announced devices only (it never subscribes to state topics) and ends within 5 seconds whatever the broker sends. Jarvis subscribes to `<prefix>/+/+/config` and `<prefix>/+/+/+/config`, builds devices from the discovery payloads it understands (light, switch, fan, cover, climate, lock, sensor, binary_sensor, button, scene), and subscribes to each device's state topic.
- A command is published to the device's command topic in the format its discovery payload declares (JSON schema or plain payloads). Jarvis publishes nothing else and never retains a command.
- Discovery payloads are untrusted: unknown fields are ignored, sizes are capped at 16 KB, and topic names are validated before use (no wildcards in publish topics).
- A device without a `unique_id` is identified by its discovery topic, node id included, so two nodes announcing the same object id stay two devices.
- If `websocket-client` or `paho-mqtt` is missing, that connector does not start, the console says which package to install, and the other connector starts anyway. No connector can stop Jarvis starting.
- When both connectors expose the same physical device (an MQTT device already added to Home Assistant), the Home Assistant copy wins and the MQTT copy is hidden, matched by the shared `unique_id`.

## homeControl

One tool with an `action` enum.

| Action | Behaviour |
|--------|-----------|
| `list` | Devices, optionally filtered by `area` or `kind`: name, area, kind, state |
| `status` | State of one device or of every device of a kind in an area ("is the garage door open", "what's the temperature in the bedroom") |
| `turn_on` / `turn_off` / `toggle` | For lights, switches and fans |
| `set` | `brightness`, `colour`, `colour_temperature`, `speed`, `position` or `temperature`, whichever the device supports |
| `open` / `close` / `stop` | Covers |
| `lock` / `unlock` | Locks |
| `arm` / `disarm` | Alarms |
| `activate` | Scenes, scripts and buttons |

- The description starts with home devices (lights, plugs, blinds, thermostats) and points to `tvControl` for the TV and `systemVolume` or `mediaControl` for the computer.
- Results are raw facts: which devices changed and their new state, or why nothing changed. A command counts as done only when the hub confirms it (Home Assistant's `result`, or for MQTT a state update within 3 seconds; with no state topic the result says the command was sent but not confirmed).
- **Safety tiers:**
  - `SAFE`: everything in `list` and `status`; lights, switches, fans and scenes; covers other than garage doors and gates; `lock`; `arm`; climate within 10 to 30 °C (50 to 86 °F).
  - `CONFIRM_VOICE`: `unlock`; `disarm`; opening a cover whose device class is `garage`, `gate` or `door`; climate outside that range; `script` and `button` (their effect is unknown to Jarvis); any command that would change more than 10 devices at once.
  - The user can raise any device to always confirm with `smarthome_confirm_devices` (a list of device ids). Nothing can be lowered below these tiers.
- In Codex or Claude reply modes the tool is offered only if `smarthome_share_with_cloud` is true (default false), because device names, rooms and states describe the user's home. When false the tool is in the bridge's excluded set for that mode, and its desktop referents are left out of the request's `other_referents` even while `<mode>_share_desktop_referents` is on.
- Every successful action records the devices as desktop referents (`memory/desktop_referents.spec.md`), so "turn them off" or "a bit dimmer" after a lights command reaches the same devices. Only device ids are recorded.
- `step_note` words a call as "Turned off 3 lights" or "Set the bedroom temperature", never with addresses.
- Device names and states are untrusted text from the hub: they are fenced as data in results and never treated as instructions. So are the hub's reasons for rejecting a command.
- A failure carries a short plain `error_message` in Jarvis's own words (what the fast path says); the fenced device data is only in the text for the model. A command that changed some devices but not all is reported as not done ("Changed 2 of 3 devices; ..."), with each device's outcome.
- A `CONFIRM_VOICE` request names a count ("1 smart home device") as its target, never a device name, because targets go through the central file-path checks; the names are in the consequence.
- The tool's arguments name devices and rooms, so the fast path logs only their keys (`log_arguments` is false).

## Fast path

Locale phrases in `fastpath/phrases/<language>.json` (family `home`) route whole utterances such as "turn on {home_target}", "turn off the {area} lights", "set {home_target} to {percent} percent", "dim {home_target}". `home_target` resolves against the in-memory device names, aliases and areas with the name resolution above, and the device's own capabilities must allow the action. An unknown or ambiguous target, or any `CONFIRM_VOICE` action, falls through to the model path. Home rules are matched together with application names, after the fixed phrases (TV, volume, time): a TV phrase is never shadowed by a device whose name holds "TV", and an utterance that could mean an application or a device ("open office" with an Office Blind) falls through to the model. The family exists only while `homeControl` is registered.

## Settings page

A **Smart Home** page in Settings:

- An on/off switch for the whole feature.
- **Home Assistant**: address, a field to paste the access token (saved straight to Credential Manager, shown as "saved" afterwards, never displayed again), a **Test connection** button that reports the number of exposed devices or the exact failure, and a link to the setup guide (`docs/SMART_HOME_SETUP.md`).
- **MQTT**: host, port, username, password, TLS, discovery prefix and the same Test button.
- **Devices**: the current device list (name, room, kind, source, state) with a "hide" option for MQTT devices and an "always confirm" option for any device.
- A share-with-cloud switch with a plain explanation.

Nothing is saved unless its test passes, matching the calendar and email sign-ins. Changed connection settings need a passing test whether or not the feature is on, so turning it on later never skips one.

## Command line

`python -m jarvis.smarthome test` checks both connectors and prints the device count per source. `python -m jarvis.smarthome devices` lists devices (name, room, kind, state). Neither prints tokens, passwords or addresses beyond what the user typed.

## Logging

`debug_log` records connector start and stop, connection failures by category, device counts, and each command's tool action, kind and outcome. Device names, rooms, entity ids, topics, addresses and tokens are never logged.

## Testing

- Connectors are tested against a fake Home Assistant WebSocket server and an in-process fake MQTT broker on loopback (`tests/fake_home_assistant.py`, `tests/fake_mqtt.py`). Automated runs never contact a real hub; `tests/conftest.py` refuses non-loopback connections.
- Behaviour tests cover name resolution, safety tiers, the exposed-entity filter, duplicate removal, confirmation of commands by state, address validation and secret handling.
- A routing eval (`evals/test_home_routing.py`) checks that home requests reach `homeControl` and TV, volume and media requests do not.
- Checks against the user's real Home Assistant are `interactive`, with the address and token taken from the environment.

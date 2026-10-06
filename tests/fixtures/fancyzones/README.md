# FancyZones fixtures

`applied-layouts.json`, `layout-templates.json` and `default-layouts.json` are copies of the files PowerToys
writes under `%LOCALAPPDATA%\Microsoft\PowerToys\FancyZones\`, with monitor identifiers, instance paths,
serial numbers and GUIDs replaced by stand-ins. Structure, layout types and the stale entries for other virtual
desktops and disconnected monitors are kept.

The source machine had no custom layouts, so `custom-layouts.json` and `settings.json` are hand-written from the
key names PowerToys reads (`custom-layouts`, `info`, `cell-child-map`, `ref-width`, `X`, ...).

Fixture identities used by the tests:

| Display | hardware id | instance | serial | number |
|---------|-------------|----------|--------|--------|
| `\.\DISPLAY1` | `TST1001` | `5&00000001&0&UID4301` | `SN00000001` | 1 |
| `\.\DISPLAY2` | `TST1002` | `5&00000004&0&UID4304` | `SN00000002` | 2 |
| `\.\DISPLAY17` | none (device name) | none | none | 17 |

Current virtual desktop: `{00000000-0000-0000-0000-0000FACE0001}`.

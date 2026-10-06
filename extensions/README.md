# Local extensions

Put your own additions to Jarvis here, one folder each, without changing Jarvis's code. Updates to Jarvis then merge without touching them. An extension can add tools, fast voice commands, a Settings page, an extra place for Jarvis's voice to play (such as a robot's speaker) and work that starts and stops with Jarvis.

Nothing in this folder runs unless you name it in `config.json`:

```json
{ "extensions_enabled": ["my_extension"] }
```

A minimal extension, `extensions/my_extension/__init__.py`:

```python
from jarvis.extensions import SettingField
from jarvis.tools.base import Tool
from jarvis.tools.types import ToolExecutionResult


class Hello(Tool):
    name = "helloThere"
    description = "Say hello from my extension."
    inputSchema = {"type": "object", "properties": {}}

    def run(self, args, context):
        return ToolExecutionResult(success=True, reply_text="Hello from my extension.")


def register(api):
    api.add_settings("My Extension", [SettingField("my_extension_greeting", "Greeting", "What to say.", "str", "Hello")])
    api.add_tool(Hello())
```

- `register(api)` only declares things. Open devices, sockets or threads in `api.on_start(...)` and close them in `api.on_stop(...)`.
- Your tools go through Jarvis's safety checks like any other: override `classify_safety` to ask before anything destructive.
- Keep the extension's spec, tests and documents in its folder. Device credentials belong in files Git ignores (`wifi_config.h` is ignored anywhere under `extensions/`).

The full contract is `src/jarvis/extensions/extensions.spec.md`.

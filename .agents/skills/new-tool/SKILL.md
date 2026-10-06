---
name: new-tool
description: >
  Checklist for extending Jarvis: adding a built-in Python tool, or deciding
  that a routine, workspace, fast-path family or MCP server is the better fit.
  Use when someone asks to "add a tool", "give Jarvis a new ability", "make an extension", "make a
  routine/workspace", or "add a fast command".
---

# Extending Jarvis

This is a checklist of touchpoints, not a copy of the rules. The linked specs
own the details; read them, do not work from this file alone.

## Step 1. Pick the smallest thing that works

| The need | Build | Reference |
|----------|-------|-----------|
| A chain of things Jarvis can already do | **Routine**, no code. Done by voice ("save that as movie mode") or in `config.json` | `docs/CONFIGURATION.md` → Routines; `src/jarvis/routines/routines.spec.md` |
| Open and place a set of apps, files and sites | **Workspace**, no code, `windows_workspaces` in config | `docs/CONFIGURATION.md` → Workspaces; `src/jarvis/platform/windows/workspaces.spec.md` |
| An outside service or app with an MCP server | **MCP server** in config, no code | `docs/CONFIGURATION.md` → MCP Integrations; `src/jarvis/tools/external/mcp_runtime.spec.md` |
| A command phrased one obvious way that an existing SAFE tool already handles, but slowly | **Fast-path family** | `docs/CONFIGURATION.md` → Adding a Deterministic Command Family; `src/jarvis/fastpath/fastpath.spec.md` |
| A personal or hardware-specific feature that should not ship in shared Jarvis (your own device, a private service) | **Local extension** in `extensions/<name>/`: tools, phrases, a settings page, a voice output, start and stop, with no change to Jarvis's files | `extensions/README.md`; `src/jarvis/extensions/extensions.spec.md` |
| A new capability nothing above covers, useful to everyone | **Built-in tool**, see Step 2 | |

Before writing a tool, check that no existing tool should gain an action
instead. One tool with an `action` enum (as in `windowControl` or `tvControl`)
keeps the router's catalogue short. Every extra tool makes routing harder for
every request.

## Step 2. Built-in tool checklist

Good models to copy: `src/jarvis/tools/builtin/tv_control.py` (thin tool over
a separate client, conditional registration, fast-path targets) and commit
`5651f5f`, which shows every file a complete tool addition touched.

### Design first
- [ ] Find the related spec (see the registry in `AGENTS.md`). Either extend it
      or write `<area>.spec.md` next to the code. Ask the user before departing
      from an existing spec.
- [ ] Keep OS or network logic in its own module (`platform/windows/`,
      `devices/`, ...) with no tool or LLM knowledge. The tool is a thin layer.
- [ ] Offline first: no required cloud service, no telemetry, nothing personal
      leaves the machine.

### The tool (`src/jarvis/tools/builtin/<name>.py`, interface in `tools/base.py`)
- [ ] `name`: camelCase.
- [ ] `description`: the router reads only the **first 120 characters**
      (`tools/selection.py`), so the deciding words go first. Add "NOT for X,
      use Y" pointers when a neighbouring tool overlaps.
- [ ] `inputSchema`: MCP-style JSON Schema. Prefer an `action` enum; mark
      optional fields as optional in their descriptions.
- [ ] `run()` returns raw data in a `ToolExecutionResult`. No LLM calls and no
      formatting for the user; the system prompt handles that.
- [ ] `classify_safety()`: the default is SAFE. Override for anything
      destructive (CONFIRM_VOICE) or forbidden (DENY). See the confirmation
      policy in `AGENTS.md` and `tools/confirmation.py`.
- [ ] `returns_outside_content` (or `returns_outside_content_for`) is True if
      results carry web, file, screen or other-app content. Fence such content
      as untrusted data.
- [ ] No hardcoded language patterns. Words the tool must understand go in a
      locale phrase file (`tools/phrases/<lang>.json` or
      `fastpath/phrases/<lang>.json`).
- [ ] `debug_log` at the important decision points only. Never log names,
      paths, titles, URLs or content.
- [ ] Every OS or network call is time-bounded.

### Wiring
- [ ] Register in `src/jarvis/tools/registry.py`: in `BUILTIN_TOOLS`, or in a
      `configure_<name>_tool(cfg)` called from `daemon.py` when it depends on a
      setting or platform.
- [ ] Settings: a field in `config.py` (dataclass, `get_default_config`,
      `load_settings`), a `FieldMeta` entry in `desktop_app/settings_window.py`,
      and `examples/config.json` if users are expected to set it.
- [ ] Reply bridges (`src/jarvis/bridge/bridge.spec.md`, `bridge/tools.py`):
      decide whether a cloud model may use it. Personal-data tools join
      `PERSONAL_DATA_TOOLS`; tools that widen the catalogue or switch providers
      join `ALWAYS_EXCLUDED_TOOLS`.
- [ ] Desktop referents (`memory/desktop_referents.spec.md`) if follow-ups
      such as "turn it off" should reach what the tool acted on.
- [ ] Fast path (optional): a phrase family, plus `fast_targets(cfg)` on the
      tool if matching needs live names. SAFE actions only.
- [ ] Routines get the tool automatically. Check that its arguments make sense
      as a saved step (`routines.spec.md`).

### Tests (TDD: write them first)
- [ ] Unit tests for the client or OS module with fakes; never drive real
      hardware or the user's own windows in the default test run.
- [ ] Tool behaviour tests: results, failures, safety tiers.
- [ ] A routing eval in `evals/` (see `evals/test_tv_routing.py`) with
      utterances that must and must not reach the new tool, including the
      neighbours it could steal requests from.
- [ ] `.mamba_env/python.exe -m pytest -q` and the smoke test from `AGENTS.md`
      both pass.

### Docs (same commit)
- [ ] The spec file, and a row in the `AGENTS.md` spec registry if it is new.
- [ ] `docs/llm_contexts.md`: the tool changes the router catalogue, so add or
      adjust its entry under the tool router context.
- [ ] `README.md` "What you can do", and `docs/CONFIGURATION.md` for any setting.
- [ ] British English, no em dashes, describe the present state rather than
      history.

### Finish
- [ ] Run it for real where you can. Parts that depend on the desktop PC's
      hardware or apps are listed as unverified in the summary.
- [ ] Conventional Commit, e.g. `feat(<area>): <what the user can now do>`.

"""The tool surface and answer contract every bridge offers its model.

One host tool, ``jarvis_execute``, runs one allowed Jarvis tool for the request. Its definition
lists the request's allowed-tool snapshot. The final message is an answer object in
``ANSWER_SCHEMA``. See ``bridge.spec.md``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..debug import debug_log
from ..memory import activity_runtime

EXECUTE_TOOL = "jarvis_execute"
# Local routing helpers and the reply-mode switch are never offered: a cloud model must not widen
# its own catalogue or move the conversation to another provider.
ALWAYS_EXCLUDED_TOOLS = frozenset({"toolSearchTool", "refreshMCPTools", "replyMode"})
PERSONAL_DATA_TOOLS = frozenset({"logMeal", "fetchMeals", "deleteMeal"})
# Offered to a cloud model only while ``activity_runtime.shared_with_cloud`` allows it.
ACTIVITY_TOOLS = frozenset({"activityLog"})
_DESCRIPTION_CHARS = 600
_REQUEST_ID = {"type": "string", "description": "The request ID from the turn input."}
_EXECUTE_DESCRIPTION = ("Run one allowed Jarvis tool for this request. Allowed tools (name: description. "
                        "Arguments: input schema):")
_CATALOGUE_DESCRIPTION = ("Run one allowed Jarvis tool for this request. Set tool_name to one of the allowed "
                          "tools and arguments to match that tool. Each allowed tool's description and "
                          "arguments are the entry of the arguments anyOf whose title is its name.")
_CATALOGUE_ARGUMENTS = "Arguments for tool_name: the anyOf entry titled with that tool's name."

# The turn's final assistant message is the answer Jarvis delivers.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "needs_user_input", "failed"]},
        "reply": {"type": "string", "description": "Concise British English answer for the user."},
    },
    "required": ["status", "reply"],
    "additionalProperties": False,
}


@dataclass
class BridgeOutcome:
    # reply | question | awaiting_confirmation | error | cancelled
    kind: str
    text: str = ""
    reason: Optional[str] = None


def execute_input_schema(tools: Dict[str, Dict[str, Any]], *, catalogue: bool = False) -> Dict[str, Any]:
    """The ``jarvis_execute`` input schema. With ``catalogue`` the ``arguments`` schema also carries each
    allowed tool (title = name, its description and input schema), for a client that cuts long tool
    descriptions but passes the input schema whole (see ``execute_catalogue_description``)."""
    tool_name: Dict[str, Any] = {"type": "string", "description": "One of the allowed tools."}
    if tools:
        tool_name["enum"] = list(tools)
    arguments: Dict[str, Any] = {"type": "object", "description": "Arguments matching that tool's input schema."}
    if catalogue and tools:
        arguments["description"] = _CATALOGUE_ARGUMENTS
        arguments["anyOf"] = [{**(entry.get("inputSchema") or {}), "title": name,
                               "description": entry.get("description") or ""}
                              for name, entry in tools.items()]
    return {"type": "object", "properties": {
        "request_id": _REQUEST_ID,
        "tool_name": tool_name,
        "arguments": arguments},
        "required": ["request_id", "tool_name", "arguments"], "additionalProperties": False}


def execute_catalogue_description() -> str:
    """The short ``jarvis_execute`` description that goes with ``execute_input_schema(catalogue=True)``.

    Measured on Claude Code 2.1.288: the model sees only the first 2048 characters of an MCP tool
    description but the whole input schema, so the catalogue belongs in the schema there."""
    return _CATALOGUE_DESCRIPTION


def execute_tool_description(tools: Dict[str, Dict[str, Any]]) -> str:
    """The ``jarvis_execute`` description listing the allowed tools.

    Measured on codex-cli 0.159: the model uses a catalogue it reads in a tool definition, but skips
    tools listed only in the turn text or context, so the catalogue belongs here.
    """
    lines = [_EXECUTE_DESCRIPTION] + [
        f"- {name}: {entry.get('description') or ''} Arguments: "
        f"{json.dumps(entry.get('inputSchema') or {}, ensure_ascii=False, sort_keys=True)}"
        for name, entry in tools.items()]
    return "\n".join(lines)


def build_tool_snapshot(cfg: Any, *, share_long_term_memory: bool, log_tag: str = "bridge"
                        ) -> Dict[str, Dict[str, Any]]:
    """Tools a bridge may use for one request: name -> description and input schema."""
    from ..tools.registry import BUILTIN_TOOLS, get_cached_mcp_tools

    snapshot: Dict[str, Dict[str, Any]] = {}
    for name, tool in BUILTIN_TOOLS.items():
        personal = name in PERSONAL_DATA_TOOLS or getattr(tool, "personal_data", False) is True
        if name in ALWAYS_EXCLUDED_TOOLS or (personal and not share_long_term_memory):
            continue
        if name in ACTIVITY_TOOLS and not activity_runtime.shared_with_cloud(cfg):
            continue
        snapshot[name] = {
            "description": (tool.description or "")[:_DESCRIPTION_CHARS],
            "inputSchema": tool.inputSchema,
        }
    if getattr(cfg, "mcps", None):
        try:
            for name, spec in get_cached_mcp_tools().items():
                snapshot[name] = {
                    "description": (spec.description or "")[:_DESCRIPTION_CHARS],
                    "inputSchema": spec.inputSchema,
                }
        except Exception as exc:
            debug_log(f"MCP tools unavailable for bridge snapshot: {type(exc).__name__}", log_tag)
    return snapshot


def parse_answer(text: Any, log_tag: str = "bridge") -> Optional[Tuple[Any, Any]]:
    """``(status, reply)`` from the final message, or None when it is not the answer object."""
    if isinstance(text, dict):
        data = text
    else:
        if not text:
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            debug_log("final message is not the answer object", log_tag)
            return None
    if not isinstance(data, dict):
        return None
    return data.get("status"), data.get("reply")

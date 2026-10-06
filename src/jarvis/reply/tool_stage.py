"""Tool-model mode: the short prompt, memory tool and phase handover for a local reply whose tools are
chosen by a dedicated tool model and whose words are written by the chat model.

The reply engine runs the phases inside its normal loop, so tool calls keep every guard and the central
safety path. See ``reply.spec.md`` (Tool-Model Mode).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# The engine's own memory lookup, offered to the tool model in place of the planner's searchMemory step.
RECALL_TOOL = "recallMemory"
# At most this many tool calls run in the tool phase before the reply phase starts.
TOOL_PHASE_MAX_CALLS = 6
# Tools the tool model is not offered: it sees the whole catalogue, so there is nothing to widen.
EXCLUDED_TOOLS = frozenset({"toolSearchTool", "refreshMCPTools"})

TOOL_STAGE_PROMPT = (
    "You choose and call tools for the user's request on their Windows PC; another model writes the "
    "spoken reply afterwards. Call the tool that does what was asked, with arguments taken from the "
    "request and the conversation. An action happens only through its tool call: never treat it as done "
    "without the tool's result. Use only displays, zones, windows and files that the request or a tool "
    "result names; never invent one. Call recallMemory only when the answer depends on what the user said "
    "in earlier conversations. When the request needs no tool, or is truly ambiguous, call nothing and "
    "write one short note saying so (a clarifying question if one is needed). After the tool results, "
    "write one short factual note of what happened."
)

RECALL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RECALL_TOOL,
        "description": ("Look up what the user said in earlier conversations (their preferences, plans and "
                        "facts they told you). Only when the answer depends on it."),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {"type": "array", "items": {"type": "string"},
                             "description": "Two to five content words to search past conversations for."},
                "questions": {"type": "array", "items": {"type": "string"},
                              "description": "Optional short questions the answer depends on, e.g. "
                                             "'What instrument does the user play?'"},
                "from": {"type": "string", "description": "Optional ISO date: earliest conversation to search."},
                "to": {"type": "string", "description": "Optional ISO date: latest conversation to search."},
            },
            "required": ["keywords"],
        },
    },
}


def tool_stage_system_message(*, referents: str = "") -> str:
    """The tool phase's system message. The time/location line is added by the engine per turn. The warm
    profile is left out: stored directives made the tool model ask instead of act."""
    parts = [TOOL_STAGE_PROMPT]
    if referents:
        parts.append("\n" + referents)
    return "\n".join(parts)


def recall_arguments(args: Any) -> Optional[Dict[str, Any]]:
    """``keywords``, ``questions``, ``from`` and ``to`` from a recallMemory call, or ``None`` when no
    usable keyword was given."""
    if not isinstance(args, dict):
        return None

    def strings(value) -> List[str]:
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value or [] if str(item).strip()][:8] if isinstance(value, list) else []

    keywords = strings(args.get("keywords"))
    if not keywords:
        return None
    dates = {key: str(args[key]).strip() for key in ("from", "to") if str(args.get(key) or "").strip()}
    return {"keywords": keywords, "questions": strings(args.get("questions")), **dates}


def without_reasoning(messages: List[Dict[str, Any]]) -> None:
    """Drop reasoning traces the tool model left on assistant messages before the chat model reads them."""
    for message in messages:
        if message.get("role") == "assistant":
            message.pop("thinking", None)


def handover_note(tools_ran: List[str], closing_text: str, failed: bool) -> str:
    """The user-role note that ends the tool phase and asks the chat model for the reply."""
    if failed:
        summary = "The tool step failed, so no tool ran."
    elif tools_ran:
        summary = "The tool step ran: " + ", ".join(dict.fromkeys(tools_ran)) + "."
    else:
        summary = "The tool step found no tool needed."
    note = f" Its closing note: {closing_text.strip()}" if closing_text.strip() else ""
    return (f"[Jarvis] {summary}{note}\nNow answer the user in your own voice from the conversation and "
            "the tool results above. Do not call tools. Describe an action as done only when a tool "
            "result above says so.")

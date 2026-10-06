from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, Tuple, List
import sys
import re
import requests
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os

from .builtin.screenshot import ScreenshotTool
from .builtin.web_search import WebSearchTool
from .builtin.local_files import LocalFilesTool
from .builtin.fetch_web_page import FetchWebPageTool
from .builtin.nutrition.log_meal import LogMealTool
from .builtin.nutrition.fetch_meals import FetchMealsTool
from .builtin.nutrition.delete_meal import DeleteMealTool
from .builtin.refresh_mcp_tools import RefreshMCPToolsTool
from .builtin.weather import WeatherTool
from .builtin.time_tool import TimeTool
from .builtin.stop import StopTool
from .builtin.tool_search import ToolSearchTool
from .builtin.routine_control import RoutineControlTool
from .request_scope import CallContext, note_outside_content, tool_call
from .types import ToolExecutionResult
from ..config import Settings
from ..debug import debug_log
from ..routines.journal import get_journal

# The MCP client class, imported on first use: the ``mcp`` package (with pydantic, anyio and httpx)
# costs about 0.3 s to import and is never used when no MCP server is configured.
MCPClient = None


def _mcp_client_class():
    global MCPClient
    if MCPClient is None:
        from .external.mcp_client import MCPClient as client_class
        MCPClient = client_class
    return MCPClient


# Registry of all builtin tools
BUILTIN_TOOLS = {
    "screenshot": ScreenshotTool(),
    "webSearch": WebSearchTool(),
    "localFiles": LocalFilesTool(),
    "fetchWebPage": FetchWebPageTool(),
    "logMeal": LogMealTool(),
    "fetchMeals": FetchMealsTool(),
    "deleteMeal": DeleteMealTool(),
    "refreshMCPTools": RefreshMCPToolsTool(),
    "getWeather": WeatherTool(),
    "getTime": TimeTool(),
    "stop": StopTool(),
    "toolSearchTool": ToolSearchTool(),
    "routineControl": RoutineControlTool(),
}


def configure_windows_tools(cfg, *, platform=None, start_index=True):
    """Apply platform/config availability to the shared builtin catalogue at startup."""
    from .builtin.windows.system_tools import SYSTEM_TOOL_CLASSES
    system_tools = [cls() for cls in SYSTEM_TOOL_CLASSES]
    for name in ('appControl', 'windowControl', 'openWebsite', 'openPath', 'workspaceControl', 'uiControl',
                 'pdfNavigate', *(t.name for t in system_tools)):
        BUILTIN_TOOLS.pop(name, None)
    if (platform or sys.platform) != 'win32' or not cfg.windows_tools_enabled:
        return
    from .builtin.windows import (AppControlTool, WindowControlTool, OpenWebsiteTool, OpenPathTool,
                                  WorkspaceControlTool, UiControlTool, PdfNavigateTool)
    for tool in (AppControlTool(), WindowControlTool(), OpenWebsiteTool(), OpenPathTool(), UiControlTool(),
                 PdfNavigateTool(), *system_tools):
        BUILTIN_TOOLS[tool.name] = tool
    # Offered to routers only when the user has defined a workspace.
    if getattr(cfg, 'windows_workspaces', None):
        BUILTIN_TOOLS['workspaceControl'] = WorkspaceControlTool()
    if start_index:
        from ..platform.windows.apps import APP_INDEX
        APP_INDEX.start()


def configure_screen_tool(cfg, *, platform=None):
    """Offer ``screenshot`` only while screen awareness is on; on Windows it also needs the Windows tools."""
    BUILTIN_TOOLS.pop('screenshot', None)
    if getattr(cfg, 'screen_awareness_enabled', True) is not True:
        return
    if (platform or sys.platform) == 'win32' and not getattr(cfg, 'windows_tools_enabled', False):
        return
    BUILTIN_TOOLS['screenshot'] = ScreenshotTool()


def configure_reply_mode_tool(cfg):
    """Offer the reply-mode switch only when at least one cloud reply mode is allowed in Settings."""
    BUILTIN_TOOLS.pop('replyMode', None)
    if getattr(cfg, 'codex_enabled', False) is True or getattr(cfg, 'claude_enabled', False) is True:
        from .builtin.reply_mode import ReplyModeTool
        BUILTIN_TOOLS['replyMode'] = ReplyModeTool()


def configure_activity_log_tool(cfg, *, platform=None):
    """Offer ``activityLog`` only when the user has turned the opt-in activity log on (Windows only)."""
    BUILTIN_TOOLS.pop('activityLog', None)
    if (platform or sys.platform) == 'win32' and getattr(cfg, 'activity_log_enabled', False) is True:
        from .builtin.activity_log import ActivityLogTool
        BUILTIN_TOOLS['activityLog'] = ActivityLogTool()


def configure_tv_tool(cfg, *, warm=True):
    """Offer ``tvControl`` only when ``roku_host`` is a private network address.

    With ``warm`` the TV's identity and app list are read once in the background (GET requests only) so
    fast-path matching can resolve app names without waiting on the network."""
    BUILTIN_TOOLS.pop('tvControl', None)
    host = (getattr(cfg, 'roku_host', '') or '').strip()
    if not host:
        return
    from ..devices import roku
    try:
        device = roku.get_device(host)
    except roku.RokuAddressError:
        debug_log('roku_host ignored: not a private network address.', 'tv')
        print('  ⚠️ roku_host ignored: it must be an IP address on your home network (such as 192.168.x.x).',
              flush=True)
        return
    from .builtin.tv_control import TvControlTool
    BUILTIN_TOOLS['tvControl'] = TvControlTool()
    if warm:
        threading.Thread(target=device.warm_up, name='roku-warm-up', daemon=True).start()


if sys.platform == 'win32':
    from .builtin.windows import (AppControlTool, WindowControlTool, OpenWebsiteTool, OpenPathTool, UiControlTool,
                                  PdfNavigateTool)
    from .builtin.windows.system_tools import SYSTEM_TOOL_CLASSES
    for _tool in (AppControlTool(), WindowControlTool(), OpenWebsiteTool(), OpenPathTool(), UiControlTool(),
                  PdfNavigateTool(),
                  *(cls() for cls in SYSTEM_TOOL_CLASSES)):
        BUILTIN_TOOLS[_tool.name] = _tool

# Global MCP tools cache
_mcp_tools_cache: Dict[str, "ToolSpec"] = {}
_mcp_tools_cache_lock = threading.Lock()
_mcp_config_cache: Dict[str, Any] = {}
# Clear while a start-up discovery runs in the background.
_mcp_discovery_done = threading.Event()
_mcp_discovery_done.set()


def initialize_mcp_tools(mcps_config: Dict[str, Any], verbose: bool = True) -> Tuple[Dict[str, "ToolSpec"], Dict[str, str]]:
    """
    Initialize MCP tools cache at startup.

    Args:
        mcps_config: MCP server configuration
        verbose: Whether to print status messages

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    global _mcp_tools_cache, _mcp_config_cache

    with _mcp_tools_cache_lock:
        _mcp_config_cache = mcps_config or {}
        _mcp_tools_cache, errors = discover_mcp_tools(mcps_config)

        if verbose and _mcp_tools_cache:
            debug_log(f"MCP tools cache initialized with {len(_mcp_tools_cache)} tools", "mcp")

        return _mcp_tools_cache.copy(), errors


def start_mcp_discovery(
    mcps_config: Dict[str, Any],
    on_done: Optional[Callable[[Dict[str, "ToolSpec"], Dict[str, str]], None]] = None,
) -> threading.Thread:
    """Fill the MCP tools cache on a background thread so start-up goes on meanwhile.

    Readers of the cache wait until discovery has finished; ``on_done`` receives the
    tools and per-server errors first. The client module is imported here, on the
    caller's thread, so its DLL loads never race the speech models' on the Windows
    loader lock.
    """
    try:
        _mcp_client_class()
    except Exception:
        pass  # discovery reports the missing client as an error
    _mcp_discovery_done.clear()

    def _discover() -> None:
        tools: Dict[str, ToolSpec] = {}
        errors: Dict[str, str] = {}
        try:
            tools, errors = initialize_mcp_tools(mcps_config, verbose=False)
            debug_log(f"MCP tools cached: {len(tools)} total", "mcp")
        except Exception as exc:
            errors = {"_global": str(exc)}
            debug_log(f"MCP discovery failed: {exc}", "mcp")
        try:
            if on_done is not None:
                on_done(tools, errors)
        finally:
            _mcp_discovery_done.set()

    thread = threading.Thread(target=_discover, name="mcp-discovery", daemon=True)
    thread.start()
    return thread


def get_cached_mcp_tools(wait: bool = True) -> Dict[str, "ToolSpec"]:
    """Get cached MCP tools without rediscovering.

    Waits for a start-up discovery still in progress; with ``wait`` False it
    returns no tools until that discovery has finished.
    """
    if wait:
        _mcp_discovery_done.wait()
    elif not _mcp_discovery_done.is_set():
        return {}
    with _mcp_tools_cache_lock:
        return _mcp_tools_cache.copy()


def refresh_mcp_tools(verbose: bool = True) -> Tuple[Dict[str, "ToolSpec"], Dict[str, str]]:
    """
    Refresh MCP tools cache by rediscovering all tools.

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    global _mcp_tools_cache

    with _mcp_tools_cache_lock:
        if not _mcp_config_cache:
            debug_log("No MCP config cached, skipping refresh", "mcp")
            return {}, {}

        if verbose:
            print("🔄 Refreshing MCP tools...", flush=True)

        _mcp_tools_cache, errors = discover_mcp_tools(_mcp_config_cache)

        if verbose:
            print(f"  ✅ Found {len(_mcp_tools_cache)} MCP tools", flush=True)

        debug_log(f"MCP tools cache refreshed with {len(_mcp_tools_cache)} tools", "mcp")
        return _mcp_tools_cache.copy(), errors


def is_mcp_cache_initialized() -> bool:
    """Check if MCP tools cache has been initialized."""
    with _mcp_tools_cache_lock:
        return len(_mcp_config_cache) > 0 or len(_mcp_tools_cache) > 0



# ToolSpec for MCP compatibility
@dataclass(frozen=True)
class ToolSpec:
    name: str  # canonical tool identifier (camelCase)
    description: str  # Human-readable description (matches MCP format)
    inputSchema: Optional[Dict[str, Any]] = None  # JSON Schema for arguments (matches MCP format)


def discover_mcp_tools(mcps_config: Dict[str, Any]) -> Tuple[Dict[str, ToolSpec], Dict[str, str]]:
    """Discover all tools from configured MCP servers and create ToolSpec entries for them.

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    if not mcps_config:
        return {}, {}

    try:
        client = _mcp_client_class()(mcps_config)
        discovered_tools = {}
        errors: Dict[str, str] = {}

        for server_name in mcps_config.keys():
            try:
                tools = client.list_tools(server_name)
                for tool_info in tools:
                    tool_name = tool_info.get("name")
                    if not tool_name:
                        continue

                    # Create a unique tool name: server__toolname
                    full_tool_name = f"{server_name}__{tool_name}"

                    # Create a ToolSpec for this MCP tool
                    description = tool_info.get("description", f"Tool from {server_name} MCP server")
                    input_schema = tool_info.get("inputSchema", {"type": "object", "properties": {}, "required": []})
                    discovered_tools[full_tool_name] = ToolSpec(
                        name=full_tool_name,
                        description=description,
                        inputSchema=input_schema
                    )

            except BaseException as e:
                # ExceptionGroups (from anyio TaskGroup) wrap the real cause;
                # extract the first sub-exception for a useful error message.
                cause = e
                if hasattr(e, "exceptions") and e.exceptions:
                    cause = e.exceptions[0]
                debug_log(f"Failed to discover tools from MCP server '{server_name}': {cause}", "mcp")
                errors[server_name] = str(cause)
                continue

        return discovered_tools, errors

    except Exception as e:
        debug_log(f"Failed to discover MCP tools: {e}", "mcp")
        return {}, {"_global": str(e)}


def generate_tools_json_schema(allowed_tools: Optional[List[str]] = None, mcp_tools: Optional[Dict[str, ToolSpec]] = None) -> List[Dict[str, Any]]:
    """
    Generate tools in OpenAI-compatible JSON schema format for native tool calling.

    This format is supported by Ollama for models with native tool calling support
    (Llama 3.1+, Llama 3.2, Qwen 3, Mistral, etc.).

    Returns a list of tool definitions in this format:
    [
        {
            "type": "function",
            "function": {
                "name": "toolName",
                "description": "Tool description",
                "parameters": {
                    "type": "object",
                    "properties": {...},
                    "required": [...]
                }
            }
        }
    ]
    """
    names = list(allowed_tools or list(BUILTIN_TOOLS.keys()))
    tools: List[Dict[str, Any]] = []

    # Add built-in tools
    for tool_name in names:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            continue

        tool_def = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema or {"type": "object", "properties": {}, "required": []},
            }
        }
        tools.append(tool_def)

    # Add discovered MCP tools
    if mcp_tools:
        for tool_name, spec in mcp_tools.items():
            if tool_name in names:  # Only include if allowed
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.inputSchema or {"type": "object", "properties": {}, "required": []},
                    }
                }
                tools.append(tool_def)

    return tools


def generate_tools_description(allowed_tools: Optional[List[str]] = None, mcp_tools: Optional[Dict[str, ToolSpec]] = None) -> str:
    """Produce a compact tool help string for the system prompt using OpenAI standard format."""
    names = list(allowed_tools or list(BUILTIN_TOOLS.keys()))
    lines: List[str] = []
    lines.append("Tool-use protocol: Use the tool_calls field in your response:")
    lines.append('tool_calls: [{"id": "call_<id>", "type": "function", "function": {"name": "<toolName>", "arguments": "<json_string>"}}]')
    lines.append("\nAvailable tools and when to use them:")

    # Add built-in tools
    for tool_name in names:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            continue
        lines.append(f"\n{tool.name}: {tool.description}")
        if tool.inputSchema:
            # Extract a simple parameter summary from the JSON schema
            props = tool.inputSchema.get("properties", {})
            required = tool.inputSchema.get("required", [])
            param_descriptions = []
            for prop_name, prop_def in props.items():
                prop_type = prop_def.get("type", "any")
                is_required = prop_name in required
                req_marker = " (required)" if is_required else ""
                param_descriptions.append(f"{prop_name}: {prop_type}{req_marker}")
            if param_descriptions:
                lines.append(f"Input: {', '.join(param_descriptions)}")

    # Add discovered MCP tools
    if mcp_tools:
        for tool_name, spec in mcp_tools.items():
            if tool_name in names:  # Only include if allowed
                lines.append(f"\n{spec.name}: {spec.description}")
                if spec.inputSchema:
                    # Extract a simple parameter summary from the JSON schema
                    props = spec.inputSchema.get("properties", {})
                    required = spec.inputSchema.get("required", [])
                    param_descriptions = []
                    for prop_name, prop_def in props.items():
                        prop_type = prop_def.get("type", "any")
                        is_required = prop_name in required
                        req_marker = " (required)" if is_required else ""
                        param_descriptions.append(f"{prop_name}: {prop_type}{req_marker}")
                    if param_descriptions:
                        lines.append(f"Input: {', '.join(param_descriptions)}")

    return "\n".join(lines)

def _normalize_time_range(args: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    since: Optional[str] = None
    until: Optional[str] = None
    if args and isinstance(args, dict):
        try:
            since_val = args.get("since_utc")
            since = str(since_val) if since_val else None
        except Exception:
            since = None
        try:
            until_val = args.get("until_utc")
            until = str(until_val) if until_val else None
        except Exception:
            until = None
    if since is None and until is None:
        # Default last 24h
        return (now - timedelta(days=1)).isoformat(), now.isoformat()
    if since is None and until is not None:
        # backfill 24h prior to until
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except Exception:
            until_dt = now
        return (until_dt - timedelta(days=1)).isoformat(), until_dt.isoformat()
    if since is not None and until is None:
        return since, now.isoformat()
    return since or (now - timedelta(days=1)).isoformat(), until or now.isoformat()


def run_tool_with_retries(
    db,
    cfg: Settings,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    system_prompt: str,
    original_prompt: str,
    redacted_text: str,
    max_retries: int = 1,
    language: Optional[str] = None,
    quiet: bool = False,
    request_ref: Optional[str] = None,
    silent: bool = False,
    allowed_tools: Optional[Any] = None,
) -> ToolExecutionResult:
    """Run one tool call through central safety, confirmation and execution.

    ``silent`` keeps the tool's own progress lines off the console (a routine prints one line per step).
    ``allowed_tools`` is a background bridge request's tool snapshot; a routine run by this call may use
    only those tools. Both, with the language and ``request_ref``, are visible to the tool for the
    duration of the call (``request_scope.current_call``)."""
    call = CallContext(language=language, quiet=quiet, request_ref=request_ref,
                       allowed_tools=None if allowed_tools is None else frozenset(allowed_tools))
    with tool_call(call):
        return _run_tool(db, cfg, tool_name, tool_args, system_prompt, original_prompt, redacted_text,
                         max_retries, language, quiet, request_ref, silent, call)


def _record(name: str, tool_args, schema) -> None:
    """Note a successful call in the recent-actions journal; recording never changes the result."""
    try:
        get_journal().record(name, tool_args if isinstance(tool_args, dict) else {}, schema)
    except Exception as exc:  # noqa: BLE001
        debug_log(f"journal not recorded ({type(exc).__name__})", "routines")


def _run_tool(db, cfg, tool_name, tool_args, system_prompt, original_prompt, redacted_text, max_retries,
              language, quiet, request_ref, silent, call: CallContext) -> ToolExecutionResult:
    from dataclasses import replace
    from .confirmation import consume_run_grant
    # Normalize tool name to canonical camelCase
    raw_name = (tool_name or "").strip()
    name = raw_name
    approval = None

    # Central safety policy evaluation
    tool_obj = BUILTIN_TOOLS.get(name)
    from .confirmation import (
        evaluate_safety,
        SafetyTier,
        get_confirmation_store,
        get_dialog_callback,
        ORIGIN_CHAT,
        ORIGIN_VOICE,
    )

    safety_req = evaluate_safety(
        tool_name=name,
        tool_args=tool_args,
        cfg=cfg,
        tool=tool_obj,
        language=language,
    )

    if safety_req.tier == SafetyTier.DENY:
        debug_log(f"Tool '{name}' denied by safety policy", "safety")
        return ToolExecutionResult(
            success=False,
            reply_text=None,
            error_message=f"Action '{safety_req.action}' is prohibited: {safety_req.reason or 'This operation cannot be executed.'}",
        )

    store = get_confirmation_store()

    if safety_req.tier in (SafetyTier.CONFIRM_VOICE, SafetyTier.CONFIRM_DIALOG):
        # An approved routine authorises each confirmed step once, for exactly the action it named.
        authorised = consume_run_grant(
            safety_req.tool_name, safety_req.action, safety_req.target, safety_req.parameters,
            required_tier=safety_req.tier,
        ) or store.consume_authorisation(
            safety_req.tool_name, safety_req.action, safety_req.target, safety_req.parameters,
            required_tier=safety_req.tier,
        )
        if authorised:
            approval = safety_req
            debug_log(f"Tool '{name}' confirmed ({safety_req.tier.value}); proceeding to execute.", "safety")
        else:
            exec_ctx = {
                "db": db,
                "cfg": cfg,
                "tool_name": name,
                "tool_args": tool_args,
                "system_prompt": system_prompt,
                "original_prompt": original_prompt,
                "redacted_text": redacted_text,
                "max_retries": max_retries,
                "language": language,
                "quiet": quiet,
                # Text chat runs the reply engine quietly; its outcome returns to the chat.
                "origin": ORIGIN_CHAT if quiet else ORIGIN_VOICE,
                # Lets an external orchestrator find the confirmation it caused.
                "request_ref": request_ref,
                # A bridge request's tool snapshot still bounds the call once it is approved.
                "allowed_tools": call.allowed_tools,
            }
            if safety_req.tier == SafetyTier.CONFIRM_VOICE:
                store.set_pending(safety_req, execution_context=exec_ctx)
                debug_log(f"Tool '{name}' requires voice confirmation; registered pending request.", "safety")
                what = " ".join(part for part in (safety_req.action, safety_req.target) if part)
                reply = f"I need your confirmation to {what}. Say yes or no."
                return ToolExecutionResult(success=False, reply_text=reply, error_message=None)

            dialog_cb = get_dialog_callback()
            if dialog_cb is None:
                debug_log(f"Tool '{name}' requires dialog confirmation, but no dialog callback registered (headless).", "safety")
                return ToolExecutionResult(
                    success=False,
                    reply_text=None,
                    error_message=f"Action '{safety_req.action}' requires desktop confirmation dialog, but no desktop interface is active.",
                )
            # Non-blocking: the dialog answer arrives later and schedules the
            # action on a worker, so this call never waits on the user.
            if not store.begin_dialog(safety_req, exec_ctx, dialog_cb):
                return ToolExecutionResult(
                    success=False,
                    reply_text=None,
                    error_message=f"Action '{safety_req.action}' could not open the desktop confirmation dialog.",
                )
            debug_log(f"Tool '{name}' awaiting desktop dialog confirmation.", "safety")
            return ToolExecutionResult(
                success=False,
                reply_text="Please confirm on your desktop to " + " ".join(
                    part for part in (safety_req.action, safety_req.target) if part) + ".",
                error_message=None,
            )

    with tool_call(replace(call, approval=approval)):
        return _execute(db, cfg, name, raw_name, tool_name, tool_args, system_prompt, original_prompt,
                        redacted_text, max_retries, language, quiet or silent)


def _execute(db, cfg, name, raw_name, tool_name, tool_args, system_prompt, original_prompt, redacted_text,
             max_retries, language, hide_output) -> ToolExecutionResult:
    # Check if tool name is a discovered MCP tool (server__toolname format)
    if "__" in raw_name:
        server_name, mcp_tool_name = raw_name.split("__", 1)
        mcps_config = getattr(cfg, "mcps", {})
        if mcps_config and server_name in mcps_config:
            try:
                try:
                    client_class = _mcp_client_class()
                except ImportError:
                    return ToolExecutionResult(success=False, reply_text=None, error_message="MCP client not available. Install 'mcp' package.")

                client = client_class(mcps_config)
                # Whatever an MCP server returns is outside content (routines.spec.md, Prompt-injection boundary).
                note_outside_content()
                result = client.invoke_tool(server_name=server_name, tool_name=mcp_tool_name, arguments=tool_args or {})
                is_error = bool(result.get("isError", False))
                text = result.get("text") or None
                if not is_error:
                    spec = get_cached_mcp_tools().get(raw_name)
                    _record(raw_name, tool_args, spec.inputSchema if spec is not None else None)
                return ToolExecutionResult(success=(not is_error), reply_text=text, error_message=(text if is_error else None))
            except Exception as e:
                detail = str(e) or type(e).__name__
                return ToolExecutionResult(success=False, reply_text=None, error_message=f"MCP tool '{raw_name}' error: {detail}")

    # Friendly user print helper (non-debug only)
    def _user_print(message: str) -> None:
        # 4-space indent: tool messages happen INSIDE an agentic-loop
        # turn. The turn header (`  🔁 Turn N/M`) sits at 2 spaces, so
        # per-tool activity nests one level deeper for visual hierarchy.
        if not hide_output and not getattr(cfg, "voice_debug", False):
            try:
                print(f"    {message}")
            except Exception:
                pass

    # Check builtin tools first
    if name in BUILTIN_TOOLS:
        tool = BUILTIN_TOOLS[name]
        if tool.returns_outside_content_for(tool_args):
            note_outside_content()
        result = tool.execute(
            db=db,
            cfg=cfg,
            tool_args=tool_args,
            system_prompt=system_prompt,
            original_prompt=original_prompt,
            redacted_text=redacted_text,
            max_retries=max_retries,
            user_print=_user_print,
            language=language,
        )
        if result.success:
            _record(name, tool_args, tool.inputSchema)
        return result

    # Unknown tool
    debug_log(f"unknown tool requested: {tool_name}", "tools")
    return ToolExecutionResult(success=False, reply_text=None, error_message=f"Unknown tool: {tool_name}")



"""Validating a whole routine against the live tool registry, then running its steps in order.

Every step goes through ``run_tool_with_retries`` exactly as if it had been asked for alone, so central
safety, confirmation, redaction and desktop referents apply on every run. One routine runs at a time,
within one deadline, and a stop addressed to Jarvis ends it between steps. See ``routines.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import threading
import time
from typing import Any, Callable, Iterable, Optional

from ..debug import debug_log
from ..utils.redact import redact
from .definitions import NEVER_STEPS, Routine, Step, fingerprint, step_label

DEADLINE_SEC = 120.0
_REASON_CHARS = 200

_run_lock = threading.Lock()
_state_lock = threading.Lock()
_stop_event: Optional[threading.Event] = None


@dataclass(frozen=True)
class PlannedStep:
    position: int
    label: str
    tool: str
    args: dict
    # The step's classification by central safety (a ``ConfirmationRequest``).
    safety: Any


@dataclass(frozen=True)
class StepResult:
    position: int
    label: str
    status: str  # done | failed | skipped
    reason: str = ""
    # The step's own confirmation question when it stopped the routine; never part of the report.
    question: str = ""


@dataclass(frozen=True)
class RoutineResult:
    name: str
    steps: tuple[StepResult, ...] = ()
    # Set when nothing ran: the routine did not validate, or another routine was running.
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None and all(step.status == "done" for step in self.steps)

    @property
    def question(self) -> str:
        """The confirmation question of the step that stopped the routine, worded and naming its target
        exactly as that call alone would ask (redacted, not scrubbed); empty when no step asked."""
        return next((step.question for step in self.steps if step.question), "")

    @property
    def text(self) -> str:
        """A short readable report: labels, positions, statuses and reasons, never argument values."""
        if self.error is not None:
            return self.error
        total, done = len(self.steps), sum(step.status == "done" for step in self.steps)
        if done == total:
            return f"{display_name(self.name)}: {'the step is' if total == 1 else f'all {total} steps'} done."
        lines = [f"{display_name(self.name)}: {done} of {total} step{'' if total == 1 else 's'} done."]
        for step in self.steps:
            if step.status != "done":
                lines.append(f"Step {step.position} ({step.label}) {step.status}: {_sentence(step.reason)}")
        return "\n".join(lines)


class RoutineInvalid(ValueError):
    """The routine cannot run as it is now. ``detail`` names the step by position and label and gives the
    reason; ``code`` is the reason code the debug log may record."""

    def __init__(self, detail: str, position: int, code: str):
        super().__init__(detail)
        self.detail = detail
        self.position = position
        self.code = code


def display_name(name: str) -> str:
    return name[:1].upper() + name[1:]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


# --- privacy --------------------------------------------------------------------------------

_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+|\bwww\.\S+|\b(?:mailto|file|data|javascript):\S+", re.IGNORECASE)
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
_PATH_TOKEN = re.compile(r"[^\s'\"]*[\\/][^\s'\"]*|(?<![\w])[~%][^\s'\"]*")


def scrub(text: str) -> str:
    """A tool's error or question as a routine report may show it: redacted, with paths replaced by
    ``[path]`` and web addresses by ``[address]``, on one line and bounded."""
    text = redact(str(text or ""))
    text = _ADDRESS.sub("[address]", text)

    def quoted(match: re.Match) -> str:
        inner = match.group(0)[1:-1]
        return "[path]" if re.search(r"[\\/]|^[A-Za-z]:|^[~%]", inner) else match.group(0)

    text = _QUOTED.sub(quoted, text)
    text = _PATH_TOKEN.sub(lambda match: match.group(0) if match.group(0) == "[address]" else "[path]", text)
    text = " ".join(text.split())
    return text if len(text) <= _REASON_CHARS else text[:_REASON_CHARS - 1].rstrip() + "…"


# --- validation -----------------------------------------------------------------------------

def catalogue(cfg) -> dict[str, Optional[dict]]:
    """Tools registered now, with their input schemas: the built-in catalogue (which already leaves out
    tools of another platform or turned off in Settings) and the discovered tools of configured MCP
    servers."""
    from ..tools.registry import BUILTIN_TOOLS, get_cached_mcp_tools
    tools: dict[str, Optional[dict]] = {name: tool.inputSchema for name, tool in BUILTIN_TOOLS.items()}
    servers = getattr(cfg, "mcps", None) or {}
    if servers:
        try:
            for name, spec in get_cached_mcp_tools().items():
                if name.split("__", 1)[0] in servers:
                    tools[name] = spec.inputSchema
        except Exception as exc:  # noqa: BLE001 - an unreadable MCP cache only makes its tools unavailable
            debug_log(f"MCP tools unavailable for routine validation ({type(exc).__name__})", "routines")
    return tools


def labels(routine: Routine, cfg) -> list[str]:
    """Each step's label, using the live schemas where the tool is registered."""
    tools = catalogue(cfg)
    return [step_label(step, tools.get(step.tool)) for step in routine.steps]


def entry_labels(entries, cfg) -> list[str]:
    """The derived label of each journal entry, as the step it would become."""
    tools = catalogue(cfg)
    return [step_label(Step(entry.tool, entry.args), tools.get(entry.tool)) for entry in entries]


def approval_parameters(routine: Routine, steps: Iterable[PlannedStep]) -> dict:
    """What an approval to run ``routine`` is bound to: its name, the fingerprint of its steps and each step
    that needs confirmation as the question names it (``steps`` is the routine's plan)."""
    from ..tools.confirmation import SafetyTier
    return {"action": "run", "name": routine.name, "fingerprint": fingerprint(routine),
            "confirm": [{"tool": step.safety.tool_name, "tier": step.safety.tier.value,
                         "action": step.safety.action, "target": step.safety.target,
                         "parameters": dict(step.safety.parameters)}
                        for step in steps if step.safety.tier != SafetyTier.SAFE]}


def approved_grants(routine: Routine, approval) -> list:
    """The step confirmations an approved run may use: exactly the steps the question the user answered
    named, when that approval is for this routine as it is now; otherwise none."""
    from ..tools.confirmation import ConfirmationRequest, SafetyTier
    parameters = getattr(approval, "parameters", None)
    if (getattr(approval, "tool_name", None) != "routineControl" or not isinstance(parameters, dict)
            or parameters.get("action") != "run" or parameters.get("name") != routine.name
            or parameters.get("fingerprint") != fingerprint(routine)):
        return []
    return [ConfirmationRequest(tool_name=item["tool"], tier=SafetyTier(item["tier"]), action=item["action"],
                                target=item["target"], parameters=dict(item["parameters"]))
            for item in parameters.get("confirm", ())]


def plan(routine: Routine, cfg, *, allowed_tools: Optional[Iterable[str]] = None,
         language: Optional[str] = None) -> list[PlannedStep]:
    """Check every step against the live state and classify it; raises ``RoutineInvalid`` for the first
    step that fails, before anything has run.

    A step must be a tool registered now (and in ``allowed_tools`` when a background bridge runs the
    routine), its arguments must fit the tool's schema, and central safety must not deny it."""
    from ..tools.confirmation import SafetyTier, evaluate_safety
    from ..tools.registry import BUILTIN_TOOLS
    from ..tools.schema_validation import validate_arguments
    tools = catalogue(cfg)
    allowed = None if allowed_tools is None else frozenset(allowed_tools)
    total = len(routine.steps)
    planned = []
    for position, step in enumerate(routine.steps, 1):
        label = step_label(step, tools.get(step.tool))

        def invalid(reason: str, code: str) -> RoutineInvalid:
            debug_log(f"routine invalid at step {position} of {total}: {code}", "routines")
            return RoutineInvalid(f"step {position} of {total} ({label}): {reason}", position, code)

        if step.tool in NEVER_STEPS:
            raise invalid("routines cannot run this tool", "forbidden")
        if step.tool not in tools or (allowed is not None and step.tool not in allowed):
            raise invalid("this tool is not available here", "unavailable")
        problem = validate_arguments(tools[step.tool], dict(step.args))
        if problem is not None:
            raise invalid(f"its arguments do not fit this tool ({problem})", "invalid_arguments")
        safety = evaluate_safety(step.tool, dict(step.args), cfg, tool=BUILTIN_TOOLS.get(step.tool),
                                 language=language)
        if safety.tier == SafetyTier.DENY:
            raise invalid("this action is not allowed", "denied")
        planned.append(PlannedStep(position, label, step.tool, dict(step.args), safety))
    return planned


# --- running --------------------------------------------------------------------------------

def stop_running() -> bool:
    """Stop the routine in flight between steps (a stop addressed to Jarvis). False when none runs."""
    with _state_lock:
        event = _stop_event
    if event is None:
        return False
    event.set()
    debug_log("routine stop requested", "routines")
    return True


def is_running() -> bool:
    with _state_lock:
        return _stop_event is not None


@dataclass
class _Run:
    stop: threading.Event = field(default_factory=threading.Event)


def run_routine(routine: Routine, cfg, *, db=None, language: Optional[str] = None, quiet: bool = False,
                user_print: Callable[[str], None] = print, executor: Optional[Callable[..., Any]] = None,
                allowed_tools: Optional[Iterable[str]] = None, request_ref: Optional[str] = None,
                original_prompt: str = "", grants: Iterable[Any] = (), deadline_sec: float = DEADLINE_SEC,
                clock: Callable[[], float] = time.monotonic) -> RoutineResult:
    """Validate the whole routine, then run its steps in order, each once.

    A failed step is reported and the next one runs; steps not started by the deadline, or after a
    stop, are skipped. Nothing runs when another routine is running or a step does not validate.

    ``grants`` are the step confirmations of the question the user approved (``approved_grants``): each
    is authorised once, for exactly the tool, action, target and parameters it names, for the duration of
    this run. Nothing else is authorised: a step that still asks for confirmation stops the routine there."""
    global _stop_event
    from ..tools.confirmation import run_grants
    if executor is None:
        from ..tools.registry import run_tool_with_retries
        executor = run_tool_with_retries
    if not _run_lock.acquire(blocking=False):
        debug_log("routine refused: another routine is running", "routines")
        return RoutineResult(routine.name, error="A routine is already running. Wait for it to finish.")
    stop = threading.Event()
    try:
        with _state_lock:
            _stop_event = stop
        try:
            steps = plan(routine, cfg, allowed_tools=allowed_tools, language=language)
        except RoutineInvalid as exc:
            return RoutineResult(routine.name, error=f"{display_name(routine.name)} was not run: {exc.detail}.")
        deadline = clock() + deadline_sec
        results = []
        halted = ""
        with run_grants(grants):
            for step in steps:
                if halted or stop.is_set() or clock() >= deadline:
                    reason = halted or ("stopped" if stop.is_set() else "out of time")
                    results.append(StepResult(step.position, step.label, "skipped", reason))
                    user_print(f"⏭️ {step.label}: {reason}")
                    continue
                outcome = _run_step(step, executor, cfg=cfg, db=db, language=language, quiet=quiet,
                                    allowed_tools=allowed_tools, request_ref=request_ref,
                                    original_prompt=original_prompt)
                results.append(outcome)
                debug_log(f"routine step {step.position} of {len(steps)} ({step.tool}): {outcome.status}",
                          "routines")
                user_print(f"✅ {step.label}" if outcome.status == "done" else f"❌ {step.label}: {outcome.reason}")
                if outcome.reason.startswith(NEEDS_CONFIRMATION):
                    halted = f"step {step.position} needs confirmation"
        if stop.is_set():
            debug_log("routine stopped between steps", "routines")
        result = RoutineResult(routine.name, tuple(results))
        debug_log(f"routine finished: {sum(r.status == 'done' for r in results)} of {len(results)} steps done",
                  "routines")
        return result
    finally:
        with _state_lock:
            _stop_event = None
        _run_lock.release()


NEEDS_CONFIRMATION = "needs confirmation"


def _run_step(step: PlannedStep, executor, *, cfg, db, language, quiet, allowed_tools, request_ref,
              original_prompt) -> StepResult:
    from ..tools.confirmation import get_confirmation_store
    store = get_confirmation_store()
    pending_before = store.get_pending()
    try:
        result = executor(db=db, cfg=cfg, tool_name=step.tool, tool_args=dict(step.args), system_prompt="",
                          original_prompt=original_prompt, redacted_text=redact(original_prompt), max_retries=1,
                          language=language, quiet=quiet, silent=True, request_ref=request_ref,
                          allowed_tools=None if allowed_tools is None else frozenset(allowed_tools))
    except Exception as exc:  # noqa: BLE001 - one step never stops the others
        debug_log(f"routine step {step.position} raised {type(exc).__name__}", "routines")
        return StepResult(step.position, step.label, "failed", "The step could not be completed.")
    if result.success:
        return StepResult(step.position, step.label, "done")
    pending = store.get_pending()
    if pending is not None and pending is not pending_before and pending.request.tool_name == step.tool:
        # Its classification changed since the routine was approved: the step's own question stands, and
        # approving it runs that step alone. The report says only that it needs confirmation; the question
        # travels beside it so the user hears the real target.
        return StepResult(step.position, step.label, "failed", NEEDS_CONFIRMATION,
                          question=redact(result.reply_text or ""))
    reason = scrub(result.error_message or result.reply_text or "") or "The step failed."
    return StepResult(step.position, step.label, "failed", reason)

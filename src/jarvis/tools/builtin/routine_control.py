"""routineControl: run, list and change the user's routines.

A thin adapter over ``jarvis.routines``: the runner validates and runs a routine, the journal supplies
the steps a save is built from, and the store writes ``config.json``. Platform-neutral and always
registered, because saving the first routine needs it. See ``routines/routines.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Dict, Optional

from ...debug import debug_log
from ...utils.redact import redact
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

ACTIONS = ("run", "list", "recent", "save", "delete", "rename")
CHANGES = ("save", "delete", "rename")
_MAX_NAME_CHARS = 60
_UNTRUSTED = ("Routines can only be changed by a direct request. Please ask for this change again on its own, "
              "without reading anything first.")
_NOTHING_DONE = "I have not done anything in this conversation to save yet."


@dataclass(frozen=True)
class _Change:
    question: str  # the full read-back, which the confirmation names as its action
    parameters: Dict[str, Any]  # what an approval is bound to
    write: Callable[[], Any]
    done: str


def _window(cfg) -> float:
    """The dialogue memory window: journal entries older than this belong to an earlier conversation."""
    value = getattr(cfg, "dialogue_memory_timeout", None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 300.0


def _available(routines) -> str:
    names = ", ".join(sorted(routine.name for routine in routines.values()))
    return f"Available routines: {names}." if names else "You have no routines yet."


def _new_name(value) -> str:
    name = " ".join(value.split()) if isinstance(value, str) else ""
    if not name:
        raise ValueError("A name for the routine is required.")
    if len(name) > _MAX_NAME_CHARS:
        raise ValueError(f"A routine name can be at most {_MAX_NAME_CHARS} characters.")
    return name


def _numbers(value) -> list[int]:
    """Journal entry numbers as the model gave them: whole numbers, or digit strings."""
    if not isinstance(value, list) or not value:
        raise ValueError("steps must be a list of numbers from the recent actions.")
    numbers = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("steps must be a list of numbers from the recent actions.")
        if isinstance(item, float) and item.is_integer():
            item = int(item)
        if isinstance(item, str) and item.strip().isdigit():
            item = int(item.strip())
        if not isinstance(item, int):
            raise ValueError("steps must be a list of numbers from the recent actions.")
        numbers.append(item)
    return numbers


def _steps_text(labels: list[str]) -> str:
    return "; ".join(f"{position}. {label}" for position, label in enumerate(labels, 1))


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


class RoutineControlTool(Tool):
    # Routine names are the user's own data: the fast path logs only the argument names.
    log_arguments = False

    @property
    def name(self) -> str:
        return "routineControl"

    @property
    def description(self) -> str:
        return ("Run one of the user's routines (a saved chain of actions such as \"movie mode\"), list them, or "
                "save what was just done as a routine; also delete or rename one.")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS),
                           "description": "run, list, recent (what was done in this conversation), save, delete "
                                          "or rename."},
                "name": {"type": "string",
                         "description": "The routine name or alias; for save, the new routine's name."},
                "steps": {"type": "array", "items": {"type": "integer"},
                          "description": "save only: numbers from recent, in the order to run them. Omit to "
                                         "save everything done in this conversation."},
                "new_name": {"type": "string", "description": "rename only: the new name."},
            },
            "required": ["action"],
        }

    # --- fast path ----------------------------------------------------------------------

    def fast_targets(self, cfg):
        """Each routine's name and aliases from the live set, for deterministic routing; no I/O."""
        from ...fastpath.matcher import FastTarget
        from ...routines import store
        from ...routines.runner import display_name
        return tuple(FastTarget((routine.name, *routine.aliases), routine.name, "", display_name(routine.name))
                     for routine in store.current(cfg).values())

    # --- safety -------------------------------------------------------------------------

    def classify_safety(self, args, cfg):
        from ..confirmation import ConfirmationRequest, SafetyTier
        from ..request_scope import outside_content_seen
        args = args if isinstance(args, dict) else {}
        action = args.get("action")

        def request(tier=SafetyTier.SAFE, what=None, parameters=None, **extra):
            return ConfirmationRequest(tool_name=self.name, tier=tier, action=what or str(action or self.name),
                                       target="", parameters=parameters if parameters is not None else dict(args),
                                       **extra)

        if action == "run":
            return self._classify_run(args, cfg, request)
        if action not in CHANGES:
            return request()
        if outside_content_seen():
            return request(SafetyTier.DENY, "change routines", reason=_UNTRUSTED)
        try:
            change = self._change(action, args, cfg)
        except ValueError:
            return request()  # nothing can change; running reports why
        return request(SafetyTier.CONFIRM_VOICE, change.question, change.parameters)

    def _classify_run(self, args, cfg, request):
        """The strictest tier of the steps; a routine of routine steps is ``SAFE`` and runs at once."""
        from ..confirmation import SafetyTier, _TIER_RANK
        from ..request_scope import current_call
        from ...routines import definitions, runner, store
        routine = definitions.find(args.get("name"), store.current(cfg))
        if routine is None:
            return request()
        call = current_call()
        try:
            steps = runner.plan(routine, cfg, allowed_tools=call.allowed_tools, language=call.language)
        except runner.RoutineInvalid:
            return request()  # nothing runs; running reports which step and why
        parameters = runner.approval_parameters(routine, steps)
        confirming = [step for step in steps if step.safety.tier != SafetyTier.SAFE]
        if not confirming:
            return request(SafetyTier.SAFE, "run", parameters)
        tier = max((step.safety.tier for step in confirming), key=_TIER_RANK.__getitem__)
        what = _join([" ".join(part for part in (step.safety.action, step.safety.target) if part)
                      for step in confirming])
        consequences = " ".join(step.safety.consequence for step in confirming if step.safety.consequence)
        return request(tier, f"run the routine {routine.name}, which will {what}", parameters,
                       consequence=consequences or None)

    # --- running ------------------------------------------------------------------------

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        try:
            if action == "run":
                return self._run(args, context)
            if action == "list":
                return self._list(context.cfg)
            if action == "recent":
                return self._recent(context.cfg)
            if action in CHANGES:
                return self._apply(action, args, context)
            raise ValueError(f"Use one of: {', '.join(ACTIONS)}.")
        except ValueError as exc:
            debug_log(f"routineControl {action if action in ACTIONS else 'unknown'}: refused", "routines")
            return ToolExecutionResult(success=False, reply_text=None, error_message=redact(str(exc)))

    def _run(self, args, context) -> ToolExecutionResult:
        from ..request_scope import current_call
        from ...routines import definitions, runner, store
        routines = store.current(context.cfg)
        routine = definitions.find(args.get("name"), routines)
        if routine is None:
            raise ValueError(f"Unknown routine. {_available(routines)}")
        call = current_call()
        # Grants come from the question the user answered, never from classifying the steps again.
        result = runner.run_routine(
            routine, context.cfg, db=context.db, language=context.language, quiet=call.quiet,
            user_print=context.user_print, allowed_tools=call.allowed_tools, request_ref=call.request_ref,
            original_prompt=context.redacted_text, grants=runner.approved_grants(routine, call.approval))
        text = redact(result.text)
        if result.success:
            return ToolExecutionResult(success=True, reply_text=text)
        if result.question:
            # A step now waits for the user: its own question follows the report, so the user hears what
            # they are approving. A bridge speaks it locally and its provider gets only the awaiting notice.
            return ToolExecutionResult(success=False, reply_text=f"{text}\n{result.question}", error_message=text)
        return ToolExecutionResult(success=False, reply_text=None, error_message=text)

    def _list(self, cfg) -> ToolExecutionResult:
        from ...routines import runner, store
        routines = store.current(cfg)
        if not routines:
            return ToolExecutionResult(success=True, reply_text=_available(routines))
        listing = {"routines": [{"name": routine.name, "aliases": list(routine.aliases),
                                 "steps": runner.labels(routine, cfg)} for routine in routines.values()]}
        return ToolExecutionResult(success=True, reply_text=redact(json.dumps(listing, ensure_ascii=False)))

    def _recent(self, cfg) -> ToolExecutionResult:
        from ...routines import runner
        from ...routines.journal import get_journal
        entries = get_journal().recent(_window(cfg))
        if not entries:
            return ToolExecutionResult(success=True, reply_text=_NOTHING_DONE)
        labels = runner.entry_labels(list(reversed(entries)), cfg)
        recent = {"recent": [{"number": entry.number, "step": label}
                             for entry, label in zip(reversed(entries), labels)]}
        return ToolExecutionResult(success=True, reply_text=redact(json.dumps(recent, ensure_ascii=False)))

    # --- changes ------------------------------------------------------------------------

    def _apply(self, action, args, context) -> ToolExecutionResult:
        from ..request_scope import current_call, outside_content_seen
        from ...routines.store import RoutineStoreError
        if outside_content_seen():
            raise ValueError(_UNTRUSTED)
        change = self._change(action, args, context.cfg)
        approval = current_call().approval
        if approval is None or approval.tool_name != self.name or approval.parameters != change.parameters:
            # Reached only when the central gate was bypassed; never write without the user's yes.
            debug_log(f"routineControl {action}: not approved, nothing written", "routines")
            raise ValueError("That change needs your confirmation first.")
        try:
            change.write()
        except RoutineStoreError as exc:
            raise ValueError(str(exc)) from None
        return ToolExecutionResult(success=True, reply_text=redact(change.done))

    def _change(self, action, args, cfg) -> _Change:
        """The exact change ``args`` asks for, its read-back and its binding; ``ValueError`` when it cannot
        be made. Building it changes nothing."""
        from ...routines import definitions, store
        routines = store.current(cfg)
        if action == "save":
            return self._save(args, cfg, routines)
        routine = definitions.find(args.get("name"), routines)
        if routine is None:
            raise ValueError(f"Unknown routine. {_available(routines)}")
        fingerprint = definitions.fingerprint(routine)
        if action == "delete":
            return _Change(f"delete the routine {routine.name}",
                           {"action": "delete", "name": routine.name, "fingerprint": fingerprint},
                           lambda: store.delete(routine.name), f"Deleted the routine {routine.name}.")
        new_name = _new_name(args.get("new_name"))
        other = definitions.find(new_name, routines)
        if other is not None and other.name != routine.name:
            raise ValueError(f"The name {new_name} is already used by the routine {other.name}.")
        return _Change(f"rename the routine {routine.name} to {new_name}",
                       {"action": "rename", "name": routine.name, "new_name": new_name, "fingerprint": fingerprint},
                       lambda: store.rename(routine.name, new_name), f"Renamed the routine to {new_name}.")

    def _save(self, args, cfg, routines) -> _Change:
        from ..request_scope import current_call
        from ...routines import definitions, runner, store
        from ...routines.journal import get_journal
        from ...utils import names
        name = _new_name(args.get("name"))
        entries = get_journal().recent(_window(cfg))
        if not entries:
            raise ValueError(_NOTHING_DONE)
        if args.get("steps") is None:
            chosen = list(reversed(entries))
        else:
            numbers = _numbers(args.get("steps"))
            if len(set(numbers)) != len(numbers):
                raise ValueError("Each recent action can be saved only once in a routine.")
            by_number = {entry.number: entry for entry in entries}
            unknown = [number for number in numbers if number not in by_number]
            if unknown:
                raise ValueError(f"There is no recent action numbered {unknown[0]} in this conversation.")
            chosen = [by_number[number] for number in numbers]
        if len(chosen) > definitions.MAX_STEPS:
            raise ValueError(f"A routine can have at most {definitions.MAX_STEPS} steps.")
        existing = definitions.find(name, routines)
        if existing is not None and names.key(existing.name) != names.key(name):
            raise ValueError(f"{name} is already an alias of the routine {existing.name}. Choose another name.")
        draft = definitions.Routine(name, (), tuple(definitions.Step(entry.tool, dict(entry.args))
                                                     for entry in chosen))
        call = current_call()
        try:
            runner.plan(draft, cfg, allowed_tools=call.allowed_tools, language=call.language)
        except runner.RoutineInvalid as exc:
            raise ValueError(f"{runner.display_name(name)} was not saved: {exc.detail}.") from None
        labels = runner.labels(draft, cfg)
        count = f"{len(labels)} step{'' if len(labels) == 1 else 's'}"
        question = f"save the routine {name} with {count}: {_steps_text(labels)}"
        if existing is not None:
            question += f" (it replaces your existing {existing.name})"
        steps = [{"tool": step.tool, "args": dict(step.args)} for step in draft.steps]
        parameters = {"action": "save", "name": name, "steps": [[step["tool"], step["args"]] for step in steps],
                      "replaces": existing.name if existing is not None else None}
        replaces = existing.name if existing is not None else None
        return _Change(question, parameters, lambda: store.save(name, steps, replaces=replaces),
                       f"Saved the routine {name} with {count}.")

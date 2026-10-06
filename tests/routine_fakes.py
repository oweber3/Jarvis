"""Inert fake tools for the routine tests: they record what they were asked to do and touch nothing."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from jarvis.tools.base import Tool
from jarvis.tools.confirmation import ConfirmationRequest, SafetyTier
from jarvis.tools.types import ToolExecutionResult


class FakeTool(Tool):
    """A tool with a real schema whose ``run`` only records the call.

    ``tier`` (or ``tiers`` keyed by the ``action`` argument) is what central safety sees. ``behaviour``
    can replace the default success with a failure, a wait or anything else a test needs."""

    def __init__(self, name: str, *, properties: Optional[Dict[str, Any]] = None, required=(),
                 tier: SafetyTier = SafetyTier.SAFE, tiers: Optional[Dict[str, SafetyTier]] = None,
                 behaviour: Optional[Callable[[dict], ToolExecutionResult]] = None,
                 outside_content: bool = False, log: Optional[List] = None):
        self._name = name
        self._properties = properties if properties is not None else {
            "action": {"type": "string"}, "target": {"type": "string"}}
        self._required = list(required)
        self.tier = tier
        self.tiers = tiers or {}
        self.behaviour = behaviour
        self.returns_outside_content = outside_content
        self.calls: List[dict] = []
        self.log = log if log is not None else []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Fake {self._name} for tests."

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": self._properties, "required": self._required}

    def classify_safety(self, args, cfg):
        args = dict(args or {})
        tier = self.tiers.get(args.get("action"), self.tier)
        action = str(args.get("action") or self._name)
        return ConfirmationRequest(tool_name=self._name, tier=tier, action=action,
                                   target=str(args.get("target") or ""), parameters=args)

    def run(self, args, context) -> ToolExecutionResult:
        args = dict(args or {})
        self.calls.append(args)
        self.log.append((self._name, args))
        context.user_print(f"{self._name} is working")
        if self.behaviour is not None:
            return self.behaviour(args)
        return ToolExecutionResult(success=True, reply_text=f"{self._name} done")


def failing(message: str) -> Callable[[dict], ToolExecutionResult]:
    return lambda _args: ToolExecutionResult(success=False, reply_text=None, error_message=message)


class Gate:
    """A step behaviour that waits until the test lets it finish."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, _args) -> ToolExecutionResult:
        self.entered.set()
        self.release.wait(10)
        return ToolExecutionResult(success=True, reply_text="gate done")

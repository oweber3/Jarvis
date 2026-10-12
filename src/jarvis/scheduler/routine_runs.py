"""Running a routine at its scheduled time.

A scheduled run is a manual run with nobody to answer a question: the whole routine is validated and
classified by central safety first, and it runs only when every step is ``SAFE``. Anything that would need
confirmation is never approved on the user's behalf; the routine is not run and Jarvis says why. Each
step then goes through ``run_tool_with_retries`` exactly as a manual run's would. See
``scheduler.spec.md``, Scheduled routines.
"""
from __future__ import annotations

import contextlib
from typing import Any, Callable, ContextManager, Optional

from ..debug import debug_log
from . import phrases


def _language(cfg) -> str:
    locales = getattr(cfg, "fast_commands_locales", None)
    return phrases.language_of(locales[0] if isinstance(locales, (list, tuple)) and locales else None)


def run_scheduled_routine(routine: Any, cfg: Any, *, announce: Callable[[str], None], db=None,
                          exclusive: Callable[[], ContextManager] = contextlib.nullcontext,
                          compose: Optional[Callable[..., Optional[str]]] = None) -> None:
    from ..routines import runner
    from ..tools.confirmation import SafetyTier, get_confirmation_store
    language = _language(cfg)
    name = runner.display_name(routine.name)
    with exclusive():
        try:
            steps = runner.plan(routine, cfg, language=language)
        except runner.RoutineInvalid as exc:
            debug_log(f"scheduled routine not run: {exc.code}", "scheduler")
            announce(phrases.text("routine_not_run", language, routine=routine.name,
                                  reason=runner.display_name(exc.detail) + "."))
            return
        if any(step.safety.tier != SafetyTier.SAFE for step in steps):
            debug_log("scheduled routine not run: a step needs confirmation", "scheduler")
            announce(phrases.text("routine_needs_confirmation", language, routine=routine.name))
            return
        print(f"🔁 Running the scheduled routine {name}", flush=True)
        confirmations = get_confirmation_store()
        pending_before = confirmations.get_pending()
        result = runner.run_routine(routine, cfg, db=db, language=language, user_print=print, grants=())
        if confirmations.get_pending() is not pending_before:
            # A step started to ask after all: nobody asked for this run, so its question never stands.
            confirmations.clear_pending()
            debug_log("scheduled routine: a step asked for confirmation; the question was dropped", "scheduler")
    if result.error is not None:
        announce(result.error)
        return
    if routine.report == "results":
        outputs = result.outputs
        text = None
        if outputs:
            compose = compose or _default_compose()
            try:
                text = compose(cfg, routine.name, outputs, language)
            except Exception as exc:  # noqa: BLE001 - a failed summary still says something
                debug_log(f"briefing not composed ({type(exc).__name__})", "scheduler")
        if not text:
            debug_log("briefing unavailable; saying so", "scheduler")
            text = phrases.text("briefing_unavailable", language, routine=name)
        announce(text)
        return
    if not result.success:
        announce(result.text)


def _default_compose():
    from .briefing import compose_briefing
    return compose_briefing

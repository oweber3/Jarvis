"""Power plans through ``powercfg``.

``powercfg`` is a fixed executable called with fixed arguments (plan identifiers are validated
GUIDs taken from its own listing). Parsing keys on the GUID and the parenthesised name, so it
does not depend on the Windows display language.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess

from ...debug import debug_log
from ._bounded import run_bounded

CALL_TIMEOUT_SEC = 8.0

# Well-known scheme GUIDs, addressed by a language-neutral key.
WELL_KNOWN = {
    'balanced': '381b4222-f694-41f0-9685-ff5bb260df2e',
    'power_saver': 'a1841308-3541-4fab-bc81-f71556f20b4a',
    'high_performance': '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c',
    'ultimate_performance': 'e9a42b02-d5df-448d-aa00-03f14749eb61',
}

_GUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_LINE = re.compile(rf'({_GUID})\s+\((.*)\)\s*(\*)?\s*$')


class PowerError(RuntimeError):
    """``powercfg`` failed or did not answer in time."""


@dataclass(frozen=True)
class PowerPlan:
    guid: str
    name: str
    active: bool = False


def _powercfg(*args: str) -> str:
    def call() -> str:
        done = subprocess.run(['powercfg', *args], capture_output=True, text=True, timeout=CALL_TIMEOUT_SEC,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), shell=False)
        if done.returncode != 0:
            raise PowerError('powercfg could not complete that request.')
        return done.stdout

    try:
        return run_bounded(call, CALL_TIMEOUT_SEC + 2)
    except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
        debug_log(f'powercfg call failed ({type(exc).__name__}).', 'windows')
        raise PowerError('Power plans could not be read or changed.') from exc


def list_plans() -> list[PowerPlan]:
    plans = []
    for line in _powercfg('/list').splitlines():
        found = _LINE.search(line.strip())
        if found:
            plans.append(PowerPlan(found.group(1).casefold(), found.group(2).strip(), bool(found.group(3))))
    return plans


def _tokens(value: str) -> set[str]:
    return set(re.findall(r'\w+', value.casefold(), re.UNICODE))


def resolve_plan(query: str, plans: list[PowerPlan]) -> PowerPlan:
    """Resolve a well-known key, GUID or name. A plan that is absent is never replaced by another."""
    wanted = str(query or '').strip().casefold()
    if not wanted:
        raise ValueError('A power plan name is required.')
    key = wanted.replace(' ', '_')
    guid = WELL_KNOWN.get(key) or (wanted if re.fullmatch(_GUID, wanted) else None)
    if guid is None:
        exact = [plan for plan in plans if plan.name.casefold() == wanted]
        loose = [plan for plan in plans if _tokens(wanted) and _tokens(wanted) <= _tokens(plan.name)]
        matches = exact or loose
    else:
        matches = [plan for plan in plans if plan.guid == guid.casefold()]
    if len(matches) == 1:
        return matches[0]
    available = ', '.join(plan.name for plan in plans) or 'none'
    if not matches:
        raise ValueError(f'That power plan is not installed. Available plans: {available}')
    raise ValueError(f'Several power plans match. Available plans: {available}')


def active_plan() -> PowerPlan:
    for plan in list_plans():
        if plan.active:
            return plan
    raise PowerError('No active power plan was reported.')


def set_plan(query: str) -> PowerPlan:
    """Activate a plan and report what Windows says is active afterwards."""
    plan = resolve_plan(query, list_plans())
    _powercfg('/setactive', plan.guid)
    current = active_plan()
    if current.guid != plan.guid:
        raise PowerError('Windows did not switch to that power plan.')
    debug_log('Power plan changed.', 'windows')
    return current

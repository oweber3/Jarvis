"""Multi-turn desktop follow-ups in both reply modes, on a simulated desktop.

"open Word", then "move it to the second monitor", "make it full screen", "now put Apple Music
next to it"; and "open Notepad", "move it to monitor 2", "maximise it", "close it". Each turn runs
through ``run_reply_engine`` and is judged by where the windows ended up. See
``evals/desktop_followups.py`` for the scenarios and the runner (also usable from the command line).

Codex cases need Codex installed and signed in with ChatGPT; local cases need the configured
local models. Unavailable modes are skipped, never faked.
"""
import pytest

from desktop_followups import SCENARIOS, FollowUpRunner

_RUNNERS = {}


def _runner(mode):
    if mode not in _RUNNERS:
        runner = FollowUpRunner(mode)
        try:
            runner.__enter__()
        except Exception as exc:  # noqa: BLE001 - an unavailable mode is a skip, not a failure
            _RUNNERS[mode] = exc
        else:
            _RUNNERS[mode] = runner
    found = _RUNNERS[mode]
    if isinstance(found, Exception):
        pytest.skip(f"{mode} mode unavailable: {found}")
    return found


@pytest.fixture(scope="module", autouse=True)
def _close_runners():
    yield
    for runner in _RUNNERS.values():
        if isinstance(runner, FollowUpRunner):
            runner.__exit__(None, None, None)
    _RUNNERS.clear()


@pytest.mark.eval
@pytest.mark.parametrize("mode", ["local", "codex"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_desktop_follow_up_scenario(mode, scenario):
    runs, failures, _desktops = _runner(mode).run(scenario)
    for run, failure in zip(runs, failures):
        assert failure is None, f"{mode}: {run.text!r} -> {failure} (calls={run.calls}, reply={run.reply!r})"
    assert len(runs) == len(scenario.turns)

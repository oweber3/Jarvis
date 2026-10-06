"""Live background Claude evals: synthetic requests through hidden headless Claude Code sessions, inert tools.

Opt in with JARVIS_CLAUDE_LIVE=1 (Claude Code must be installed and signed in with a Claude
subscription). Point JARVIS_CONFIG_PATH at a configuration that names the model to measure. No
window is needed, and a running Jarvis does not interfere. Nothing here uses a local judge model;
assertions are deterministic.
"""
import os

import pytest

from codex_runner import CASES, ClaudeRunner

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(os.environ.get("JARVIS_CLAUDE_LIVE") != "1",
                       reason="live background Claude evals need JARVIS_CLAUDE_LIVE=1"),
]


@pytest.fixture(scope="module")
def runner():
    """JARVIS_CLAUDE_VARIANT (baseline or proposed) picks the session instructions."""
    with ClaudeRunner(os.environ.get("JARVIS_CLAUDE_VARIANT", "proposed")) as r:
        yield r


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_claude_case(runner, case):
    run = runner.run(case)
    assert case.check(run) is None, f"{case.name}: {case.check(run)} | calls={run.names()} | reply={run.text!r}"

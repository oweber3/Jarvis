"""Live background Codex evals: synthetic requests through hidden Codex sessions, inert tools.

Opt in with JARVIS_CODEX_LIVE=1 (Codex must be installed and signed in with ChatGPT). No Codex
window, chat or binding is needed, and a running Jarvis does not interfere. Nothing here uses a
local judge model; assertions are deterministic.
"""
import os

import pytest

from codex_runner import CASES, CodexRunner

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(os.environ.get("JARVIS_CODEX_LIVE") != "1",
                       reason="live background Codex evals need JARVIS_CODEX_LIVE=1"),
]


@pytest.fixture(scope="module")
def runner():
    """JARVIS_CODEX_VARIANT (baseline or proposed) picks the session instructions."""
    with CodexRunner(os.environ.get("JARVIS_CODEX_VARIANT", "proposed")) as r:
        yield r


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_codex_case(runner, case):
    run = runner.run(case)
    assert case.check(run) is None, f"{case.name}: {case.check(run)} | calls={run.names()} | reply={run.text!r}"

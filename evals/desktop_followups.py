"""Multi-turn desktop follow-ups ("open Word", then "move it to the second monitor", ...) and
requests that do not name their target ("close this", "turn it off", "pause it").

Every turn runs through ``run_reply_engine`` exactly as voice or text chat would, sharing one
``DialogueMemory``, in ``local`` or ``codex`` reply mode. The desktop is simulated
(``desktop_sim.SimulatedDesktop``), including the window the user is looking at: ``appControl`` and
``windowControl`` execute for real against the simulation, every other tool is inert and only
recorded, so no case can change the volume, press a TV key, open a file or touch a real window. The
inert ``tvControl`` and ``mediaControl`` leave the desktop record the real tools leave (a simulated
TV and an Apple Music session), when the running build keeps one. Checks assert desktop outcomes
(which window ended up where) and which tool a request reached, never the wording of a reply.

The configuration is always a copy: ``JARVIS_CONFIG_PATH`` when set, otherwise a temporary copy of
the default configuration file. The database is a temporary file; MCP servers and location lookups
are disabled for the run.

Usage (from the repo root, with the project interpreter):
    set PYTHONPATH=src;evals
    python evals/desktop_followups.py --mode local
    python evals/desktop_followups.py --mode codex --repeat 3 --out .tmp/followups_codex.json
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "evals"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from desktop_sim import PRIMARY, SECOND, SimulatedDesktop  # noqa: E402

WINDOW_TOOLS = ("appControl", "windowControl")
# Tools that would act on this computer instead of the TV or the media player.
COMPUTER_TOOLS = ("systemVolume", "appControl", "windowControl", "inputControl", "systemSettings")


@dataclass
class TurnRun:
    text: str
    reply: str = ""
    seconds: float = 0.0
    calls: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)


Check = Callable[[SimulatedDesktop, Dict[str, Any], TurnRun], Optional[str]]


@dataclass
class Turn:
    text: str
    check: Check


@dataclass
class Scenario:
    name: str
    turns: List[Turn]
    setup: Callable[[SimulatedDesktop, Dict[str, Any]], None] = lambda desktop, state: None


# --- checks --------------------------------------------------------------------------------------
# ``state`` carries facts between turns of one scenario (for example the handle opened in turn one).

def _new_window(process: str, key: str) -> Check:
    def check(desktop, state, run):
        found = [w for w in desktop.open_windows(process) if w.hwnd not in state.get("before", set())]
        if len(found) != 1:
            return f"expected one new {process} window, found {len(found)}"
        state[key] = found[0].hwnd
        return None
    return check


def _window(desktop, hwnd):
    return next((w for w in desktop.windows if w.hwnd == hwnd), None)


def _untouched_others(desktop, state, keep: Tuple[str, ...]) -> Optional[str]:
    for hwnd, (monitor, status) in state.get("baseline", {}).items():
        if hwnd in {state.get(k) for k in keep}:
            continue
        window = _window(desktop, hwnd)
        if window is None or not window.open or window.monitor != monitor or window.state != status:
            return f"a window the user did not mean ({hwnd}) was changed"
    return None


def _no_relaunch(desktop, state) -> Optional[str]:
    if len(desktop.launches) != state.get("launches", 0):
        return f"launched again: {desktop.launches}"
    return None


def _on_second(key: str) -> Check:
    def check(desktop, state, run):
        window = _window(desktop, state.get(key))
        if window is None or not window.open:
            return f"the {key} window is gone"
        if window.monitor != SECOND:
            return f"the {key} window is on {window.monitor}, not the second monitor"
        return _no_relaunch(desktop, state) or _untouched_others(desktop, state, (key,))
    return check


def _maximised(key: str) -> Check:
    def check(desktop, state, run):
        window = _window(desktop, state.get(key))
        if window is None or not window.open:
            return f"the {key} window is gone"
        if window.state != "maximised":
            return f"the {key} window is {window.state}, not maximised"
        if window.monitor != SECOND:
            return f"maximising moved the {key} window off the second monitor"
        return _no_relaunch(desktop, state) or _untouched_others(desktop, state, (key,))
    return check


def _closed(key: str) -> Check:
    def check(desktop, state, run):
        window = _window(desktop, state.get(key))
        if window is None or window.open:
            return f"the {key} window is still open"
        return _no_relaunch(desktop, state) or _untouched_others(desktop, state, (key,))
    return check


def _beside(other: str, key: str) -> Check:
    def check(desktop, state, run):
        anchor = _window(desktop, state.get(key))
        partners = desktop.open_windows(other)
        if anchor is None or not anchor.open or anchor.monitor != SECOND:
            return f"the {key} window should still be on the second monitor"
        if len(partners) != 1:
            return f"expected one {other} window, found {len(partners)}"
        if partners[0].monitor != anchor.monitor:
            return f"{other} is on {partners[0].monitor}, not next to {key} on {anchor.monitor}"
        return None
    return check


def _tool_call(tool: str, forbidden: Tuple[str, ...] = (), **expected: Any) -> Check:
    """The turn called ``tool`` with arguments including ``expected`` (case-insensitive values), and
    nothing in ``forbidden``."""
    def matches(args: Dict[str, Any]) -> bool:
        return all(str(args.get(k, "")).replace(" ", "").casefold() == str(v).casefold()
                   for k, v in expected.items())

    def check(desktop, state, run):
        wrong = [name for name, _ in run.calls if name in forbidden]
        if wrong:
            return f"acted on the wrong thing: {wrong}"
        if not any(name == tool and matches(args) for name, args in run.calls):
            return f"expected {tool} {expected}, got {[c[0] for c in run.calls]}"
        return None
    return check


def _looking_at(process: str, key: str) -> Callable[[SimulatedDesktop, Dict[str, Any]], None]:
    """Before the scenario: the user is looking at the (only) ``process`` window."""
    def setup(desktop, state):
        window = desktop.only(process)
        desktop.foreground = window.hwnd
        state[key] = window.hwnd
    return setup


def _snapshot_baseline(desktop, state) -> None:
    state["baseline"] = {w.hwnd: (w.monitor, w.state) for w in desktop.open_windows()}
    state["launches"] = len(desktop.launches)


def _remember_existing(desktop, state) -> None:
    state["before"] = {w.hwnd for w in desktop.open_windows()}


SCENARIOS: List[Scenario] = [
    Scenario("word_move_fullscreen_beside", [
        Turn("open Word", _new_window("WINWORD", "word")),
        Turn("move it to the second monitor", _on_second("word")),
        Turn("make it full screen", _maximised("word")),
        Turn("now put Apple Music next to it", _beside("AppleMusic", "word")),
    ]),
    Scenario("notepad_move_maximise_close", [
        Turn("open Notepad", _new_window("notepad", "notepad")),
        Turn("move it to monitor 2", _on_second("notepad")),
        Turn("maximise it", _maximised("notepad")),
        Turn("close it", _closed("notepad")),
    ]),
    # Another Word window is already open: "it" is the one Jarvis just opened, not the older one.
    Scenario("word_with_existing_window", [
        Turn("open Word", _new_window("WINWORD", "word")),
        Turn("move it to the second monitor", _on_second("word")),
    ], setup=lambda desktop, state: desktop.spawn("WINWORD", PRIMARY)),
    # Nothing done yet: "this" is the window the user is looking at.
    Scenario("foreground_close_this", [
        Turn("close this", _closed("chrome")),
    ], setup=_looking_at("chrome", "chrome")),
    Scenario("foreground_move_this", [
        Turn("move this to my other monitor", _on_second("code")),
    ], setup=_looking_at("Code", "code")),
    # The window Jarvis just opened is also the one in front.
    Scenario("opened_then_close_this", [
        Turn("open Notepad", _new_window("notepad", "notepad")),
        Turn("close this", _closed("notepad")),
    ]),
    # Devices and the media player: the follow-up names neither.
    Scenario("tv_turn_it_off", [
        Turn("turn on the TV", _tool_call("tvControl", key="PowerOn")),
        Turn("turn it off", _tool_call("tvControl", COMPUTER_TOOLS, key="PowerOff")),
    ]),
    Scenario("tv_make_it_louder", [
        Turn("turn on the TV", _tool_call("tvControl", key="PowerOn")),
        Turn("make it louder", _tool_call("tvControl", COMPUTER_TOOLS, key="VolumeUp")),
    ]),
    Scenario("music_pause_it", [
        Turn("resume the music", _tool_call("mediaControl", action="play")),
        Turn("pause it", _tool_call("mediaControl", ("tvControl", "windowControl", "appControl"),
                                    action="pause")),
    ]),
]


# --- tools ---------------------------------------------------------------------------------------

_MEDIA_AFTER = {"play": "playing", "pause": "paused"}


def _remember_inert(tool_name: str, args: Dict[str, Any]) -> None:
    """Leave the desktop record the real tool leaves, when the running build keeps one."""
    from jarvis.memory.desktop_referents import get_desktop_referents

    store = get_desktop_referents()
    if tool_name == "tvControl" and hasattr(store, "record_device"):
        action = str(args.get("action") or "")
        key = str(args.get("key") or "")
        store.record_device("tv", "tvControl", f"key {key}" if action == "key" and key else action)
    elif tool_name == "mediaControl" and hasattr(store, "record_media"):
        store.record_media("Apple Music", _MEDIA_AFTER.get(str(args.get("action") or ""), "playing"))


class Executor:
    """Window tools run for real against the simulated desktop; every other tool is inert."""

    def __init__(self):
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, db, cfg, tool_name, tool_args, system_prompt="", original_prompt="",
                 redacted_text="", max_retries=1, language=None, quiet=False, request_ref=None):
        from jarvis.tools.registry import run_tool_with_retries
        from jarvis.tools.types import ToolExecutionResult

        self.calls.append((tool_name, dict(tool_args or {})))
        if tool_name in WINDOW_TOOLS:
            return run_tool_with_retries(db, cfg, tool_name, tool_args, system_prompt, original_prompt,
                                         redacted_text, max_retries=max_retries, language=language,
                                         quiet=quiet, request_ref=request_ref)
        _remember_inert(tool_name, dict(tool_args or {}))
        return ToolExecutionResult(True, "OK")


# --- configuration -------------------------------------------------------------------------------

def _config_copy(workdir: Path) -> Path:
    """The configuration file the run reads: never the user's real file."""
    configured = os.environ.get("JARVIS_CONFIG_PATH")
    from jarvis.config import default_config_path

    real = default_config_path().resolve()
    if configured and Path(configured).expanduser().resolve() != real:
        return Path(configured).expanduser()
    copy = workdir / "config.json"
    data = json.loads(real.read_text(encoding="utf-8")) if real.exists() else {}
    copy.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.environ["JARVIS_CONFIG_PATH"] = str(copy)
    return copy


def load_eval_settings(mode: str, workdir: Path, overrides: Optional[Dict[str, Any]] = None):
    from jarvis.config import load_settings

    _config_copy(workdir)
    cfg = load_settings()
    return dataclasses.replace(cfg, reply_mode=mode, mcps={}, location_enabled=False,
                               db_path=str(workdir / "eval.db"), **SimulatedDesktop.overrides(),
                               **(overrides or {}))


# --- runner --------------------------------------------------------------------------------------

class FollowUpRunner:
    def __init__(self, mode: str, overrides: Optional[Dict[str, Any]] = None, origin: str = "chat",
                 referents: bool = True):
        if mode not in ("local", "codex"):
            raise ValueError("mode must be local or codex")
        self.mode, self.overrides, self.quiet = mode, overrides or {}, origin == "chat"
        # False hides Jarvis's desktop referents from every model context (ablation baseline).
        self.referents = referents
        self.executor = Executor()

    def __enter__(self):
        from jarvis.bridge import modes
        from jarvis.tools.builtin.tv_control import TvControlTool
        from jarvis.tools.registry import BUILTIN_TOOLS

        # A simulated TV: offered like a configured one, but every call is inert (see Executor).
        self._had_tv = BUILTIN_TOOLS.get("tvControl")
        BUILTIN_TOOLS["tvControl"] = TvControlTool()
        self._workdir = Path(tempfile.mkdtemp(prefix="jarvis-followups-"))
        self.cfg = load_eval_settings(self.mode, self._workdir, self.overrides)
        self.service = None
        if self.mode == "codex":
            from jarvis.codex_bridge import lifecycle
            from jarvis.codex_bridge.service import BridgeService, failure_text
            runtime_dir = self._workdir / "codex_runtime"
            runtime_dir.mkdir()
            self.service = BridgeService(self.cfg, lifecycle.build_client(self.cfg, runtime_dir),
                                         executor=self.executor, runtime_dir=runtime_dir)
            failure = self.service.prepare()
            if failure is not None:
                self.service.close()
                raise RuntimeError(failure_text(failure))
            modes.start(dataclasses.replace(self.cfg, codex_enabled=True),
                        factories={"codex": lambda cfg: self.service}, save=lambda values: True)
        return self

    def __exit__(self, *exc):
        from jarvis.bridge import modes
        from jarvis.tools.registry import BUILTIN_TOOLS

        if self._had_tv is None:
            BUILTIN_TOOLS.pop("tvControl", None)
        else:
            BUILTIN_TOOLS["tvControl"] = self._had_tv
        modes.reset()
        shutil.rmtree(self._workdir, ignore_errors=True)
        return False

    def run(self, scenario: Scenario) -> Tuple[List[TurnRun], List[Optional[str]], List[Dict[str, Any]]]:
        from unittest.mock import patch

        from jarvis.memory.conversation import DialogueMemory
        from jarvis.memory.db import Database
        from jarvis.reply import engine
        from jarvis.tools.confirmation import get_confirmation_store

        db = Database(":memory:", sqlite_vss_path=None)
        dialogue = DialogueMemory(inactivity_timeout=300, max_interactions=20)
        runs: List[TurnRun] = []
        failures: List[Optional[str]] = []
        desktops: List[Dict[str, Any]] = []
        state: Dict[str, Any] = {}
        reset = _reset_referents()
        try:
            with SimulatedDesktop() as desktop, patch.object(engine, "run_tool_with_retries", self.executor),                     _hidden_referents(not self.referents):
                scenario.setup(desktop, state)
                for turn in scenario.turns:
                    _snapshot_baseline(desktop, state)
                    _remember_existing(desktop, state)
                    start = len(self.executor.calls)
                    t0 = time.time()
                    reply = engine.run_reply_engine(db, self.cfg, None, turn.text, dialogue, quiet=self.quiet)
                    run = TurnRun(turn.text, reply or "", time.time() - t0, list(self.executor.calls[start:]))
                    get_confirmation_store().clear_pending()
                    failure = turn.check(desktop, state, run)
                    runs.append(run)
                    failures.append(failure)
                    desktops.append({"windows": desktop.snapshot(), "launches": list(desktop.launches)})
                    if failure is not None:
                        break
        finally:
            reset()
            db.close()
        return runs, failures, desktops


def _hidden_referents(hide: bool):
    """Keep recording referents but show none to any model, for an ablation run."""
    from contextlib import nullcontext
    from unittest.mock import patch

    if not hide:
        return nullcontext()
    from contextlib import ExitStack

    from jarvis.memory import desktop_referents
    stack = ExitStack()
    stack.enter_context(patch.object(desktop_referents, "live_referents", lambda cfg: []))
    if hasattr(desktop_referents, "live_others"):
        stack.enter_context(patch.object(desktop_referents, "live_others", lambda cfg: []))
    return stack


def _reset_referents() -> Callable[[], None]:
    """Each scenario starts with no remembered windows, when the running build keeps any."""
    try:
        from jarvis.memory.desktop_referents import get_desktop_referents
    except ImportError:
        return lambda: None
    get_desktop_referents().clear()
    return get_desktop_referents().clear


def run_all(mode: str, only: Optional[List[str]] = None, repeat: int = 1,
            overrides: Optional[Dict[str, Any]] = None, origin: str = "chat",
            referents: bool = True) -> Dict[str, Any]:
    selected = [s for s in SCENARIOS if not only or any(s.name.startswith(n) for n in only)]
    rows = []
    with FollowUpRunner(mode, overrides, origin, referents) as runner:
        model = (f"{runner.cfg.codex_model} ({runner.cfg.codex_reasoning_effort})" if mode == "codex"
                 else runner.cfg.llm_chat_model)
        for index in range(repeat):
            for scenario in selected:
                runs, failures, desktops = runner.run(scenario)
                passed = len(runs) == len(scenario.turns) and all(f is None for f in failures)
                rows.append({"scenario": scenario.name, "repeat": index + 1, "passed": passed,
                             "turns": [{"text": r.text, "reply": r.reply, "seconds": round(r.seconds, 2),
                                        "calls": r.calls, "failure": f, "desktop": d}
                                       for r, f, d in zip(runs, failures, desktops)]})
                print(f"{'✅' if passed else '❌'} #{index + 1} {scenario.name}", flush=True)
                for r, f in zip(runs, failures):
                    print(f"    {'✅' if f is None else '❌'} {r.text!r} ({r.seconds:.1f}s)", flush=True)
                    for name, args in r.calls:
                        print(f"        🔧 {name} {json.dumps(args, ensure_ascii=False)}", flush=True)
                    print(f"        💬 {r.reply[:160]!r}", flush=True)
                    if f is not None:
                        print(f"        ⚠️ {f}", flush=True)
    counts: Dict[str, str] = {}
    for scenario in selected:
        outcomes = [row["passed"] for row in rows if row["scenario"] == scenario.name]
        counts[scenario.name] = f"{sum(outcomes)}/{len(outcomes)}"
    return {"mode": mode, "model": model, "repeat": repeat, "overrides": overrides or {},
            "referents": referents, "passed": sum(r["passed"] for r in rows), "runs": len(rows),
            # Turns completed before a scenario's first failure: finer-grained than whole scenarios.
            "turns_passed": sum(sum(t["failure"] is None for t in r["turns"]) for r in rows),
            "turns_total": sum(len(s.turns) for s in selected) * repeat,
            "pass_counts": counts, "rows": rows}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Multi-turn desktop follow-up eval on a simulated desktop.")
    ap.add_argument("--mode", choices=["local", "codex"], required=True)
    ap.add_argument("--only", nargs="*", help="scenario names or name prefixes")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--origin", choices=["chat", "voice"], default="chat")
    ap.add_argument("--set", nargs="*", default=[], metavar="KEY=JSON",
                    help="setting overrides, e.g. codex_recent_dialogue_messages=6")
    ap.add_argument("--no-referents", action="store_true",
                    help="hide Jarvis's desktop referents from the models (ablation baseline)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    overrides = {}
    for item in args.set:
        key, _, value = item.partition("=")
        overrides[key] = json.loads(value)
    report = run_all(args.mode, args.only, max(1, args.repeat), overrides, args.origin, not args.no_referents)
    print(json.dumps({k: report[k] for k in ("mode", "model", "overrides", "referents", "passed", "runs",
                                              "turns_passed", "turns_total", "pass_counts")},
                     ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if report["passed"] == report["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())

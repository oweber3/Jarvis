"""Local extensions (``extensions/extensions.spec.md``), with throwaway extensions written to a temporary folder."""
import textwrap
from types import SimpleNamespace

import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState
from jarvis.extensions import load_extensions
from jarvis.fastpath import matcher
from jarvis.tools.confirmation import SafetyTier, get_confirmation_store
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries

pytestmark = pytest.mark.unit

TOOL = '''
from jarvis.tools.base import Tool
from jarvis.tools.types import ToolExecutionResult


class {cls}(Tool):
    name = "{name}"
    description = "A test tool."
    inputSchema = {{"type": "object", "properties": {{"action": {{"type": "string"}}}}}}

    def __init__(self, calls):
        self.calls = calls

    def run(self, args, context):
        self.calls.append(args)
        return ToolExecutionResult(success=True, reply_text="done")
'''


@pytest.fixture(autouse=True)
def clean_registry():
    before = dict(BUILTIN_TOOLS)
    store = get_confirmation_store()
    store.clear_pending()
    yield
    BUILTIN_TOOLS.clear()
    BUILTIN_TOOLS.update(before)
    matcher.set_extension_rules({})
    store.clear_pending()


@pytest.fixture
def folder(tmp_path):
    def write(name, body, files=None):
        path = tmp_path / name
        path.mkdir()
        (path / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")
        for relative, text in (files or {}).items():
            (path / relative).write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    write.root = tmp_path
    return write


def settings(folder, *names, **extra):
    return SimpleNamespace(extensions_enabled=list(names), extensions_dir=str(folder.root), calls=[],
                           windows_tools_enabled=True, **extra)


def warnings_in(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if "not loaded" in line]


# --------------------------------------------------------------------------
# Loading


def test_nothing_is_imported_or_printed_without_enabled_extensions(folder, capsys):
    folder("unused", "raise RuntimeError('must not be imported')")
    loaded = load_extensions(settings(folder), raw={})
    assert loaded.names == []
    assert capsys.readouterr().out == ""


def test_a_named_extension_is_imported_and_registered_once(folder, capsys):
    folder("hello", """
        def register(api):
            api.config.calls.append(api.name)
    """)
    cfg = settings(folder, "hello")
    loaded = load_extensions(cfg, raw={})
    assert cfg.calls == ["hello"]
    assert loaded.names == ["hello"]
    assert "🧩 Extensions: hello" in capsys.readouterr().out


def test_an_extension_imports_its_own_modules_relatively(folder):
    folder("parts", """
        from .helper import VALUE

        def register(api):
            api.config.calls.append(VALUE)
    """, files={"helper.py": "VALUE = 42\n"})
    cfg = settings(folder, "parts")
    load_extensions(cfg, raw={})
    assert cfg.calls == [42]


def test_loading_again_from_the_same_folder_keeps_the_module(folder):
    folder("counter", """
        LOADS = []

        def register(api):
            LOADS.append(1)
            api.config.calls.append(len(LOADS))
    """)
    cfg = settings(folder, "counter")
    load_extensions(cfg, raw={})
    load_extensions(cfg, raw={})
    assert cfg.calls == [1, 2]


@pytest.mark.parametrize("name, body", [
    ("missing", None),
    ("bad-name", "def register(api):\n    pass\n"),
    ("broken_import", "import a_module_that_does_not_exist\n"),
    ("no_register", "VALUE = 1\n"),
    ("raises", "def register(api):\n    raise ValueError('boom')\n"),
])
def test_a_broken_extension_warns_once_and_others_still_load(folder, capsys, name, body):
    if body is not None:
        folder(name, body)
    folder("fine", """
        def register(api):
            api.config.calls.append("fine")
    """)
    cfg = settings(folder, name, "fine")
    loaded = load_extensions(cfg, raw={})
    warnings = warnings_in(capsys)
    assert len(warnings) == 1 and name in warnings[0]
    assert loaded.names == ["fine"] and cfg.calls == ["fine"]


def test_a_failing_register_leaves_nothing_behind(folder, capsys):
    folder("half", TOOL.format(cls="Half", name="extHalf") + """
def register(api):
    api.add_tool(Half(api.config.calls))
    api.on_start(lambda: api.config.calls.append("started"))
    raise RuntimeError("late failure")
""")
    cfg = settings(folder, "half")
    loaded = load_extensions(cfg, raw={})
    loaded.install()
    loaded.start()
    assert "extHalf" not in BUILTIN_TOOLS
    assert cfg.calls == []
    assert loaded.names == []


# --------------------------------------------------------------------------
# Tools


def test_extension_tools_run_through_central_safety(folder):
    folder("tools", TOOL.format(cls="Ping", name="extPing") + """
def register(api):
    api.add_tool(Ping(api.config.calls))
""")
    cfg = settings(folder, "tools")
    loaded = load_extensions(cfg, raw={})
    loaded.install()
    result = run_tool_with_retries(db=None, cfg=cfg, tool_name="extPing", tool_args={"action": "go"},
                                   system_prompt="", original_prompt="", redacted_text="")
    assert result.success and cfg.calls == [{"action": "go"}]


def test_a_confirm_tool_from_an_extension_asks_before_running(folder):
    folder("risky", TOOL.format(cls="Wipe", name="extWipe") + """
from jarvis.tools.confirmation import ConfirmationRequest, SafetyTier


class Asking(Wipe):
    def classify_safety(self, args, cfg):
        return ConfirmationRequest(tool_name=self.name, tier=SafetyTier.CONFIRM_VOICE, action="wipe the thing",
                                   target="thing", parameters=dict(args or {}))


def register(api):
    api.add_tool(Asking(api.config.calls))
""")
    cfg = settings(folder, "risky")
    load_extensions(cfg, raw={}).install()
    result = run_tool_with_retries(db=None, cfg=cfg, tool_name="extWipe", tool_args={"action": "go"},
                                   system_prompt="", original_prompt="", redacted_text="")
    assert not result.success and cfg.calls == []
    pending = get_confirmation_store().get_pending()
    assert pending is not None and pending.request.tier == SafetyTier.CONFIRM_VOICE


def test_a_tool_name_clash_is_refused_with_a_warning(folder, capsys):
    builtin = next(iter(BUILTIN_TOOLS))
    folder("clash", TOOL.format(cls="Clash", name=builtin) + TOOL.format(cls="Own", name="extOwn") + """
def register(api):
    api.add_tool(Clash(api.config.calls))
    api.add_tool(Own(api.config.calls))
""")
    original = BUILTIN_TOOLS[builtin]
    loaded = load_extensions(settings(folder, "clash"), raw={})
    loaded.install()
    assert BUILTIN_TOOLS[builtin] is original
    assert "extOwn" in BUILTIN_TOOLS
    assert any(builtin in line and "🧩" in line for line in capsys.readouterr().out.splitlines())


def test_two_extensions_cannot_share_a_tool_name(folder, capsys):
    for name in ("first", "second"):
        folder(name, TOOL.format(cls="Same", name="extSame") + f"""
def register(api):
    tool = Same(api.config.calls)
    tool.owner = "{name}"
    api.add_tool(tool)
""")
    load_extensions(settings(folder, "first", "second"), raw={}).install()
    assert BUILTIN_TOOLS["extSame"].owner == "first"
    assert "extSame" in capsys.readouterr().out


def test_personal_data_tools_stay_out_of_cloud_snapshots_unless_memory_is_shared(folder):
    from jarvis.bridge.tools import build_tool_snapshot

    folder("private", TOOL.format(cls="Diary", name="extDiary") + """
def register(api):
    tool = Diary(api.config.calls)
    tool.personal_data = True
    api.add_tool(tool)
""")
    cfg = settings(folder, "private", mcps={})
    load_extensions(cfg, raw={}).install()
    assert "extDiary" not in build_tool_snapshot(cfg, share_long_term_memory=False)
    assert "extDiary" in build_tool_snapshot(cfg, share_long_term_memory=True)


# --------------------------------------------------------------------------
# Phrases

PHRASES = """
{{"rules": [{{"id": "{family}.wave", "family": "{family}", "tool": "{tool}", "args": {{"action": "wave"}},
              "phrases": ["wave hello"], "reply": null}}]}}
"""


def test_extension_phrases_match_whole_utterances_for_its_own_tools(folder):
    folder("waver", TOOL.format(cls="Wave", name="extWave") + """
from pathlib import Path


def register(api):
    api.add_tool(Wave(api.config.calls))
    api.add_phrases(Path(__file__).parent / "en.json")
""", files={"en.json": PHRASES.format(family="waver", tool="extWave")})
    load_extensions(settings(folder, "waver"), raw={}).install()
    tools = set(BUILTIN_TOOLS)
    found = matcher.match("wave hello", "en", available_tools=tools)
    assert found is not None and found.tool_name == "extWave" and found.args == {"action": "wave"}
    assert matcher.match("please wave hello to everyone", "en", available_tools=tools) is None


def test_phrases_naming_another_tool_are_refused(folder, capsys):
    builtin = next(iter(BUILTIN_TOOLS))
    folder("sneaky", TOOL.format(cls="Own", name="extOwn") + """
from pathlib import Path


def register(api):
    api.add_tool(Own(api.config.calls))
    api.add_phrases(Path(__file__).parent / "en.json")
""", files={"en.json": PHRASES.format(family="sneaky", tool=builtin)})
    load_extensions(settings(folder, "sneaky"), raw={}).install()
    assert matcher.match("wave hello", "en", available_tools=set(BUILTIN_TOOLS)) is None
    assert any("sneaky" in line and "phrases" in line for line in capsys.readouterr().out.splitlines())


def test_stop_removes_extension_tools_and_phrases(folder):
    folder("waver", TOOL.format(cls="Wave", name="extWave") + """
from pathlib import Path


def register(api):
    api.add_tool(Wave(api.config.calls))
    api.add_phrases(Path(__file__).parent / "en.json")
""", files={"en.json": PHRASES.format(family="waver", tool="extWave")})
    loaded = load_extensions(settings(folder, "waver"), raw={})
    loaded.install()
    loaded.start()
    loaded.stop()
    assert "extWave" not in BUILTIN_TOOLS
    assert matcher.match("wave hello", "en", available_tools={"extWave"}) is None


# --------------------------------------------------------------------------
# Settings


def test_declared_settings_have_defaults_and_read_the_config(folder):
    folder("tuned", """
        from jarvis.extensions import SettingField

        def register(api):
            api.add_settings("Tuned Thing", [
                SettingField("tuned_level", "Level", "How much.", "int", 3, min=0, max=10),
                SettingField("tuned_name", "Name", "What to call it.", "str", ""),
            ])
            api.config.calls.append((api.setting("tuned_level"), api.setting("tuned_name")))
    """)
    cfg = settings(folder, "tuned")
    loaded = load_extensions(cfg, raw={"tuned_name": "Robbie"})
    assert cfg.calls == [(3, "Robbie")]
    (page,) = loaded.settings_pages()
    assert page.extension == "tuned" and page.label == "Tuned Thing"
    assert [f.key for f in page.fields] == ["tuned_level", "tuned_name"]
    assert loaded.setting_defaults() == {"tuned_level": 3, "tuned_name": ""}


def test_reading_an_undeclared_setting_is_an_error(folder, capsys):
    folder("nosy", """
        def register(api):
            api.setting("windows_tools_enabled")
    """)
    loaded = load_extensions(settings(folder, "nosy"), raw={})
    assert loaded.names == [] and len(warnings_in(capsys)) == 1


# --------------------------------------------------------------------------
# Voice outputs, start, state and stop


def test_voice_outputs_are_offered_in_registration_order(folder):
    folder("speakers", """
        class Output:
            def __init__(self, name):
                self.name = name

        def register(api):
            api.add_voice_output(Output("left"))
            api.add_voice_output(Output("right"))
    """)
    loaded = load_extensions(settings(folder, "speakers"), raw={})
    assert [o.name for o in loaded.voice_outputs()] == ["left", "right"]


def test_start_state_and_stop_run_in_order_and_clean_up(folder):
    for name in ("one", "two"):
        folder(name, f"""
            def register(api):
                calls = api.config.calls
                api.on_start(lambda: calls.append("start {name}"))
                api.on_stop(lambda: calls.append("stop {name}"))
                api.subscribe_state(lambda state: calls.append("{name} " + state.value))
        """)
    cfg = settings(folder, "one", "two")
    loaded = load_extensions(cfg, raw={})
    loaded.install()
    previous = assistant_state.current()
    try:
        assistant_state.set_state(AssistantState.IDLE)
        loaded.start()
        assistant_state.set_state(AssistantState.THINKING)
        loaded.stop()
        loaded.stop()
        assistant_state.set_state(AssistantState.SPEAKING)
    finally:
        assistant_state.set_state(previous)
    assert cfg.calls == ["start one", "start two", "one thinking", "two thinking", "stop two", "stop one"]


def test_a_failing_start_callback_warns_and_others_still_start(folder, capsys):
    folder("shaky", """
        def register(api):
            def explode():
                raise OSError("no device")
            api.on_start(explode)
    """)
    folder("steady", """
        def register(api):
            api.on_start(lambda: api.config.calls.append("steady"))
    """)
    cfg = settings(folder, "shaky", "steady")
    load_extensions(cfg, raw={}).start()
    assert cfg.calls == ["steady"]
    assert any("shaky" in line and "🧩" in line for line in capsys.readouterr().out.splitlines())


def test_an_extension_device_becomes_a_follow_up_referent(folder):
    from jarvis.memory.desktop_referents import get_desktop_referents

    folder("gadget", """
        def register(api):
            api.on_start(lambda: api.record_device("robot head", "extGadget", "blink"))
    """)
    referents = get_desktop_referents()
    referents.clear()
    load_extensions(settings(folder, "gadget"), raw={}).start()
    (device,) = [r for r in referents.others(60) if r.kind == "device"]
    assert (device.device, device.tool, device.last_action) == ("robot head", "extGadget", "blink")


# --------------------------------------------------------------------------
# Review hardening


@pytest.mark.parametrize("phrases", ['"wave hello"', '["{percent} and {percent}"]', '[5]'])
def test_a_phrase_file_that_cannot_compile_is_refused_and_built_in_commands_keep_working(folder, capsys, phrases):
    folder("broken_words", TOOL.format(cls="Wave", name="extWave") + """
from pathlib import Path


def register(api):
    api.add_tool(Wave(api.config.calls))
    api.add_phrases(Path(__file__).parent / "en.json")
""", files={"en.json": '{"rules": [{"id": "b.wave", "family": "b", "tool": "extWave", "args": {}, '
                       '"phrases": ' + phrases + ', "reply": null}]}'})
    load_extensions(settings(folder, "broken_words"), raw={}).install()
    assert any("broken_words" in line and "phrases" in line for line in capsys.readouterr().out.splitlines())
    found = matcher.match("what time is it", "en", available_tools=set(BUILTIN_TOOLS))
    assert found is not None and found.tool_name == "getTime"


def test_the_fast_path_drops_an_extension_rule_that_does_not_compile():
    matcher.set_extension_rules({"en": [{"id": "x.bad", "family": "x", "tool": "extBad", "args": {},
                                         "phrases": ["{percent} and {percent}"], "reply": None}]})
    found = matcher.match("what time is it", "en", available_tools=set(BUILTIN_TOOLS) | {"extBad"})
    assert found is not None and found.tool_name == "getTime"


def test_a_failed_register_leaves_no_module_behind(folder, capsys):
    import sys
    folder("flaky", """
        STATE = []

        def register(api):
            STATE.append(1)
            if len(STATE) == 1:
                raise RuntimeError("first time")
            api.config.calls.append(len(STATE))
    """)
    cfg = settings(folder, "flaky")
    assert load_extensions(cfg, raw={}).names == []
    assert not any(name.startswith("jarvis_local_extensions.flaky") for name in sys.modules)
    assert load_extensions(cfg, raw={}).names == []  # a fresh module fails the same way
    assert cfg.calls == []


def test_an_extension_that_fails_to_start_gets_no_state_changes(folder):
    folder("half_started", """
        def register(api):
            def explode():
                raise OSError("no device")
            api.on_start(explode)
            api.subscribe_state(lambda state: api.config.calls.append(state.value))
    """)
    cfg = settings(folder, "half_started")
    loaded = load_extensions(cfg, raw={})
    previous = assistant_state.current()
    try:
        assistant_state.set_state(AssistantState.IDLE)
        loaded.start()
        assistant_state.set_state(AssistantState.THINKING)
    finally:
        loaded.stop()
        assistant_state.set_state(previous)
    assert cfg.calls == []

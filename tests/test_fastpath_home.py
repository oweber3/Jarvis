"""Home commands are deterministic fast routes against the in-memory device names (smarthome.spec.md, Fast path)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.fastpath.matcher import FastTarget, match

TOOLS = {'getTime', 'appControl', 'windowControl', 'systemVolume', 'mediaControl', 'tvControl', 'homeControl'}

def target(names, device_id, kind, display, capabilities=None):
    from jarvis.smarthome.model import capabilities_for
    from jarvis.tools.builtin.home_control import capability_key
    return FastTarget(names, device_id, capability_key(capabilities or capabilities_for(kind)), display)


DEVICES = (
    target(('Bedroom Lamp', 'Reading light'), 'ha:light.bedroom_lamp', 'light', 'Bedroom Lamp'),
    target(('Kettle',), 'ha:switch.kettle', 'switch', 'Kettle'),
    target(('Lounge Blinds',), 'ha:cover.blinds', 'cover', 'Lounge Blinds'),
    target(('Garage Door',), 'ha:cover.garage', 'cover', 'Garage Door'),
    target(('Front Door',), 'ha:lock.front', 'lock', 'Front Door'),
    target(('Desk Light',), 'ha:light.desk_a', 'light', 'Desk Light'),
    target(('Desk Light Strip',), 'ha:light.desk_b', 'light', 'Desk Light Strip'),
)
AREAS = (FastTarget(('Kitchen',), 'Kitchen', 'area', 'Kitchen'), FastTarget(('Living Room',), 'Living Room',
                                                                              'area', 'Living Room'))


def route(text, devices=DEVICES, areas=AREAS, tools=TOOLS):
    return match(text, 'en', home_devices=devices, home_areas=areas, available_tools=tools)


def rules():
    data = json.loads((Path(__file__).resolve().parents[1] / 'src/jarvis/fastpath/phrases/en.json')
                      .read_text(encoding='utf-8'))
    return [rule for rule in data['rules'] if rule['family'] == 'home']


@pytest.mark.unit
class TestMatching:
    @pytest.mark.parametrize('text,action', [
        ('Turn on the bedroom lamp', 'turn_on'), ('turn the bedroom lamp off', 'turn_off'),
        ('switch on the kettle', 'turn_on'), ('Switch the kettle off.', 'turn_off'),
        ('turn on the reading light', 'turn_on'),
    ])
    def test_on_and_off_reach_the_one_device(self, text, action):
        result = route(text)
        assert result is not None, text
        assert result.tool_name == 'homeControl' and result.args['action'] == action
        assert result.args['target'] in ('ha:light.bedroom_lamp', 'ha:switch.kettle')

    @pytest.mark.parametrize('text,action', [('Turn off the kitchen lights', 'turn_off'),
                                             ('turn the living room lights on', 'turn_on')])
    def test_room_lights_reach_the_area(self, text, action):
        result = route(text)
        assert result.args == {'action': action, 'area': result.args['area'], 'kind': 'light'}
        assert result.args['area'] in ('Kitchen', 'Living Room')

    def test_set_to_a_percentage_is_the_devices_own_level(self):
        result = route('set the lounge blinds to 40 percent')
        assert result.args == {'action': 'set', 'target': 'ha:cover.blinds', 'level': 40.0}

    def test_dim_reaches_lights_only(self):
        assert route('dim the bedroom lamp').args['action'] == 'set'
        assert route('dim the bedroom lamp to 20 percent').args == {'action': 'set', 'target': 'ha:light.bedroom_lamp',
                                                                   'brightness': 20.0}
        assert route('dim the kettle') is None

    def test_an_unknown_or_ambiguous_device_falls_through(self):
        assert route('turn on the jacuzzi') is None
        assert route('turn on the desk') is None          # Desk Light and Desk Light Strip
        assert route('turn on the kitchen lights', areas=()) is None

    def test_actions_a_device_kind_cannot_do_fall_through(self):
        assert route('turn on the front door') is None

    def test_the_devices_own_capabilities_decide_not_its_kind(self):
        from jarvis.smarthome.model import Capability
        plain = (target(('Porch Light',), 'ha:light.porch', 'light', 'Porch Light', {Capability.ON_OFF}),)
        assert route('turn on the porch light', devices=plain) is not None
        assert route('dim the porch light', devices=plain) is None
        assert route('set the porch light to 50 percent', devices=plain) is None

    def test_without_the_tool_nothing_matches(self):
        assert route('turn on the bedroom lamp', tools=TOOLS - {'homeControl'}) is None

    def test_tv_phrases_keep_their_meaning(self):
        result = route('turn off the tv')
        assert result.tool_name == 'tvControl'

    def test_every_home_rule_is_a_home_control_call(self):
        assert rules() and all(rule['tool'] == 'homeControl' for rule in rules())
        assert all(rule['args']['action'] in ('turn_on', 'turn_off', 'set', 'open', 'close') for rule in rules())


@pytest.fixture
def live_home():
    from jarvis.smarthome import service
    from jarvis.smarthome.model import Device, Registry, capabilities_for
    from jarvis.tools.builtin.home_control import HomeControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS

    def dev(id_, name, kind, area="", device_class=""):
        return Device(id_, name, kind, "ha", area=area, state="off", capabilities=capabilities_for(kind),
                      device_class=device_class)

    registry = Registry()
    registry.replace_source("ha", [dev("ha:light.bedroom_lamp", "Bedroom Lamp", "light", "Bedroom"),
                                   dev("ha:cover.garage", "Garage Door", "cover", device_class="garage"),
                                   dev("ha:cover.blinds", "Lounge Blinds", "cover", "Lounge", "blind")]
                            + [dev(f"ha:light.attic_{i}", f"Attic {i}", "light", "Attic") for i in range(11)])
    service.set_home(service.SmartHome(registry, {}))
    BUILTIN_TOOLS["homeControl"] = HomeControlTool()
    yield
    BUILTIN_TOOLS.pop("homeControl", None)
    service.set_home(None)


def offered(text, windows=False):
    from jarvis.fastpath.dispatcher import match_command
    cfg = SimpleNamespace(fast_commands_enabled=True, fast_commands_locales=['en'], windows_tools_enabled=windows,
                          smarthome_confirm_devices=[])
    return match_command(text, cfg)


@pytest.mark.unit
class TestDispatcher:
    def test_safe_home_commands_are_offered_with_live_names(self, live_home):
        result = offered('turn on the bedroom lamp')
        assert result is not None and result.args == {'action': 'turn_on', 'target': 'ha:light.bedroom_lamp'}
        assert offered('open the lounge blinds') is not None

    def test_confirm_voice_actions_fall_through_to_the_model(self, live_home):
        assert offered('open the garage door') is None
        assert offered('turn off the attic lights') is None   # 11 devices at once


@pytest.fixture
def app_and_tv_tools(live_home):
    """An application catalogue holding Office, and a TV, next to the live home devices."""
    from jarvis.smarthome import service
    from jarvis.smarthome.model import Device, capabilities_for
    from jarvis.tools.base import Tool
    from jarvis.tools.registry import BUILTIN_TOOLS

    class FakeApps(Tool):
        name = 'appControl'
        description = 'apps'
        inputSchema = {'type': 'object'}

        def run(self, args, context):
            raise AssertionError('not run')

        def fast_targets(self, cfg):
            return (FastTarget(('Office',), 'office', 'office.exe', 'Office'),)

    class FakeTv(FakeApps):
        name = 'tvControl'

        def fast_targets(self, cfg):
            return ()

    service.get_home().registry.upsert(Device("ha:cover.office_blind", "Office Blind", "cover", "ha",
                                              capabilities=capabilities_for("cover"), device_class="blind"))
    service.get_home().registry.upsert(Device("ha:light.tv_backlight", "TV Backlight", "light", "ha",
                                              capabilities=capabilities_for("light")))
    saved = {name: BUILTIN_TOOLS.get(name) for name in ('appControl', 'tvControl')}
    BUILTIN_TOOLS['appControl'], BUILTIN_TOOLS['tvControl'] = FakeApps(), FakeTv()
    yield
    for name, tool in saved.items():
        if tool is None:
            BUILTIN_TOOLS.pop(name, None)
        else:
            BUILTIN_TOOLS[name] = tool


@pytest.mark.unit
class TestNeighbours:
    def test_the_tv_keeps_its_own_phrases_beside_a_tv_named_device(self, app_and_tv_tools):
        result = offered('turn off the tv', windows=True)
        assert result is not None and result.tool_name == 'tvControl'

    def test_a_name_both_an_app_and_a_device_could_mean_goes_to_the_model(self, app_and_tv_tools):
        assert offered('open office', windows=True) is None

    def test_home_routes_work_without_the_windows_tools(self, app_and_tv_tools):
        assert offered('open the office blind', windows=False).tool_name == 'homeControl'


@pytest.mark.unit
def test_a_failed_home_route_says_the_short_message_never_the_fenced_data(live_home):
    from jarvis.fastpath.dispatcher import dispatch
    cfg = SimpleNamespace(fast_commands_enabled=True, fast_commands_locales=['en'], windows_tools_enabled=False,
                          smarthome_confirm_devices=[], db_path=':memory:')
    result = offered('turn on the bedroom lamp')
    spoken = dispatch(result, None, cfg, 'turn on the bedroom lamp')
    assert spoken and 'UNTRUSTED' not in spoken and '{' not in spoken

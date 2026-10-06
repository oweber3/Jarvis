"""tvControl: actions, argument validation, honest failures and registration, against a fake TV."""
import pytest


@pytest.fixture
def tv(fake_tv, mock_config):
    mock_config.roku_host = "127.0.0.1"
    return fake_tv


def run(cfg, **args):
    from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
    from jarvis.tools.builtin.tv_control import TvControlTool
    BUILTIN_TOOLS.setdefault("tvControl", TvControlTool())
    return run_tool_with_retries(db=None, cfg=cfg, tool_name="tvControl", tool_args=args, system_prompt="",
                                 original_prompt="", redacted_text="", max_retries=1, quiet=True)


@pytest.fixture(autouse=True)
def _clean_registry():
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS.pop("tvControl", None)
    yield
    BUILTIN_TOOLS.pop("tvControl", None)


# --- key -------------------------------------------------------------------------------------------

def test_key_presses_the_key(tv, mock_config):
    result = run(mock_config, action="key", key="Home")
    assert result.success and tv.keys() == ["Home"]


def test_key_repeat_steps_volume(tv, mock_config):
    result = run(mock_config, action="key", key="VolumeDown", repeat=4)
    assert result.success and tv.keys() == ["VolumeDown"] * 4
    assert "4" in result.reply_text


def test_repeat_is_capped(tv, mock_config):
    from jarvis.devices.roku import MAX_REPEAT
    run(mock_config, action="key", key="VolumeUp", repeat=500)
    assert tv.keys() == ["VolumeUp"] * MAX_REPEAT


def test_repeat_accepts_a_numeric_string_from_a_small_model(tv, mock_config):
    run(mock_config, action="key", key="VolumeUp", repeat="3")
    assert tv.keys() == ["VolumeUp"] * 3


@pytest.mark.parametrize("args", [{"action": "key"}, {"action": "key", "key": "Format"},
                                  {"action": "key", "key": "../launch/12"}])
def test_bad_keys_fail_without_touching_the_tv(tv, mock_config, args):
    result = run(mock_config, **args)
    assert not result.success and tv.posts() == []


def test_power_off_is_routine_and_needs_no_confirmation(tv, mock_config):
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    from jarvis.tools.builtin.tv_control import TvControlTool
    for key in ("PowerOff", "PowerOn", "VolumeUp"):
        safety = evaluate_safety("tvControl", {"action": "key", "key": key}, mock_config, tool=TvControlTool())
        assert safety.tier == SafetyTier.SAFE
    assert run(mock_config, action="key", key="PowerOff").success
    assert tv.keys() == ["PowerOff"]


# --- launch ----------------------------------------------------------------------------------------

def test_launch_resolves_the_name_against_the_tvs_own_app_list(tv, mock_config):
    tv.apps = [("777", "appl", "Brand New App")]
    result = run(mock_config, action="launch", app="brand new app")
    assert result.success and tv.posts() == ["/launch/777"]
    assert "Brand New App" in result.reply_text


def test_launch_exact_name_beats_a_token_match(tv, mock_config):
    result = run(mock_config, action="launch", app="plex")
    assert result.success and tv.posts() == ["/launch/100"]


def test_ambiguous_launch_lists_candidates_and_launches_nothing(tv, mock_config):
    tv.apps = [("1", "appl", "Plex Player"), ("2", "appl", "Plex Media Server")]
    result = run(mock_config, action="launch", app="plex")
    assert not result.success and tv.posts() == []
    assert "Plex Player" in result.error_message and "Plex Media Server" in result.error_message


def test_unknown_app_says_what_is_installed(tv, mock_config):
    result = run(mock_config, action="launch", app="crunchyroll")
    assert not result.success and tv.posts() == []
    assert "Netflix" in result.error_message


def test_launch_needs_an_app_name(tv, mock_config):
    assert not run(mock_config, action="launch").success
    assert tv.posts() == []


def test_hdmi_inputs_launch_like_apps(tv, mock_config):
    assert run(mock_config, action="launch", app="HDMI 1").success
    assert tv.posts() == ["/launch/tvin.hdmi1"]


def test_launch_refreshes_the_cached_app_list(tv, mock_config):
    from jarvis.devices.roku import get_device
    run(mock_config, action="launch", app="netflix")
    assert any(a.name == "Netflix" for a in get_device("127.0.0.1").cached_apps())


# --- type ------------------------------------------------------------------------------------------

def test_type_sends_the_text_and_does_not_echo_it(tv, mock_config):
    result = run(mock_config, action="type", text="the office")
    assert result.success and "".join(k[4:] for k in tv.keys()) == "the office"
    assert "the office" not in result.reply_text


def test_type_needs_text_and_bounds_it(tv, mock_config):
    assert not run(mock_config, action="type").success
    assert not run(mock_config, action="type", text="x" * 500).success
    assert tv.posts() == []


# --- status ----------------------------------------------------------------------------------------

def test_status_reports_name_power_active_app_and_apps_with_gets_only(tv, mock_config):
    tv.active = "Netflix"
    result = run(mock_config, action="status")
    assert result.success
    for fact in ("Living Room TV", "PowerOn", "Netflix", "Hulu"):
        assert fact in result.reply_text
    assert tv.posts() == []


def test_status_in_standby_says_so(tv, mock_config):
    tv.mode = "standby"
    assert "Ready" in run(mock_config, action="status").reply_text


# --- failures --------------------------------------------------------------------------------------

def test_forbidden_tells_the_user_where_the_setting_is(tv, mock_config):
    tv.mode = "limited"
    result = run(mock_config, action="key", key="Home")
    assert not result.success
    assert "Control by mobile apps" in result.error_message and "Network access" in result.error_message


def test_an_unreachable_tv_is_reported_honestly(tv, mock_config, monkeypatch):
    from jarvis.devices import roku

    def refuse(self, *args, **kwargs):
        raise roku.RokuUnreachable("x")

    monkeypatch.setattr(roku.RokuClient, "_send", refuse)
    result = run(mock_config, action="key", key="Home")
    assert not result.success and "did not answer" in result.error_message


def test_a_moved_tv_is_followed_and_the_user_is_told_to_update_the_address(tv, mock_config, monkeypatch):
    from jarvis.devices import roku
    mock_config.roku_host = "127.0.0.2"
    device = roku.get_device("127.0.0.2")
    device.serial = "FAKESERIAL1"
    real = roku.RokuClient._send

    def send(self, method, path, **kw):
        if self.host == "127.0.0.2":
            raise roku.RokuUnreachable("x")
        return real(self, method, path, **kw)

    monkeypatch.setattr(roku.RokuClient, "_send", send)
    monkeypatch.setattr(roku, "discover", lambda *a, **k: [roku.Discovered("127.0.0.1", "FAKESERIAL1")])
    result = run(mock_config, action="key", key="Home")
    assert result.success and tv.keys() == ["Home"]
    assert "roku_host" in result.reply_text and "127.0.0.1" not in result.reply_text


def test_an_unusable_host_fails_clearly(fake_tv, mock_config):
    mock_config.roku_host = "8.8.8.8"
    result = run(mock_config, action="key", key="Home")
    assert not result.success and "private" in result.error_message
    assert fake_tv.requests == []


def test_unknown_action_fails(tv, mock_config):
    assert not run(mock_config, action="reboot").success


# --- description and schema ------------------------------------------------------------------------

def test_the_description_routes_the_tv_apart_from_the_pc():
    from jarvis.tools.builtin.tv_control import TvControlTool
    tool = TvControlTool()
    routed = tool.description[:120]  # all the router reads
    assert "TV" in routed and "NOT the PC" in routed
    assert "systemVolume" in tool.description and "mediaControl" in tool.description


def test_schema_enums_come_from_the_client_vocabulary():
    from jarvis.devices.roku import KEYS, MAX_REPEAT
    from jarvis.tools.builtin.tv_control import TvControlTool
    props = TvControlTool().inputSchema["properties"]
    assert props["key"]["enum"] == list(KEYS)
    assert props["repeat"]["maximum"] == MAX_REPEAT
    assert props["action"]["enum"] == ["key", "launch", "type", "status"]


# --- registration ----------------------------------------------------------------------------------

def test_tool_is_not_registered_without_a_host(mock_config):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_tv_tool
    mock_config.roku_host = ""
    configure_tv_tool(mock_config, warm=False)
    assert "tvControl" not in BUILTIN_TOOLS


def test_tool_is_registered_for_a_private_host_and_removed_when_unset(mock_config):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_tv_tool
    mock_config.roku_host = "192.168.1.50"
    configure_tv_tool(mock_config, warm=False)
    assert "tvControl" in BUILTIN_TOOLS
    mock_config.roku_host = ""
    configure_tv_tool(mock_config, warm=False)
    assert "tvControl" not in BUILTIN_TOOLS


@pytest.mark.parametrize("host", ["8.8.8.8", "example.com", "garbage"])
def test_tool_is_not_registered_for_an_unsafe_host(mock_config, host, capsys):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_tv_tool
    mock_config.roku_host = host
    configure_tv_tool(mock_config, warm=False)
    assert "tvControl" not in BUILTIN_TOOLS
    assert "roku_host" in capsys.readouterr().out


def test_registration_warms_the_app_cache_in_the_background(tv, mock_config):
    import time
    from jarvis.devices.roku import get_device
    from jarvis.tools.registry import configure_tv_tool
    configure_tv_tool(mock_config, warm=True)
    deadline = time.monotonic() + 3
    while not get_device("127.0.0.1").cached_apps() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert get_device("127.0.0.1").cached_apps()
    assert tv.posts() == []


def test_fast_targets_come_from_the_cache_without_network(tv, mock_config):
    from jarvis.devices.roku import get_device
    from jarvis.tools.builtin.tv_control import TvControlTool
    assert TvControlTool().fast_targets(mock_config) == ()
    get_device("127.0.0.1").apps()
    tv.requests.clear()
    names = {t.display for t in TvControlTool().fast_targets(mock_config)}
    assert {"Netflix", "Disney Plus", "HDMI 1"} <= names
    assert tv.requests == []


def test_fast_targets_also_offer_the_short_name_before_a_dash(tv, mock_config):
    from jarvis.devices.roku import get_device
    from jarvis.fastpath.matcher import match
    from jarvis.tools.builtin.tv_control import TvControlTool
    tv.apps = [("41468", "appl", "Tubi - Free Movies & TV"), ("12", "appl", "Netflix")]
    get_device("127.0.0.1").apps()
    targets = TvControlTool().fast_targets(mock_config)
    route = match("Put on Tubi", tv_apps=targets, available_tools={"tvControl"})
    assert route is not None
    assert route.args == {"action": "launch", "app": "Tubi - Free Movies & TV"}

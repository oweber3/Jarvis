"""Checks against a real TV on the local network. The address comes from JARVIS_TEST_ROKU_HOST, never from the code.

Read-only checks (GET only) are ``integration`` and skipped without the variable. Anything that presses a
key, launches an app or types is ``interactive``: it changes what someone may be watching, so automated
runs never do it (``conftest.py`` refuses such requests). Run by hand when the TV is free:

    JARVIS_TEST_ROKU_HOST=<tv address> pytest -m interactive tests/test_roku_real_device.py
"""
import os

import pytest

HOST = os.environ.get("JARVIS_TEST_ROKU_HOST", "")


@pytest.mark.integration
@pytest.mark.skipif(not HOST, reason="JARVIS_TEST_ROKU_HOST is not set")
class TestReadOnly:
    def test_device_info_apps_and_active_app_answer(self):
        from jarvis.devices.roku import RokuClient
        with RokuClient(HOST) as client:
            info = client.device_info()
            apps = client.apps()
            client.active_app()
        assert info.name and info.power_mode
        assert apps and all(app.id and app.name for app in apps)

    def test_automated_runs_cannot_command_the_real_tv(self):
        from jarvis.devices.roku import RokuClient
        with RokuClient(HOST) as client:
            with pytest.raises(AssertionError, match="must not send POST"):
                client.key("Home")


@pytest.mark.interactive
@pytest.mark.skipif(not HOST, reason="JARVIS_TEST_ROKU_HOST is not set")
class TestOnTheRealTv:
    def test_home_and_back_and_a_volume_step(self):
        from jarvis.devices.roku import RokuClient
        with RokuClient(HOST) as client:
            client.key("Home")
            client.key("VolumeUp")
            client.key("VolumeDown")

    def test_launch_netflix_by_name_from_the_tvs_own_list(self):
        from jarvis.devices.roku import RokuClient, resolve_app
        with RokuClient(HOST) as client:
            app, _ = resolve_app("netflix", client.apps())
            assert app is not None
            client.launch(app.id)

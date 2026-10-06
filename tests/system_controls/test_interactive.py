"""State-changing checks against a real Windows PC. Run by hand: ``pytest -m interactive tests/system_controls``.

Each test restores what it changed. They move the screen, the sound and the keyboard focus, so run
them when nobody is using the machine.
"""
import time

import pytest

from jarvis.platform.windows import (audio_outputs, brightness, input_control, power, virtual_desktops)

pytestmark = pytest.mark.interactive


def test_brightness_changes_and_is_restored():
    before = {r.device: r.percent for r in brightness.read_levels() if r.percent is not None}
    assert before, 'no DDC/CI capable monitor'
    try:
        readings = brightness.set_levels(max(0, min(100, next(iter(before.values())) - 10)))
        assert any(r.verified for r in readings if r.percent is not None)
    finally:
        for device, percent in before.items():
            brightness.set_levels(percent, device=device)


def test_the_default_audio_output_switches_and_returns():
    outputs = audio_outputs.list_outputs()
    if len(outputs) < 2:
        pytest.skip('needs two playback devices')
    original = next(device for device in outputs if device.default)
    other = next(device for device in outputs if not device.default)
    try:
        assert audio_outputs.set_default_output(other.name, {}).default
    finally:
        audio_outputs.set_default_output(original.name, {})
    assert next(device for device in audio_outputs.list_outputs() if device.default).id == original.id


def test_the_power_plan_switches_and_returns():
    plans = power.list_plans()
    if len(plans) < 2:
        pytest.skip('needs two power plans')
    original = next(plan for plan in plans if plan.active)
    other = next(plan for plan in plans if not plan.active)
    try:
        assert power.set_plan(other.guid).guid == other.guid
    finally:
        power.set_plan(original.guid)


def test_virtual_desktops_switch_create_and_close():
    start = virtual_desktops.state()
    created = virtual_desktops.create()
    try:
        assert created.count == start.count + 1 and created.current == created.count
        assert virtual_desktops.switch(str(start.current)).current == start.current
    finally:
        virtual_desktops.close(str(created.count))
        virtual_desktops.switch(str(start.current))
    assert virtual_desktops.state().count == start.count


def test_a_hotkey_reaches_the_focused_window():
    """Opens and closes the Run dialog (win+r then esc). Watch the screen for it."""
    input_control.send_chord(input_control.parse_chord('win+r'))
    time.sleep(1)
    input_control.send_chord(input_control.parse_chord('esc'))


def test_the_real_clipboard_round_trips():
    original = input_control.read_clipboard()
    try:
        input_control.write_clipboard('jarvis clipboard check')
        assert input_control.read_clipboard()['text'] == 'jarvis clipboard check'
    finally:
        if not original.get('empty'):
            input_control.write_clipboard(original['text'])

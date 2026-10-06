"""Power plans through powercfg, with the process boundary replaced."""
import pytest

from jarvis.platform.windows import power

BALANCED = '381b4222-f694-41f0-9685-ff5bb260df2e'
HIGH = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
SAVER = 'a1841308-3541-4fab-bc81-f71556f20b4a'
CUSTOM = '11111111-2222-3333-4444-555555555555'

LIST_OUTPUT = f"""Existing Power Schemes (* Active)
-----------------------------------
Power Scheme GUID: {BALANCED}  (Balanced) *
Power Scheme GUID: {HIGH}  (High performance)
Power Scheme GUID: {SAVER}  (Power saver)
Power Scheme GUID: {CUSTOM}  (Gaming (quiet))
"""

LOCALISED_OUTPUT = f"""Vorhandene Energieschemas (* Aktiv)
-----------------------------------
Energieschema-GUID: {BALANCED}  (Ausbalanciert) *
Energieschema-GUID: {HIGH}  (Höchstleistung)
"""


class FakePowercfg:
    def __init__(self, output=LIST_OUTPUT):
        self.output = output
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.output if args == ('/list',) else ''


@pytest.fixture
def powercfg(monkeypatch):
    fake = FakePowercfg()
    monkeypatch.setattr(power, '_powercfg', fake)
    return fake


def test_plans_are_parsed_with_guid_name_and_active_flag(powercfg):
    plans = power.list_plans()
    assert [(p.guid, p.name, p.active) for p in plans] == [
        (BALANCED, 'Balanced', True), (HIGH, 'High performance', False),
        (SAVER, 'Power saver', False), (CUSTOM, 'Gaming (quiet)', False)]


def test_parsing_does_not_depend_on_the_windows_display_language(monkeypatch):
    monkeypatch.setattr(power, '_powercfg', FakePowercfg(LOCALISED_OUTPUT))
    plans = power.list_plans()
    assert [(p.name, p.active) for p in plans] == [('Ausbalanciert', True), ('Höchstleistung', False)]


@pytest.mark.parametrize('query,guid', [
    ('balanced', BALANCED), ('high_performance', HIGH), ('High performance', HIGH),
    ('power saver', SAVER), ('power_saver', SAVER), (CUSTOM.upper(), CUSTOM), ('gaming', CUSTOM),
])
def test_a_plan_resolves_from_a_key_guid_or_name(powercfg, query, guid):
    assert power.resolve_plan(query, power.list_plans()).guid == guid


def test_a_plan_that_is_not_installed_is_reported_not_substituted(monkeypatch):
    monkeypatch.setattr(power, '_powercfg', FakePowercfg(LOCALISED_OUTPUT))
    with pytest.raises(ValueError) as error:
        power.resolve_plan('power_saver', power.list_plans())
    assert 'Ausbalanciert' in str(error.value)


def test_a_name_matching_two_plans_is_ambiguous(monkeypatch):
    output = LIST_OUTPUT + f'Power Scheme GUID: 22222222-2222-3333-4444-555555555555  (Gaming (loud))\n'
    monkeypatch.setattr(power, '_powercfg', FakePowercfg(output))
    with pytest.raises(ValueError):
        power.resolve_plan('gaming', power.list_plans())


def test_set_plan_activates_the_resolved_guid_and_reports_what_is_active(monkeypatch):
    state = {'active': BALANCED}

    def fake(*args):
        if args == ('/list',):
            lines = [f"Power Scheme GUID: {g}  ({n}){' *' if g == state['active'] else ''}"
                     for g, n in ((BALANCED, 'Balanced'), (HIGH, 'High performance'))]
            return 'Existing Power Schemes\n' + '\n'.join(lines)
        assert args[0] == '/setactive'
        state['active'] = args[1]
        return ''
    monkeypatch.setattr(power, '_powercfg', fake)
    plan = power.set_plan('high performance')
    assert state['active'] == HIGH and plan.guid == HIGH and plan.active


def test_a_failed_activation_is_an_error(monkeypatch):
    def fake(*args):
        if args == ('/list',):
            return LIST_OUTPUT
        raise power.PowerError('The power scheme could not be set.')
    monkeypatch.setattr(power, '_powercfg', fake)
    with pytest.raises(power.PowerError):
        power.set_plan('balanced')


def test_the_process_runner_only_ever_receives_fixed_arguments(monkeypatch):
    seen = []

    class Done:
        returncode, stdout, stderr = 0, LIST_OUTPUT, ''
    monkeypatch.setattr(power.subprocess, 'run', lambda cmd, **kw: seen.append((cmd, kw)) or Done())
    power.list_plans()
    command, options = seen[0]
    assert command[0].casefold().endswith('powercfg.exe') or command[0].casefold() == 'powercfg'
    assert command[1:] == ['/list']
    assert options['timeout'] <= 10 and options['shell'] is False if 'shell' in options else True


@pytest.mark.integration
def test_real_power_plans_can_be_listed_without_changing_anything():
    plans = power.list_plans()
    assert plans and sum(1 for plan in plans if plan.active) == 1
    assert power.active_plan() in plans

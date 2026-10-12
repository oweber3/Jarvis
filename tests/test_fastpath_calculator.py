"""Spoken sums and conversions are deterministic fast routes; anything unclear falls through to the model."""
import json
from pathlib import Path

import pytest

TOOLS = {'getTime', 'systemVolume', 'calculator', 'appControl', 'openPath', 'systemSettings'}


def route(text, tools=TOOLS):
    from jarvis.fastpath.matcher import match
    return match(text, 'en', available_tools=tools)


def locale():
    return json.loads((Path(__file__).resolve().parents[1] / 'src/jarvis/fastpath/phrases/en.json')
                      .read_text(encoding='utf-8'))


def evaluated(text):
    from jarvis.maths.expression import evaluate
    result = route(text)
    assert result is not None, text
    assert result.tool_name == 'calculator' and result.args['action'] == 'evaluate', result
    return evaluate(result.args['expression']).value


@pytest.mark.parametrize('text,expected', [
    ('What is 17 times 23?', 391), ("What's 15% of 80?", 12), ('whats 15 percent of 80', 12),
    ('Calculate 2 to the power of 10', 1024), ('what is five times six', 30), ('What is 100 divided by 8?', 12.5),
    ('whats 12 plus 30 minus 2', 40), ('What is 9 squared', 81), ('what is 2.5 times 4', 10),
    ('Work out 17 mod 5', 2), ('what is 2+2', 4), ('what is 10-3', 7), ('What is 7 multiplied by 6?', 42),
])
def test_spoken_arithmetic_is_evaluated_exactly(text, expected):
    assert evaluated(text) == pytest.approx(expected)


@pytest.mark.parametrize('text', [
    'What is 17', 'what is 17 times', 'what is the volume', 'What is love?', 'what is 1/2 of 10', 'what is x times y',
    'what is 17 times 23 and 4 plus 4', 'What is the volume of a sphere?', 'what is 15 of 80', 'what is of 80',
    'What is 2 times the speed of light', 'times 5', 'what is 3 plus plus 4',
])
def test_unclear_arithmetic_falls_through(text):
    result = route(text)
    assert result is None or result.tool_name != 'calculator', (text, result)


@pytest.mark.parametrize('text,args', [
    ('Convert 5 miles to km', {'value': 5, 'from_unit': 'mi', 'to_unit': 'km'}),
    ('How many cups in a litre?', {'value': 1, 'from_unit': 'L', 'to_unit': 'cup'}),
    ('convert 72 Fahrenheit to Celsius', {'value': 72, 'from_unit': 'degF', 'to_unit': 'degC'}),
    ('What is 10 kg in pounds?', {'value': 10, 'from_unit': 'kg', 'to_unit': 'lb'}),
    ('how many ounces are in a pound', {'value': 1, 'from_unit': 'lb', 'to_unit': 'oz'}),
    ('Convert 2.5 kilometres into miles', {'value': 2.5, 'from_unit': 'km', 'to_unit': 'mi'}),
    ('How many seconds in a day', {'value': 1, 'from_unit': 'd', 'to_unit': 's'}),
    ('how many megabytes in a gigabyte', {'value': 1, 'from_unit': 'GB', 'to_unit': 'MB'}),
    ('Convert 100 degrees Celsius to Fahrenheit', {'value': 100, 'from_unit': 'degC', 'to_unit': 'degF'}),
    ('convert three feet to metres', {'value': 3, 'from_unit': 'ft', 'to_unit': 'm'}),
    ('convert -40 celsius to fahrenheit', {'value': -40, 'from_unit': 'degC', 'to_unit': 'degF'}),
])
def test_spoken_conversions_name_the_units_by_symbol(text, args):
    result = route(text)
    assert result is not None, text
    assert (result.tool_name, result.args) == ('calculator', {'action': 'convert', **args})


@pytest.mark.parametrize('text', [
    'convert 5 smoots to km', 'convert 5 miles to kilograms', 'convert 10 dollars to euros',
    'how many cups in a litre and a half', 'convert miles to km', 'convert 5 miles to km and back',
    'how many people in a team',
])
def test_unknown_mismatched_or_compound_conversions_fall_through(text):
    result = route(text)
    assert result is None or result.tool_name != 'calculator', (text, result)


def test_nothing_routes_to_the_calculator_when_it_is_not_registered():
    assert route('what is 17 times 23', tools=TOOLS - {'calculator'}) is None


def test_bare_sums_and_conversions_need_the_wake_word():
    bare = route('17 times 23')
    assert bare is not None and bare.needs_wake_word
    bare = route('5 miles in km')
    assert bare is not None and bare.needs_wake_word
    assert not route('what is 17 times 23').needs_wake_word


def test_every_locale_unit_is_known_to_the_calculator():
    from jarvis.maths.units import find_unit
    for name, symbol in locale()['units'].items():
        assert find_unit(symbol).symbol == symbol, name


def test_every_locale_operator_makes_a_valid_expression():
    from jarvis.maths.expression import validate
    for word, symbol in locale()['arithmetic']['operators'].items():
        validate(f'3 {symbol}' if any(c.isdigit() for c in symbol) else f'3 {symbol} 2')


def test_fast_route_runs_the_calculator_through_the_dispatcher(mock_config):
    from jarvis.fastpath.dispatcher import dispatch, match_command
    found = match_command('What is 17 times 23?', mock_config)
    assert found is not None and found.tool_name == 'calculator'
    assert dispatch(found, None, mock_config, 'What is 17 times 23?', quiet=True) == '17 × 23 = 391'

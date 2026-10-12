"""Unit conversion: exact factors, affine temperatures, honest refusals."""
import pytest

from jarvis.maths import MathsError
from jarvis.maths.units import CATEGORIES, UNITS, convert, find_unit


@pytest.mark.parametrize('value,source,target,expected', [
    (5, 'mi', 'km', 8.04672), (1, 'in', 'cm', 2.54), (3, 'ft', 'm', 0.9144), (1, 'nmi', 'm', 1852),
    (1, 'kg', 'lb', 2.20462262185), (1, 'st', 'lb', 14), (16, 'oz', 'lb', 1), (1, 't', 'kg', 1000),
    (1, 'L', 'cup', 4.22675283773), (1, 'gal', 'L', 3.785411784), (1, 'imp_gal', 'L', 4.54609),
    (1, 'imp_pt', 'mL', 568.26125), (1, 'tbsp', 'tsp', 3), (1, 'm3', 'L', 1000),
    (100, 'km/h', 'mph', 62.1371192237), (1, 'kn', 'km/h', 1.852), (10, 'm/s', 'km/h', 36),
    (1, 'ha', 'm2', 10000), (1, 'acre', 'ft2', 43560), (1, 'km2', 'ha', 100),
    (90, 'min', 'h', 1.5), (1, 'wk', 'd', 7), (1, 'd', 's', 86400), (1, 'yr', 'd', 365.25),
    (1, 'GB', 'MB', 1000), (1, 'GiB', 'MiB', 1024), (1, 'B', 'bit', 8), (100, 'Mbit', 'MB', 12.5),
    (1, 'kWh', 'MJ', 3.6), (1, 'kcal', 'kJ', 4.184), (1, 'Wh', 'J', 3600),
    (1, 'hp', 'W', 745.69987158227), (1, 'atm', 'kPa', 101.325), (1, 'bar', 'psi', 14.5037737730),
])
def test_conversions_use_exact_factors(value, source, target, expected):
    assert convert(value, source, target).result == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize('value,source,target,expected', [
    (72, 'degF', 'degC', 22.2222222222), (100, 'degC', 'degF', 212), (0, 'degC', 'K', 273.15),
    (-40, 'degF', 'degC', -40), (0, 'K', 'degF', -459.67), (491.67, 'degR', 'degC', 0),
])
def test_temperatures_convert_with_their_offsets(value, source, target, expected):
    assert convert(value, source, target).result == pytest.approx(expected, abs=1e-9)


def test_temperatures_below_absolute_zero_are_refused():
    with pytest.raises(MathsError):
        convert(-300, 'degC', 'K')


@pytest.mark.parametrize('name,symbol', [
    ('km', 'km'), ('kilometre', 'km'), ('Kilometers', 'km'), ('miles', 'mi'), ('°F', 'degF'), ('fahrenheit', 'degF'),
    ('degrees Celsius', 'degC'), ('°C', 'degC'), ('litre', 'L'), ('liters', 'L'), ('l', 'L'), ('cups', 'cup'),
    ('square metres', 'm2'), ('m²', 'm2'), ('m^2', 'm2'), ('km/h', 'km/h'), ('kph', 'km/h'), ('MB', 'MB'),
    ('Mb', 'Mbit'), ('megabytes', 'MB'), ('fl oz', 'floz'), ('fluid ounces', 'floz'), ('kilowatt hours', 'kWh'),
    ('µm', 'um'), ('pounds', 'lb'), ('lbs', 'lb'), ('hours', 'h'), ('mph', 'mph'),
])
def test_units_are_found_by_symbol_or_canonical_name(name, symbol):
    assert find_unit(name).symbol == symbol


def test_case_decides_between_units_that_differ_only_in_case():
    assert find_unit('B').symbol == 'B' and find_unit('b').symbol == 'bit'
    assert find_unit('Cal').symbol == 'kcal' and find_unit('cal').symbol == 'cal'
    with pytest.raises(MathsError) as error:
        find_unit('mb')
    assert 'MB' in str(error.value) and 'Mbit' in str(error.value)


def test_unknown_units_are_refused_with_a_clear_message():
    with pytest.raises(MathsError) as error:
        find_unit('smoots')
    assert 'smoots' in str(error.value)


def test_units_of_different_kinds_do_not_convert():
    with pytest.raises(MathsError) as error:
        convert(1, 'kg', 'L')
    assert 'mass' in str(error.value) and 'volume' in str(error.value)


@pytest.mark.parametrize('source,target', [('USD', 'EUR'), ('GBP', 'usd'), ('$', '£'), ('dollars', 'euros')])
def test_currency_is_refused_honestly_because_rates_need_the_network(source, target):
    with pytest.raises(MathsError) as error:
        convert(10, source, target)
    assert 'currenc' in str(error.value).casefold()


def test_every_category_named_in_the_spec_has_units():
    for category in ('length', 'mass', 'volume', 'temperature', 'speed', 'area', 'time', 'data', 'energy'):
        assert category in CATEGORIES
        assert sum(unit.category == category for unit in UNITS) >= 3


def test_every_unit_converts_to_itself():
    for unit in UNITS:
        assert convert(7, unit.symbol, unit.symbol).result == pytest.approx(7)


def test_a_conversion_reports_readable_unit_names():
    conversion = convert(1, 'L', 'cup')
    assert conversion.text == '1 litre = 4.22675 US cups'
    assert convert(5, 'mi', 'km').text == '5 miles = 8.04672 kilometres'
    assert convert(72, 'degF', 'degC').text == '72 degrees Fahrenheit = 22.2222 degrees Celsius'


def test_non_finite_values_are_refused():
    for bad in (float('inf'), float('nan')):
        with pytest.raises(MathsError):
            convert(bad, 'm', 'km')

"""Safe arithmetic: exact results for what small models get wrong, and refusal of anything that is not maths."""
import math
import time

import pytest

from jarvis.maths import MathsError
from jarvis.maths.expression import evaluate, validate


def value(expression, **kwargs):
    return evaluate(expression, **kwargs).value


@pytest.mark.parametrize('expression,expected', [
    ('17 * 23', 391), ('2 + 3 * 4', 14), ('(2 + 3) * 4', 20), ('7 / 2', 3.5), ('7 // 2', 3), ('17 % 5', 2),
    ('2 ** 10', 1024), ('2 ^ 10', 1024), ('-3 + 5', 2), ('+4', 4), ('17 × 23', 391), ('10 ÷ 4', 2.5),
    ('10 − 4', 6), ('1.5 * 4', 6), ('.5 + .25', 0.75),
])
def test_arithmetic_follows_the_usual_rules(expression, expected):
    assert value(expression) == expected


def test_integer_arithmetic_stays_exact():
    assert value('123456789 * 987654321') == 121932631112635269
    assert value('2 ** 100') == 1267650600228229401496703205376


@pytest.mark.parametrize('expression,expected', [
    ('15% * 80', 12), ('15 % * 80', 12), ('50%', 0.5), ('80 * 15%', 12), ('200 / 50%', 400), ('12.5%', 0.125),
])
def test_a_percent_sign_without_an_operand_after_it_is_a_percentage(expression, expected):
    assert value(expression) == pytest.approx(expected)


@pytest.mark.parametrize('expression,expected', [
    ('sqrt(144)', 12), ('cbrt(27)', 3), ('abs(-4)', 4), ('round(2.567, 2)', 2.57), ('floor(2.7)', 2),
    ('ceil(2.1)', 3), ('factorial(5)', 120), ('log(1000)', 3), ('log(8, 2)', 3), ('ln(e)', 1), ('log2(8)', 3),
    ('log10(100)', 2), ('exp(0)', 1), ('min(3, 1, 2)', 1), ('max(3, 1, 2)', 3), ('hypot(3, 4)', 5),
    ('gcd(12, 18)', 6), ('lcm(4, 6)', 12), ('pi', math.pi), ('tau', math.tau), ('2 * pi', 2 * math.pi),
])
def test_common_functions_and_constants(expression, expected):
    assert value(expression) == pytest.approx(expected)


def test_trigonometry_uses_radians_unless_degrees_are_asked_for():
    assert value('sin(pi / 2)') == pytest.approx(1)
    assert value('cos(pi)') == pytest.approx(-1)
    assert value('sin(30)', degrees=True) == pytest.approx(0.5)
    assert value('tan(45)', degrees=True) == pytest.approx(1)
    assert value('asin(1)', degrees=True) == pytest.approx(90)


def test_values_that_should_be_zero_are_zero():
    assert value('sin(pi)') == 0
    assert value('cos(90)', degrees=True) == 0
    assert evaluate('0.1 + 0.2').result == '0.3'


@pytest.mark.parametrize('expression,shown', [
    ('17 * 23', '17 × 23'), ('10 / 4', '10 ÷ 4'), ('15% * 80', '15% × 80'), ('(2 + 3) * 4', '(2 + 3) × 4'),
    ('sqrt(144)', 'sqrt(144)'), ('2 - (3 - 1)', '2 − (3 − 1)'),
])
def test_the_expression_is_shown_with_readable_symbols(expression, shown):
    assert evaluate(expression).expression == shown


@pytest.mark.parametrize('expression,result', [
    ('6 / 3', '2'), ('1 / 3', '0.333333333333'), ('2 ** 0.5', '1.41421356237'), ('10 ** 40', '1e+40'),
])
def test_results_are_formatted_without_float_noise(expression, result):
    assert evaluate(expression).result == result


@pytest.mark.parametrize('expression', [
    '__import__("os")', 'open("x")', 'x + 1', '"a" * 3', '[1, 2]', '(1, 2)', '1 if 1 else 2', 'lambda: 1',
    '1 < 2', 'True + 1', 'pi.real', 'sqrt(x=4)', 'sqrt(*[4])', '1j * 2', 'a.b()', '', '   ', '2 +', '1,000',
    'eval("1")', 'exec("1")', 'globals()', '().__class__',
])
def test_anything_that_is_not_arithmetic_is_refused(expression):
    with pytest.raises(MathsError):
        evaluate(expression)


@pytest.mark.parametrize('expression', [
    '9 ** 9 ** 9', '10 ** 100000', 'factorial(100000)', '2 ** 2 ** 2 ** 2 ** 2 ** 2', '1.5 ** 100000',
    '(10 ** 3000) * (10 ** 3000) * (10 ** 3000) * (10 ** 3000)', '1' * 2000,
])
def test_huge_work_is_refused_quickly(expression):
    started = time.perf_counter()
    with pytest.raises(MathsError):
        evaluate(expression)
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize('expression', ['1 / 0', '5 // 0', '5 % 0', 'sqrt(-1)', 'log(0)', 'factorial(-1)',
                                        'factorial(2.5)', 'asin(2)'])
def test_undefined_results_are_errors_not_crashes(expression):
    with pytest.raises(MathsError):
        evaluate(expression)


def test_validate_accepts_arithmetic_and_rejects_the_rest_without_evaluating():
    validate('1 / 0')  # well formed, even though it cannot be evaluated
    with pytest.raises(MathsError):
        validate('2 3')
    with pytest.raises(MathsError):
        validate('import os')

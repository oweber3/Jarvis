"""calculator: exact results through the central executor, honest failures, always registered and SAFE."""
import pytest


def run(cfg, **args):
    from jarvis.tools.registry import run_tool_with_retries
    return run_tool_with_retries(db=None, cfg=cfg, tool_name='calculator', tool_args=args, system_prompt='',
                                 original_prompt='', redacted_text='', max_retries=1, quiet=True)


def test_the_calculator_is_always_offered():
    from jarvis.tools.registry import BUILTIN_TOOLS
    assert 'calculator' in BUILTIN_TOOLS


def test_router_description_names_maths_and_conversion_up_front():
    from jarvis.tools.registry import BUILTIN_TOOLS
    head = BUILTIN_TOOLS['calculator'].description[:120].casefold()
    assert 'arithmetic' in head and 'unit' in head


def test_evaluate_returns_the_exact_result(mock_config):
    result = run(mock_config, action='evaluate', expression='17 * 23')
    assert result.success and result.reply_text == '17 × 23 = 391'


def test_evaluate_is_the_default_action_when_an_expression_is_given(mock_config):
    result = run(mock_config, expression='15% * 80')
    assert result.success and result.reply_text.endswith('= 12')


def test_evaluate_in_degrees(mock_config):
    result = run(mock_config, action='evaluate', expression='sin(30)', angle_unit='degrees')
    assert result.success and result.reply_text.endswith('= 0.5')


def test_convert_returns_the_converted_value(mock_config):
    result = run(mock_config, action='convert', value=72, from_unit='°F', to_unit='°C')
    assert result.success and result.reply_text == '72 degrees Fahrenheit = 22.2222 degrees Celsius'


def test_convert_is_the_default_action_when_units_are_given(mock_config):
    result = run(mock_config, value='1', from_unit='litre', to_unit='cups')
    assert result.success and '4.22675' in result.reply_text


def test_convert_defaults_the_value_to_one(mock_config):
    result = run(mock_config, action='convert', from_unit='mi', to_unit='km')
    assert result.success and result.reply_text.startswith('1 mile =')


@pytest.mark.parametrize('args', [
    {'action': 'evaluate', 'expression': '__import__("os").system("calc")'},
    {'action': 'evaluate', 'expression': '1 / 0'},
    {'action': 'evaluate'},
    {'action': 'convert', 'value': 1, 'from_unit': 'kg', 'to_unit': 'L'},
    {'action': 'convert', 'value': 'lots', 'from_unit': 'kg', 'to_unit': 'lb'},
    {'action': 'convert', 'value': 1, 'from_unit': 'kg'},
    {'action': 'divine'},
])
def test_failures_are_honest_and_never_raise(mock_config, args):
    result = run(mock_config, **args)
    assert not result.success and result.reply_text


def test_currency_requests_say_why_they_cannot_be_done(mock_config):
    result = run(mock_config, action='convert', value=10, from_unit='USD', to_unit='EUR')
    assert not result.success and 'currenc' in result.reply_text.casefold()


def test_every_call_is_safe(mock_config):
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    from jarvis.tools.registry import BUILTIN_TOOLS
    tool = BUILTIN_TOOLS['calculator']
    for args in ({'action': 'evaluate', 'expression': '1+1'}, {'action': 'convert', 'from_unit': 'm', 'to_unit': 'ft'}):
        assert evaluate_safety('calculator', args, mock_config, tool=tool).tier == SafetyTier.SAFE

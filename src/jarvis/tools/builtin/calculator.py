"""calculator: exact arithmetic and unit conversion, so small models never do sums in their heads.

A thin layer over ``jarvis.maths`` (maths.spec.md). Results are raw data ("17 × 23 = 391"); the reply model
phrases them. Everything is local and pure, so every call is SAFE.
"""
from typing import Any, Dict, Optional

from ...debug import debug_log
from ...maths import MathsError
from ...maths.expression import evaluate
from ...maths.units import CATEGORIES, convert
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


def _number(value):
    if value is None or value == '':
        return 1
    if isinstance(value, bool):
        raise MathsError('The value to convert must be a number.')
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip().replace(',', '')
        return int(text) if text.lstrip('+-').isdigit() else float(text)
    except ValueError:
        raise MathsError('The value to convert must be a number.') from None


class CalculatorTool(Tool):
    @property
    def name(self) -> str:
        return 'calculator'

    @property
    def description(self) -> str:
        return ('Exact arithmetic and unit conversion. Use for ANY sum, percentage or maths expression and for '
                'converting units (' + ', '.join(CATEGORIES) + '). Always use it instead of working numbers '
                'out yourself. NOT for currency, which needs live exchange rates.')

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string', 'enum': ['evaluate', 'convert'],
                    'description': 'evaluate calculates an expression; convert changes a value from one unit '
                                   'to another.',
                },
                'expression': {
                    'type': 'string',
                    'description': 'For evaluate. Digits and + - * / // % ^ ( ), e.g. "17 * 23", "15% * 80" '
                                   '(a % with nothing after it is a percentage; between numbers it is the '
                                   'remainder), "sqrt(2) * 3". Functions: sqrt cbrt abs round floor ceil '
                                   'factorial min max log (base 10, or log(x, base)) ln log2 exp sin cos tan '
                                   'asin acos atan hypot gcd lcm. Constants: pi e tau.',
                },
                'angle_unit': {
                    'type': 'string', 'enum': ['radians', 'degrees'],
                    'description': 'OPTIONAL, for evaluate. Angle unit for sin, cos, tan and their inverses; '
                                   'default radians.',
                },
                'value': {'type': 'number', 'description': 'For convert. The amount to convert; default 1.'},
                'from_unit': {
                    'type': 'string',
                    'description': 'For convert. Unit symbol or English unit name, e.g. km, mi, lb, kg, L, cup, '
                                   'floz, degF, degC, K, km/h, mph, m2, acre, h, GB, GiB, kWh, kcal.',
                },
                'to_unit': {'type': 'string', 'description': 'For convert. The unit to convert to, as from_unit.'},
            },
            'required': [],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args if isinstance(args, dict) else {}
        action = args.get('action') or ('convert' if args.get('from_unit') or args.get('to_unit') else 'evaluate')
        context.user_print('🧮 Calculating...')
        try:
            if action == 'evaluate':
                outcome = evaluate(args.get('expression') or '',
                                   degrees=str(args.get('angle_unit', '')).casefold() == 'degrees')
                text = outcome.text
            elif action == 'convert':
                text = convert(_number(args.get('value')), args.get('from_unit'), args.get('to_unit')).text
            else:
                raise MathsError('Unknown action: use evaluate or convert.')
        except MathsError as exc:
            debug_log(f'calculator {str(action)[:10]}: refused ({len(str(exc))} chars)', 'tools')
            context.user_print(f'   ⚠️ {exc}')
            return ToolExecutionResult(success=False, reply_text=f'Could not calculate: {exc}')
        debug_log(f'calculator {action}: done', 'tools')
        context.user_print(f'   ✅ {text}')
        return ToolExecutionResult(success=True, reply_text=text)

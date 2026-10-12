"""Safe arithmetic over a whitelisted Python expression syntax tree (maths.spec.md, Expressions).

The text is parsed with ``ast.parse`` and walked by hand: only numbers, the named constants and functions
below, and the arithmetic operators are accepted. Nothing is ever passed to ``eval`` or ``exec``. Size
limits keep any expression cheap: its length, its node count, the bit length of every integer and the
argument of ``factorial``.
"""
import ast
import math
import operator
import re
from dataclasses import dataclass

from . import MathsError, format_number

MAX_LENGTH = 300
MAX_NODES = 120
# Below Python's 4300-digit limit for turning an integer into text.
MAX_INT_BITS = 13000
MAX_FACTORIAL = 1000
SIGNIFICANT = 12

# A percent sign with no operand after it is a percentage ("15% * 80"); followed by an operand it is the
# remainder operator ("17 % 5").
_PERCENT = re.compile(r'(\d+(?:\.\d*)?|\.\d+)\s*%(?!\s*[\w.(])')
_PERCENT_CALL = '_percent'
_SYMBOLS = {'×': '*', '÷': '/', '−': '-', '^': '**', '·': '*'}

CONSTANTS = {'pi': math.pi, 'e': math.e, 'tau': math.tau}


def _integral(value, name):
    if isinstance(value, float):
        if not value.is_integer():
            raise MathsError(f'{name} needs whole numbers.')
        value = int(value)
    return value


def _factorial(value):
    value = _integral(value, 'factorial')
    if not 0 <= value <= MAX_FACTORIAL:
        raise MathsError(f'factorial works for whole numbers from 0 to {MAX_FACTORIAL}.')
    return math.factorial(value)


def _log(value, base=10):
    return math.log10(value) if base == 10 else math.log(value, base)


def _round(value, digits=0):
    digits = _integral(digits, 'round')
    return round(value) if digits == 0 else round(value, digits)


def _snap(value):
    """Trigonometry results that are zero up to float error are zero."""
    return 0.0 if abs(value) < 1e-12 else value


def _functions(degrees: bool) -> dict:
    to_radians = math.radians if degrees else (lambda x: x)
    from_radians = math.degrees if degrees else (lambda x: x)
    return {
        'sqrt': math.sqrt, 'cbrt': math.cbrt, 'exp': math.exp, 'ln': math.log, 'log': _log,
        'log10': math.log10, 'log2': math.log2, 'abs': abs, 'round': _round, 'floor': math.floor,
        'ceil': math.ceil, 'trunc': math.trunc, 'factorial': _factorial, 'min': min, 'max': max,
        'hypot': math.hypot, 'degrees': math.degrees, 'radians': math.radians,
        'gcd': lambda *v: math.gcd(*(_integral(x, 'gcd') for x in v)),
        'lcm': lambda *v: math.lcm(*(_integral(x, 'lcm') for x in v)),
        'sin': lambda x: _snap(math.sin(to_radians(x))), 'cos': lambda x: _snap(math.cos(to_radians(x))),
        'tan': lambda x: _snap(math.tan(to_radians(x))),
        'asin': lambda x: from_radians(math.asin(x)), 'acos': lambda x: from_radians(math.acos(x)),
        'atan': lambda x: from_radians(math.atan(x)), 'atan2': lambda y, x: from_radians(math.atan2(y, x)),
        'sinh': math.sinh, 'cosh': math.cosh, 'tanh': math.tanh,
        _PERCENT_CALL: lambda x: x / 100,
    }


FUNCTION_NAMES = frozenset(_functions(False)) - {_PERCENT_CALL}

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_SHOWN = {ast.Add: '+', ast.Sub: '−', ast.Mult: '×', ast.Div: '÷', ast.FloorDiv: '//', ast.Mod: 'mod',
          ast.Pow: '^'}
_PRECEDENCE = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2, ast.FloorDiv: 2, ast.Mod: 2, ast.Pow: 4}


@dataclass(frozen=True)
class Evaluation:
    expression: str  # the expression as parsed, with readable symbols
    value: int | float
    result: str  # the value as short text

    @property
    def text(self) -> str:
        return f'{self.expression} = {self.result}'


def _parse(text: str) -> ast.Expression:
    if not isinstance(text, str) or not text.strip():
        raise MathsError('There is no expression to calculate.')
    if len(text) > MAX_LENGTH:
        raise MathsError(f'The expression is longer than {MAX_LENGTH} characters.')
    for symbol, replacement in _SYMBOLS.items():
        text = text.replace(symbol, replacement)
    text = _PERCENT.sub(lambda m: f'{_PERCENT_CALL}({m.group(1)})', text)
    try:
        tree = ast.parse(text.strip(), mode='eval')
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise MathsError('That is not a valid arithmetic expression.') from None
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise MathsError('The expression is too long to calculate.')
    for node in nodes:
        _check(node)
    return tree


def _check(node) -> None:
    if isinstance(node, (ast.Expression, ast.Load, ast.UnaryOp, ast.UAdd, ast.USub)) or type(node) in _BINARY:
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINARY:
            raise MathsError('Only + − × ÷ // % and ^ are supported.')
        return
    if isinstance(node, ast.Constant):
        if type(node.value) not in (int, float):
            raise MathsError('Only numbers can be calculated.')
        return
    if isinstance(node, ast.Name):
        if node.id not in CONSTANTS and node.id not in FUNCTION_NAMES and node.id != _PERCENT_CALL:
            raise MathsError(f'Unknown name "{node.id[:20]}": only numbers, pi, e, tau and maths functions '
                             'are supported.')
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id in CONSTANTS:
            raise MathsError('Only the supported maths functions can be called.')
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise MathsError('Functions take plain numbers in order.')
        return
    raise MathsError('That is not a valid arithmetic expression.')


def validate(expression: str) -> None:
    """Raise MathsError unless ``expression`` is well-formed arithmetic; nothing is calculated."""
    _parse(expression)


def _check_size(value):
    if isinstance(value, complex):
        raise MathsError('The result is not a real number.')
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise MathsError('The result is too large to calculate.')
    if isinstance(value, float) and not math.isfinite(value):
        raise MathsError('The result is too large to calculate.')
    return value


def _binary(op, left, right):
    if op in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
        raise MathsError('Division by zero is undefined.')
    if op is ast.Mult and isinstance(left, int) and isinstance(right, int) \
            and left.bit_length() + right.bit_length() > MAX_INT_BITS + 1:
        raise MathsError('The result is too large to calculate.')
    if op is ast.Pow and isinstance(left, int) and isinstance(right, int) and right > 0 \
            and abs(left) > 1 and (abs(left).bit_length() - 1) * right > MAX_INT_BITS:
        raise MathsError('The result is too large to calculate.')
    if op is ast.Div and isinstance(left, int) and isinstance(right, int) and left % right == 0:
        return left // right
    if op is ast.Pow and isinstance(left, float) | isinstance(right, float):
        if left < 0 and not float(right).is_integer():
            raise MathsError('The result is not a real number.')
        return math.pow(left, right)
    return _BINARY[op](left, right)


def _evaluate(node, functions):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, functions)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in CONSTANTS:
            raise MathsError(f'{node.id} is a function; give it a value in brackets.')
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, functions)
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, functions), _evaluate(node.right, functions)
        return _check_size(_binary(type(node.op), left, right))
    name = node.func.id
    args = [_evaluate(arg, functions) for arg in node.args]
    if name in ('min', 'max') and not args:
        raise MathsError(f'{name} needs at least one number.')
    try:
        return _check_size(functions[name](*args))
    except TypeError:
        raise MathsError(f'Wrong number of values for {name}.') from None


def evaluate(expression: str, *, degrees: bool = False) -> Evaluation:
    """Calculate ``expression`` exactly where integers allow. Trigonometry uses degrees when asked."""
    tree = _parse(expression)
    try:
        value = _evaluate(tree, _functions(degrees))
    except MathsError:
        raise
    except ZeroDivisionError:
        raise MathsError('Division by zero is undefined.') from None
    except OverflowError:
        raise MathsError('The result is too large to calculate.') from None
    except (ValueError, ArithmeticError):
        raise MathsError('The result is undefined for those values.') from None
    if isinstance(value, bool):
        value = int(value)
    return Evaluation(_show(tree.body), value, format_number(value, SIGNIFICANT))


def _show(node, parent: int = 0) -> str:
    """The expression with readable symbols, bracketed only where precedence needs it."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.UnaryOp):
        text = ('−' if isinstance(node.op, ast.USub) else '+') + _show(node.operand, 3)
        return f'({text})' if parent > 3 else text
    if isinstance(node, ast.Call):
        if node.func.id == _PERCENT_CALL:
            return _show(node.args[0], 5) + '%'
        return f'{node.func.id}({", ".join(_show(arg) for arg in node.args)})'
    op = type(node.op)
    level = _PRECEDENCE[op]
    if op is ast.Pow:
        left, right = _show(node.left, level + 1), _show(node.right, level)
    else:
        left, right = _show(node.left, level), _show(node.right, level + 1)
    text = f'{left}{_SHOWN[op]}{right}' if op is ast.Pow else f'{left} {_SHOWN[op]} {right}'
    return f'({text})' if level < parent else text

"""Exact arithmetic and unit conversion, with no model, no network and no ``eval`` (maths.spec.md)."""
import math


class MathsError(ValueError):
    """An expression or conversion that cannot be done; the message is safe to show the user."""


def format_number(value, significant: int) -> str:
    """A number as short readable text: integers in full (scientific beyond 30 digits), floats rounded to
    ``significant`` digits without float noise, positional unless very large or very small."""
    if isinstance(value, int):
        text = str(abs(value))
        if len(text) <= 30:
            return str(value)
        mantissa = f'{text[0]}.{text[1:significant]}'.rstrip('0').rstrip('.')
        return f'{"-" if value < 0 else ""}{mantissa}e+{len(text) - 1}'
    if not math.isfinite(value):
        raise MathsError('The result is not a finite number.')
    if value == 0:
        return '0'
    rounded = float(f'{value:.{significant}g}')
    magnitude = math.floor(math.log10(abs(rounded)))
    if rounded.is_integer() and abs(rounded) < 1e15:
        return str(int(rounded))
    if -6 <= magnitude < 15:
        decimals = max(0, significant - 1 - magnitude)
        return f'{rounded:.{decimals}f}'.rstrip('0').rstrip('.')
    return f'{rounded:.{significant}g}'

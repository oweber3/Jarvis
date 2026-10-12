"""Unit conversion by exact factors (maths.spec.md, Units).

Every unit has a canonical symbol, a category and its size in the category's base unit (temperatures also
carry an offset to kelvin). Units are found by symbol or canonical name: an exact, case-sensitive match
first, then a case-folded match without spaces and separators when that names one unit only. These are
unit identifiers, not language: phrases a person says are mapped to symbols by the fast-path locale files
or by the model.
"""
import math
import unicodedata
from dataclasses import dataclass

from . import MathsError, format_number

SIGNIFICANT = 6


@dataclass(frozen=True)
class Unit:
    symbol: str
    category: str
    factor: float  # size in the category's base unit
    singular: str
    plural: str
    aliases: tuple = ()
    offset: float = 0.0  # temperatures only: kelvin = value * factor + offset


def _u(symbol, category, factor, singular, plural=None, *aliases, offset=0.0):
    return Unit(symbol, category, factor, singular, plural or singular + 's', tuple(aliases), offset)


_INCH = 0.0254
_FOOT = 0.3048
_MILE = 1609.344
_US_GALLON = 3.785411784e-3
_IMP_GALLON = 4.54609e-3

UNITS = (
    # length, base metre
    _u('m', 'length', 1, 'metre', None, 'meters'), _u('km', 'length', 1e3, 'kilometre', None, 'kms'),
    _u('cm', 'length', 1e-2, 'centimetre'), _u('mm', 'length', 1e-3, 'millimetre'),
    _u('um', 'length', 1e-6, 'micrometre', None, 'µm', 'micron', 'microns'), _u('nm', 'length', 1e-9, 'nanometre'),
    _u('mi', 'length', _MILE, 'mile'), _u('yd', 'length', 0.9144, 'yard', None, 'yds'),
    _u('ft', 'length', _FOOT, 'foot', 'feet'), _u('in', 'length', _INCH, 'inch', 'inches'),
    _u('nmi', 'length', 1852, 'nautical mile'), _u('au', 'length', 149597870700, 'astronomical unit'),
    _u('ly', 'length', 9460730472580800, 'light year', None, 'light-year', 'light-years', 'lightyear'),
    # mass, base kilogram
    _u('kg', 'mass', 1, 'kilogram', None, 'kilo', 'kilos', 'kgs'), _u('g', 'mass', 1e-3, 'gram', None, 'grams'),
    _u('mg', 'mass', 1e-6, 'milligram'), _u('ug', 'mass', 1e-9, 'microgram', None, 'µg', 'mcg'),
    _u('t', 'mass', 1e3, 'tonne', None, 'metric ton', 'metric tons'),
    _u('lb', 'mass', 0.45359237, 'pound', None, 'lbs'), _u('oz', 'mass', 0.45359237 / 16, 'ounce'),
    _u('st', 'mass', 0.45359237 * 14, 'stone', 'stone', 'stones'),
    _u('ton', 'mass', 907.18474, 'US short ton', None, 'short ton', 'short tons', 'tons', 'short_ton'),
    _u('long_ton', 'mass', 1016.0469088, 'imperial long ton', None, 'long ton', 'long tons'),
    _u('ct', 'mass', 2e-4, 'carat'),
    # volume, base cubic metre
    _u('m3', 'volume', 1, 'cubic metre'), _u('L', 'volume', 1e-3, 'litre', None, 'l', 'ltr'),
    _u('mL', 'volume', 1e-6, 'millilitre', None, 'ml', 'cm3', 'cc', 'cubic centimetre', 'cubic centimetres'),
    _u('cL', 'volume', 1e-5, 'centilitre', None, 'cl'), _u('dL', 'volume', 1e-4, 'decilitre', None, 'dl'),
    _u('ft3', 'volume', _FOOT ** 3, 'cubic foot', 'cubic feet'),
    _u('in3', 'volume', _INCH ** 3, 'cubic inch', 'cubic inches'),
    _u('gal', 'volume', _US_GALLON, 'US gallon', None, 'gallon', 'gallons'),
    _u('qt', 'volume', _US_GALLON / 4, 'US quart', None, 'quart', 'quarts'),
    _u('pt', 'volume', _US_GALLON / 8, 'US pint', None, 'pint', 'pints'),
    _u('cup', 'volume', _US_GALLON / 16, 'US cup', None, 'cup', 'cups'),
    _u('floz', 'volume', _US_GALLON / 128, 'US fluid ounce', None, 'fl oz', 'fluid ounce', 'fluid ounces'),
    _u('tbsp', 'volume', _US_GALLON / 256, 'US tablespoon', None, 'tablespoon', 'tablespoons'),
    _u('tsp', 'volume', _US_GALLON / 768, 'US teaspoon', None, 'teaspoon', 'teaspoons'),
    _u('imp_gal', 'volume', _IMP_GALLON, 'imperial gallon'),
    _u('imp_qt', 'volume', _IMP_GALLON / 4, 'imperial quart'),
    _u('imp_pt', 'volume', _IMP_GALLON / 8, 'imperial pint'),
    _u('imp_floz', 'volume', _IMP_GALLON / 160, 'imperial fluid ounce', None, 'imp fl oz'),
    _u('metric_cup', 'volume', 2.5e-4, 'metric cup'),
    # temperature, base kelvin
    _u('degC', 'temperature', 1, 'degree Celsius', 'degrees Celsius', '°C', 'C', 'celsius', 'centigrade',
       'deg C', 'degrees C', offset=273.15),
    _u('degF', 'temperature', 5 / 9, 'degree Fahrenheit', 'degrees Fahrenheit', '°F', 'F', 'fahrenheit',
       'deg F', 'degrees F', offset=273.15 - 32 * 5 / 9),
    _u('K', 'temperature', 1, 'kelvin', 'kelvin', 'kelvins'),
    _u('degR', 'temperature', 5 / 9, 'degree Rankine', 'degrees Rankine', '°R', 'rankine'),
    # speed, base metre per second
    _u('m/s', 'speed', 1, 'metre per second', 'metres per second', 'mps'),
    _u('km/h', 'speed', 1 / 3.6, 'kilometre per hour', 'kilometres per hour', 'kph', 'kmh', 'km per hour'),
    _u('mph', 'speed', _MILE / 3600, 'mile per hour', 'miles per hour'),
    _u('kn', 'speed', 1852 / 3600, 'knot', None, 'kt', 'kts'),
    _u('ft/s', 'speed', _FOOT, 'foot per second', 'feet per second', 'fps'),
    # area, base square metre
    _u('m2', 'area', 1, 'square metre', None, 'sq m'), _u('km2', 'area', 1e6, 'square kilometre', None, 'sq km'),
    _u('cm2', 'area', 1e-4, 'square centimetre'), _u('mm2', 'area', 1e-6, 'square millimetre'),
    _u('ha', 'area', 1e4, 'hectare'), _u('acre', 'area', 4046.8564224, 'acre', None, 'ac'),
    _u('ft2', 'area', _FOOT ** 2, 'square foot', 'square feet', 'sq ft'),
    _u('yd2', 'area', 0.9144 ** 2, 'square yard', None, 'sq yd'),
    _u('in2', 'area', _INCH ** 2, 'square inch', 'square inches', 'sq in'),
    _u('mi2', 'area', _MILE ** 2, 'square mile', None, 'sq mi'),
    # time, base second
    _u('ns', 'time', 1e-9, 'nanosecond'), _u('us', 'time', 1e-6, 'microsecond', None, 'µs'),
    _u('ms', 'time', 1e-3, 'millisecond'), _u('s', 'time', 1, 'second', None, 'sec', 'secs'),
    _u('min', 'time', 60, 'minute', None, 'mins'), _u('h', 'time', 3600, 'hour', None, 'hr', 'hrs'),
    _u('d', 'time', 86400, 'day'), _u('wk', 'time', 604800, 'week', None, 'weeks'),
    _u('mo', 'time', 86400 * 365.25 / 12, 'average month', None, 'month', 'months'),
    _u('yr', 'time', 86400 * 365.25, 'year', None, 'yrs'),
    # data, base byte
    _u('bit', 'data', 1 / 8, 'bit', None, 'b'), _u('B', 'data', 1, 'byte'),
    _u('kB', 'data', 1e3, 'kilobyte', None, 'KB'), _u('MB', 'data', 1e6, 'megabyte'),
    _u('GB', 'data', 1e9, 'gigabyte'), _u('TB', 'data', 1e12, 'terabyte'), _u('PB', 'data', 1e15, 'petabyte'),
    _u('KiB', 'data', 2 ** 10, 'kibibyte'), _u('MiB', 'data', 2 ** 20, 'mebibyte'),
    _u('GiB', 'data', 2 ** 30, 'gibibyte'), _u('TiB', 'data', 2 ** 40, 'tebibyte'),
    _u('PiB', 'data', 2 ** 50, 'pebibyte'),
    _u('kbit', 'data', 125, 'kilobit', None, 'Kb'), _u('Mbit', 'data', 1.25e5, 'megabit', None, 'Mb'),
    _u('Gbit', 'data', 1.25e8, 'gigabit', None, 'Gb'), _u('Tbit', 'data', 1.25e11, 'terabit', None, 'Tb'),
    # energy, base joule
    _u('J', 'energy', 1, 'joule'), _u('kJ', 'energy', 1e3, 'kilojoule'), _u('MJ', 'energy', 1e6, 'megajoule'),
    _u('cal', 'energy', 4.184, 'calorie'), _u('kcal', 'energy', 4184, 'kilocalorie', None, 'Cal'),
    _u('Wh', 'energy', 3600, 'watt hour'), _u('kWh', 'energy', 3.6e6, 'kilowatt hour'),
    _u('MWh', 'energy', 3.6e9, 'megawatt hour'), _u('BTU', 'energy', 1055.05585262, 'British thermal unit'),
    _u('eV', 'energy', 1.602176634e-19, 'electronvolt'),
    # power, base watt
    _u('W', 'power', 1, 'watt'), _u('kW', 'power', 1e3, 'kilowatt'), _u('MW', 'power', 1e6, 'megawatt'),
    _u('hp', 'power', 745.69987158227, 'horsepower', 'horsepower'),
    # pressure, base pascal
    _u('Pa', 'pressure', 1, 'pascal'), _u('hPa', 'pressure', 100, 'hectopascal'),
    _u('kPa', 'pressure', 1e3, 'kilopascal'), _u('MPa', 'pressure', 1e6, 'megapascal'),
    _u('bar', 'pressure', 1e5, 'bar'), _u('mbar', 'pressure', 100, 'millibar'),
    _u('atm', 'pressure', 101325, 'standard atmosphere'), _u('psi', 'pressure', 6894.757293168, 'psi', 'psi'),
    _u('mmHg', 'pressure', 133.322387415, 'millimetre of mercury', 'millimetres of mercury'),
    _u('inHg', 'pressure', 3386.389, 'inch of mercury', 'inches of mercury'),
)

CATEGORIES = tuple(dict.fromkeys(unit.category for unit in UNITS))

# Currency symbols and codes: recognised only to say honestly why they cannot be converted offline.
_CURRENCIES = frozenset({
    '$', '€', '£', '¥', '₹', '₩', '₽', '₺', '₿', 'usd', 'eur', 'gbp', 'jpy', 'cny', 'inr', 'aud', 'cad', 'chf',
    'sek', 'nok', 'dkk', 'pln', 'czk', 'huf', 'try', 'rub', 'brl', 'mxn', 'zar', 'krw', 'sgd', 'hkd', 'nzd',
    'btc', 'eth', 'dollar', 'dollars', 'euro', 'euros', 'yen', 'yuan', 'rupee', 'rupees', 'franc', 'francs',
    'peso', 'pesos', 'krona', 'kronor', 'krone', 'kroner', 'bitcoin', 'sterling',
})


def _clean(text: str) -> str:
    return unicodedata.normalize('NFKC', text).replace('μ', 'u').replace('µ', 'u').strip()


def _folded(text: str) -> str:
    text = _clean(text).casefold()
    for separator in (' ', '_', '-', '^', '.', '°'):
        text = text.replace(separator, '')
    return text


def _spellings(name: str):
    yield name
    yield name.replace('metre', 'meter').replace('litre', 'liter')


def _tables():
    exact, folded = {}, {}
    for unit in UNITS:
        names = {unit.symbol, *unit.aliases}
        for name in (unit.singular, unit.plural, *unit.aliases):
            names.update(_spellings(name))
        for name in names:
            exact.setdefault(_clean(name), unit)
            folded.setdefault(_folded(name), {})[unit.symbol] = unit
    return exact, folded


_EXACT, _FOLDED = _tables()
_BY_SYMBOL = {unit.symbol: unit for unit in UNITS}


def find_unit(name) -> Unit:
    """The unit called ``name`` (symbol or canonical name), or MathsError naming the problem."""
    if not isinstance(name, str) or not name.strip():
        raise MathsError('A unit is missing.')
    if name in _BY_SYMBOL:
        return _BY_SYMBOL[name]
    if _clean(name) in _EXACT:
        return _EXACT[_clean(name)]
    candidates = _FOLDED.get(_folded(name), {})
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    shown = name.strip()[:30]
    if candidates:
        raise MathsError(f'"{shown}" could mean {" or ".join(sorted(candidates))}; use the exact symbol.')
    if _folded(name) in _CURRENCIES:
        raise MathsError('Currency conversion needs live exchange rates, which the offline calculator does not '
                         'have.')
    raise MathsError(f'Unknown unit "{shown}".')


@dataclass(frozen=True)
class Conversion:
    value: float
    result: float
    source: Unit
    target: Unit

    @property
    def text(self) -> str:
        return (f'{format_number(self.value, 12)} {_name(self.source, self.value)} = '
                f'{format_number(self.result, SIGNIFICANT)} {_name(self.target, self.result)}')


def _name(unit: Unit, value) -> str:
    return unit.singular if value == 1 else unit.plural


def compatible(source: str, target: str) -> bool:
    """Whether both names are known units of the same category."""
    try:
        return find_unit(source).category == find_unit(target).category
    except MathsError:
        return False


def convert(value, source: str, target: str) -> Conversion:
    """``value`` in ``source`` units expressed in ``target`` units."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MathsError('The value to convert must be a finite number.')
    errors = []
    units = []
    for name in (source, target):
        try:
            units.append(find_unit(name))
        except MathsError as exc:
            errors.append(str(exc))
    currency = [e for e in errors if 'Currency' in e]
    if currency or errors:
        raise MathsError(currency[0] if currency else ' '.join(errors))
    origin, goal = units
    if origin.category != goal.category:
        raise MathsError(f'Cannot convert {origin.plural} ({origin.category}) to {goal.plural} '
                         f'({goal.category}): they measure different things.')
    if origin.category == 'temperature':
        kelvin = value * origin.factor + origin.offset
        if kelvin < -1e-9:
            raise MathsError('That temperature is below absolute zero.')
        result = (kelvin - goal.offset) / goal.factor
    else:
        result = value * origin.factor / goal.factor
    if not math.isfinite(result):
        raise MathsError('The result is too large to calculate.')
    return Conversion(value, result, origin, goal)

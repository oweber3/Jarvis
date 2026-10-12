# Calculator: exact arithmetic and unit conversion

Small local models get arithmetic and unit factors wrong. The `calculator` tool does both exactly, on the
computer, with no model, no network and no `eval`. `jarvis.maths` holds the pure logic (`expression.py`,
`units.py`); `tools/builtin/calculator.py` is a thin tool over it.

## Tool

`calculator` is always registered (it needs no setting, device or network) and every call is `SAFE`. It
has an `action` enum:

| Action | Arguments | Result |
|--------|-----------|--------|
| `evaluate` | `expression`; optional `angle_unit` (`radians`, the default, or `degrees`) | `17 × 23 = 391`: the expression as parsed, with readable symbols, and the value |
| `convert` | `value` (default 1), `from_unit`, `to_unit` | `1 litre = 4.22675 US cups`: the value and both unit names |

With no `action`, an expression means `evaluate` and a unit means `convert`. Results are raw data; the
reply model phrases them. A refusal is `success=False` with a short reason (`Could not calculate: …`), never
an exception and never a guess. Debug logs name the action and whether it was refused, never the
expression or units. Results skip the tool-result digest (`reply/engine.py`, `_DIGEST_SKIP_TOOLS`) because
they are already one short line.

The router reads the first 120 characters of the description, which open with "Exact arithmetic and unit
conversion" and ask for the tool to be used for any sum, percentage or conversion. The description says
currency is not supported. The local reply loop's tool constraints (`reply/prompts/model_variants.py`, both
model sizes) add that any calculation, percentage or unit conversion goes through `calculator`, never mental
arithmetic (`evals/test_calculator_routing.py`). Reply bridges offer the tool like any other: it touches no personal data.

## Expressions

The expression is parsed with `ast.parse(mode='eval')` and walked by hand. Accepted:

- integer and decimal numbers; `+ - * / // % **` and unary `+ -`; brackets;
- `^` as power and `× ÷ − ·` as their ASCII operators;
- a `%` with no operand after it is a percentage (`15% * 80` is 12, `200 / 50%` is 400); between two
  operands it is the remainder (`17 % 5` is 2), shown as `mod`;
- constants `pi`, `e`, `tau`;
- functions `sqrt cbrt exp ln log log10 log2 abs round floor ceil trunc factorial min max hypot degrees
  radians gcd lcm sin cos tan asin acos atan atan2 sinh cosh tanh`, called with plain positional
  arguments. `log(x)` is base 10 and `log(x, b)` base `b`; `ln` is natural.

Everything else (names, attributes, strings, comparisons, keywords, collections, complex numbers,
booleans) is refused before anything is calculated. `validate(expression)` checks the syntax only.

Integer arithmetic stays exact; division of integers that divide evenly stays an integer. Trigonometry
uses radians unless degrees are asked for, and results within 1e-12 of zero are zero. Floats are shown to
12 significant digits without float noise (`0.1 + 0.2` is `0.3`); integers longer than 30 digits are shown in
scientific notation.

Limits keep every call cheap: at most 300 characters and 120 syntax nodes, integers of at most 13,000 bits
(checked before a multiplication or power is computed), `factorial` up to 1000. Division by zero, domain
errors (`sqrt(-1)`, `log(0)`), non-real results, overflow and results over the limits are refusals.

## Units

Each unit has a canonical symbol, a category, its size in the category's base unit and English display
names (temperatures also carry an offset to kelvin). Categories: length, mass, volume, temperature, speed,
area, time, data, energy, power and pressure.

A unit is found by its symbol or a canonical name: first an exact, case-sensitive match, then a case-folded
match ignoring spaces, `_`, `-`, `^`, `.` and `°`, accepted only when it names one unit. Case decides
between units that differ only in case (`B` byte and `b` bit, `MB` megabyte and `Mb` megabit, `cal` and `Cal`
kilocalorie); a folded name that could mean several (`mb`) is refused with the candidates. These names are
unit identifiers, not language: what a person says is mapped to symbols by the fast-path locale table or
the model.

Defaults where a name is ambiguous across systems: `cup`, `pint`, `quart`, `gallon`, `floz`, `tbsp` and `tsp`
are US customary, and `ton` is the US short ton; imperial and metric variants have their own symbols
(`imp_gal`, `imp_pt`, `imp_qt`, `imp_floz`, `metric_cup`, `long_ton`, `t`). Display names say "US" or
"imperial", so the result states which was used. `kB`, `MB`, `GB` are decimal; `KiB`, `MiB`, `GiB` binary.
`mo` and `yr` are average months and Julian years (365.25 days).

Temperatures convert through kelvin and refuse values below absolute zero. Units of different categories
(mass to volume, which would need a density) are refused. Currency codes, symbols and names are recognised
only to refuse them honestly: exchange rates need the network, and the calculator is offline. Converted
values are shown to 6 significant digits.

## Fast path

Spoken sums and conversions are fast-path families (`fastpath/fastpath.spec.md`, family `calculator`):

- `{expression}`: the locale `arithmetic.operators` table maps words to operators (`times` to `*`, `to the
  power of` to `**`, `squared` to `** 2`), `arithmetic.percent_of` names the word joining a percentage to its
  base ("15 percent of 80"), and `numbers` maps number words. Digits and `+ - %` pass through. The result
  must be one number followed by operator and number pairs, contain an operation and pass `validate`;
  anything else falls through.
- `{amount}`: digits (with a sign or decimal point), a `numbers` word, or an `amount_words` word ("a").
- `{from_unit}`, `{to_unit}`: a name in the locale `units` table, which maps to a calculator symbol. When
  both are resolved they must be of the same category, or the rule falls through.

Phrases such as "what is {expression}" and "convert {amount} {from_unit} to {to_unit}" are offered to
every request; the bare forms ("{expression}", "{amount} {from_unit} in {to_unit}") are marked
`needs_wake_word`, so a sum overheard in the hot window is not answered. Typed `*` and `/` are removed by
fast-path normalisation, so typed expressions with them take the model path, which calls the tool.

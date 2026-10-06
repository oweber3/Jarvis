"""Pure, whole-utterance matching against locale command-family templates."""
from dataclasses import dataclass, field
from functools import lru_cache
import json
from typing import Callable
from pathlib import Path
import re
import unicodedata

from rapidfuzz.fuzz import ratio

from ..debug import debug_log


@dataclass(frozen=True)
class FastTarget:
    """Known application names and the tool targets for launch/window actions."""
    names: tuple[str, ...]
    open_target: str
    window_target: str
    display: str


@dataclass(frozen=True)
class FastMatch:
    command_id: str
    family: str
    tool_name: str
    args: dict
    reply_template: str | None = None
    slots: dict = field(default_factory=dict)
    # Acts on whatever has focus from a phrase short enough to be overheard: offered only to requests
    # addressed with the wake word (or typed), never to hot-window follow-ups.
    needs_wake_word: bool = False


def normalise(text: str) -> str:
    """Keep numeric signs and separators so malformed slots stay malformed."""
    text = unicodedata.normalize('NFKC', text).casefold().replace('’', "'")
    text = text.replace("'", '')
    text = re.sub(r'[^\w\s.%+;&\-]', ' ', text, flags=re.UNICODE)
    return ' '.join(text.split()).strip(' .')


@dataclass(frozen=True)
class _Context:
    """What a slot resolver may read: the locale tables, the known targets and the call built so far."""
    data: dict
    targets: tuple[FastTarget, ...]
    workspaces: tuple[FastTarget, ...]
    args: dict
    rule: dict
    tv_apps: tuple[FastTarget, ...] = ()
    routines: tuple[FastTarget, ...] = ()


@dataclass(frozen=True)
class _Slot:
    """A ``{name}`` placeholder in locale phrases: the text it matches and how it becomes arguments.

    ``expression`` builds the regular expression from the locale data. ``resolve`` returns the
    arguments to add and the display text for the reply template, or ``None`` when the text is not a
    valid value, in which case the rule does not match."""
    expression: Callable[[dict], str]
    resolve: Callable[[str, _Context], tuple[dict, str] | None]


_FREE_TEXT = r'.{1,80}?'


def _free_text(_data: dict) -> str:
    return _FREE_TEXT


def _strip_articles(value: str, data: dict) -> str:
    """Drop leading articles and possessives (locale data) from a spoken name."""
    words = value.split()
    while len(words) > 1 and words[0] in data.get('article_prefixes', ()):
        words.pop(0)
    return ' '.join(words)


def _folder(value: str, ctx: _Context):
    folder = ctx.data['folders'].get(value)
    return None if folder is None else ({'target': folder}, folder)


def _percent(value: str, ctx: _Context):
    percent = float(value)
    return ({'percent': percent}, value) if 0 <= percent <= 100 else None


def _workspace(value: str, ctx: _Context):
    workspace = _resolve_target(value, ctx.workspaces, False)
    return None if workspace is None else ({'target': workspace.open_target}, workspace.display)


def _app(value: str, ctx: _Context):
    window = ctx.rule['family'] == 'windows' or ctx.args['action'] != 'open'
    target = _resolve_target(value, ctx.targets, window)
    if target is None:
        return None
    # Deterministic window routes never fall back to loose title matching.
    updates = {'target': target.window_target, 'match': 'process'} if window else {'target': target.open_target}
    return updates, target.display


def _tv_app(value: str, ctx: _Context):
    app = _resolve_target(value, ctx.tv_apps, False)
    return None if app is None else ({'app': app.open_target}, app.display)


def _routine(value: str, ctx: _Context):
    routine = _resolve_target(value, ctx.routines, False)
    return None if routine is None else ({'name': routine.open_target}, routine.display)


def _table_lookup(table: str, arg: str):
    def resolve(value: str, ctx: _Context):
        found = ctx.data.get(table, {}).get(value)
        return None if found is None else ({arg: found}, value)
    return resolve


def _number_expression(data: dict) -> str:
    words = sorted(data.get('numbers', {}), key=len, reverse=True)
    return '|'.join([r'\d{1,2}', *(re.escape(word) for word in words)])


def _number(value: str, ctx: _Context):
    number = int(value) if value.isdigit() else ctx.data.get('numbers', {}).get(value)
    return ({'number': str(number)}, value) if number and number >= 1 else None


def _page_number_expression(data: dict) -> str:
    words = sorted(data.get('numbers', {}), key=len, reverse=True)
    return '|'.join([r'\d{1,5}', *(re.escape(word) for word in words)])


def _page_number(value: str, ctx: _Context):
    page = int(value) if value.isdigit() else ctx.data.get('numbers', {}).get(value)
    return ({'page': page}, str(page)) if page and page >= 1 else None


def _keys(value: str, ctx: _Context):
    """A spoken or typed chord as canonical key names. Every word must be a known key. A joiner word joins
    only between two keys; anywhere else it names its own key ("control plus" is Ctrl and the plus key)."""
    joiners = set(ctx.data.get('key_joiners', ()))
    names = ctx.data.get('keys', {})
    keys = []
    words = value.replace('+', ' ').split()
    for index, word in enumerate(words):
        if word in joiners and keys and index + 1 < len(words):
            continue
        key = names.get(word) or (word if re.fullmatch(r'[a-z0-9]|f\d{1,2}', word) else None)
        if key is None:
            return None
        keys.append(key)
    from ..platform.windows.input_control import parse_chord  # pure: validates the chord's shape only
    try:
        chord = '+'.join(parse_chord('+'.join(keys)))
    except ValueError:
        return None
    return {'keys': chord}, chord


def _device(value: str, ctx: _Context):
    name = _strip_articles(value, ctx.data)
    return ({'name': name}, name) if name else None


# Every ``{slot}`` that locale phrases may use. Rules rename a slot's argument with ``slot_args``.
SLOTS: dict[str, _Slot] = {
    'app': _Slot(_free_text, _app),
    'folder': _Slot(_free_text, _folder),
    'percent': _Slot(lambda _data: r'\d{1,3}(?:\.\d+)?', _percent),
    'workspace': _Slot(_free_text, _workspace),
    'page': _Slot(_free_text, _table_lookup('settings_pages', 'page')),
    'plan': _Slot(_free_text, _table_lookup('power_plans', 'name')),
    'number': _Slot(_number_expression, _number),
    'keys': _Slot(lambda _data: r'.{1,60}?', _keys),
    'device': _Slot(lambda _data: r'.{1,60}?', _device),
    'tv_app': _Slot(_free_text, _tv_app),
    'page_number': _Slot(_page_number_expression, _page_number),
    'routine': _Slot(_free_text, _routine),
}


# Rules added by local extensions (``extensions/extensions.spec.md``), per language, after the built-in rules.
_extension_rules: dict[str, tuple] = {}


def set_extension_rules(rules_by_language: dict) -> None:
    """Replace the extension rules; the loader validates them first."""
    global _extension_rules
    _extension_rules = {lang: tuple(rules) for lang, rules in rules_by_language.items()}
    _locale.cache_clear()


def _compile(phrase: str, data) -> re.Pattern:
    pattern = re.escape(phrase)
    for name, slot in SLOTS.items():
        pattern = pattern.replace(re.escape('{' + name + '}'), f'(?P<{name}>{slot.expression(data)})')
    return re.compile(pattern)


def _built_in(language: str):
    if not re.fullmatch(r'[a-z]{2,3}', language):
        return None
    path = Path(__file__).parent / 'phrases' / f'{language}.json'
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def rule_problem(language: str, rule) -> str | None:
    """Why an extension rule cannot be used for ``language``, or None when every phrase compiles."""
    data = _built_in(language)
    if data is None:
        return f'there are no built-in {language} phrases to add to'
    phrases = rule.get('phrases') if isinstance(rule, dict) else None
    if not isinstance(phrases, list) or not phrases or not all(isinstance(p, str) and p for p in phrases):
        return 'phrases must be a list of text'
    try:
        for phrase in phrases:
            _compile(phrase, data)
    except (re.error, KeyError, TypeError, ValueError) as exc:
        return f'a phrase does not compile ({type(exc).__name__})'
    return None


@lru_cache(maxsize=16)
def _locale(language: str):
    data = _built_in(language)
    if data is None:
        return None
    patterns = [(rule, _compile(phrase, data)) for rule in data['rules'] for phrase in rule['phrases']]
    for rule in _extension_rules.get(language, ()):
        if rule_problem(language, rule) is not None:
            debug_log(f'Extension fast-path rule {rule.get("id")} skipped: it does not compile.', 'routing')
            continue
        data['rules'].append(rule)
        patterns.extend((rule, _compile(phrase, data)) for phrase in rule['phrases'])
    return data, patterns


def _resolve_target(query: str, targets: tuple[FastTarget, ...], window: bool):
    # Group duplicate catalogue entries by the actual tool target.
    ranked = {}
    window_identities = {}
    if window:
        for target in targets:
            window_identities.setdefault(target.window_target.casefold(), set()).add(target.open_target.casefold())
    for target in targets:
        key = target.window_target if window else target.open_target
        if not key or (window and (not target.open_target or len(window_identities[key.casefold()]) > 1)):
            continue
        names = [normalise(name) for name in target.names]
        # Spelling tolerance may change characters, never add words or versions.
        names = [name for name in names if len(name.split()) == len(query.split())
                 and re.findall(r'\d+', name) == re.findall(r'\d+', query)]
        score = max((min(ratio(a, b) for a, b in zip(query.split(), name.split()))
                     for name in names), default=0)
        if key not in ranked or score > ranked[key][0]:
            ranked[key] = (score, target)
    ordered = sorted(ranked.values(), key=lambda item: item[0], reverse=True)
    if not ordered or ordered[0][0] < 92:
        return None
    if len(ordered) > 1 and ordered[0][0] - ordered[1][0] < 8:
        return None
    return ordered[0][1]


def match(text: str, language: str | None = 'en', *, targets: tuple[FastTarget, ...] = (),
          workspaces: tuple[FastTarget, ...] = (), tv_apps: tuple[FastTarget, ...] = (),
          routines: tuple[FastTarget, ...] = (), available_tools=()) -> FastMatch | None:
    """Return exactly one high-confidence call, with no discovery or execution.

    ``targets`` are application names; ``workspaces`` are configured workspace names and aliases
    (``open_target`` is the canonical workspace name); ``tv_apps`` are the apps the TV reported;
    ``routines`` are the names and aliases of the user's routines (``open_target`` is the canonical name)."""
    lang = (language or 'en').casefold().replace('_', '-').split('-')[0]
    locale = _locale(lang)
    if locale is None or any(marker in text for marker in (';', '&', '\n')):
        return None
    data, patterns = locale
    cleaned = normalise(text)
    # Fillers are boundary-only: never erase words from the middle of a request.
    for _ in range(4):
        before = cleaned
        for prefix in data['prefixes']:
            if cleaned.startswith(prefix + ' '):
                cleaned = cleaned[len(prefix):].strip()
        for suffix in data['suffixes']:
            if cleaned.endswith(' ' + suffix):
                cleaned = cleaned[:-len(suffix)].strip(' .')
        if cleaned == before:
            break
    if set(cleaned.split()) & set(data['compound_words']):
        return None
    matches = {}
    for rule, pattern in patterns:
        if rule['tool'] not in available_tools:
            continue
        found = pattern.fullmatch(cleaned)
        if found is None:
            continue
        args, slots = dict(rule['args']), found.groupdict()
        context = _Context(data, targets, workspaces, args, rule, tv_apps, routines)
        for name, value in list(slots.items()):
            resolved = SLOTS[name].resolve(value, context)
            if resolved is None:
                break
            updates, display = resolved
            rename = rule.get('slot_args', {}).get(name)
            if rename and len(updates) == 1:
                updates = {rename: next(iter(updates.values()))}
            args.update(updates)
            slots[name] = display
        else:
            result = FastMatch(rule['id'], rule['family'], rule['tool'], args, rule['reply'], slots,
                               bool(rule.get('needs_wake_word')))
            matches[(result.tool_name, json.dumps(result.args, sort_keys=True))] = result
    return next(iter(matches.values())) if len(matches) == 1 else None

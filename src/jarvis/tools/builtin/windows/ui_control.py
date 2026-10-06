"""uiControl: drive any application's controls through UI Automation (``ui_automation.spec.md``)."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ...base import Tool
from ...types import ToolExecutionResult
from ....debug import debug_log
from ....utils.redact import redact
from .desktop_control import _redact_data

ACTIONS = ('snapshot', 'click', 'set_text', 'select', 'toggle', 'expand', 'menu', 'read', 'scroll')
_ALIASES = {'invoke': 'click'}
_ARGUMENTS = ('action', 'window', 'element', 'value')
_TIMEOUT_SEC = 12
_INSPECT_TIMEOUT_SEC = 3
# Actions that commit something when aimed at an irreversible control.
_COMMITTING = ('click', 'toggle')
# Actions that change the window and so leave a desktop referent.
_REMEMBERED = ('click', 'set_text', 'select', 'toggle', 'expand', 'menu', 'scroll')
_WORDS_DIR = Path(__file__).resolve().parent / 'control_words'
_WORD_RE = re.compile(r'\w+', re.UNICODE)

_TABLE_LOCK = threading.Lock()
_TABLES: Optional[Dict[str, List[Tuple[str, ...]]]] = None


def _control_words() -> Dict[str, List[Tuple[str, ...]]]:
    """Every shipped language's word lists together: an application's interface language is independent
    of the language the user speaks."""
    global _TABLES
    with _TABLE_LOCK:
        if _TABLES is None:
            tables: Dict[str, List[Tuple[str, ...]]] = {'irreversible': [], 'generic_accept': []}
            for path in sorted(_WORDS_DIR.glob('*.json')):
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    debug_log(f'Control word table unreadable ({type(exc).__name__}).', 'safety')
                    continue
                for key in tables:
                    tables[key] += [tuple(_words(phrase)) for phrase in data.get(key, []) if _words(phrase)]
            _TABLES = tables
        return _TABLES


def _words(text: Any) -> List[str]:
    return [word.casefold() for word in _WORD_RE.findall(str(text or '').replace('&', ''))]


def _contains_phrase(text: str, phrases: Iterable[Tuple[str, ...]]) -> bool:
    """Whole words or word sequences, never substrings: "Sender" is not "send"."""
    words = _words(text)
    return any(words[start:start + len(phrase)] == list(phrase)
               for phrase in phrases for start in range(len(words) - len(phrase) + 1))


def is_irreversible(name: str) -> bool:
    return _contains_phrase(name, _control_words()['irreversible'])


def is_generic_accept(name: str) -> bool:
    words = _words(name)
    return bool(words) and tuple(words) in set(_control_words()['generic_accept'])


def _menu_segments(path: str) -> List[str]:
    return [segment.strip() for segment in re.split(r'\s*(?:>|→|»)\s*', str(path or '')) if segment.strip()]


def _clip(text: str, limit: int = 60) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


SNAPSHOT_NOTE = ('Nothing in the window has changed yet. To act, call uiControl again with the element id from '
                 'this list; say an action is done only after that result confirms it.')


def snapshot_result(data: dict) -> dict:
    """The model-facing snapshot: the controls, labelled as a list to act on rather than an outcome."""
    return {**data, 'note': SNAPSHOT_NOTE}


def _record_window(window: dict, requested: str) -> None:
    """Remember the application window an action changed (``memory/desktop_referents.spec.md``)."""
    try:
        from ....memory.desktop_referents import DesktopReferent, get_desktop_referents
        hwnd = window.get('top_hwnd') or window.get('hwnd')
        if hwnd is None:
            return
        named = '' if requested.strip().isdigit() else requested.strip()
        get_desktop_referents().record(DesktopReferent(
            application=named or window.get('process') or '', process=window.get('process') or '',
            hwnd=int(hwnd), last_action='control'))
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f'desktop referent not recorded ({type(exc).__name__}).', 'windows')


class UiControlTool(Tool):
    # Its results carry outside content (routines.spec.md, Prompt-injection boundary).
    returns_outside_content = True

    name = 'uiControl'
    description = ('Click buttons, type into fields, choose menu items, tick boxes or read text in any open app '
                   "window, by the control's visible name. Use snapshot only to list controls whose names are "
                   'unknown. NOT for opening or switching apps (use appControl) or PDF pages (use pdfNavigate).')

    @property
    def inputSchema(self):
        return {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': list(ACTIONS), 'description': (
                    'snapshot lists the window\'s controls with ids; click presses a button, link, tab or item; '
                    'set_text replaces a field\'s text; select picks an item (value names it inside a list or '
                    'combo box); toggle ticks or unticks; expand opens a tree item or drop-down; menu runs a menu '
                    'path; read returns text; scroll moves the view.')},
                'window': {'type': 'string', 'description': (
                    'App name, window title or handle. Leave empty for the window the user is working in.')},
                'element': {'type': 'string', 'description': (
                    'The control\'s visible name, e.g. "Save" or "Subject"; or an id such as "e3" only from a '
                    'snapshot result you already have.')},
                'value': {'type': 'string', 'description': (
                    'set_text: the text. select: the item. menu: a path such as "File > Save As". toggle: on or '
                    'off. scroll: up, down, left, right, top or bottom.')},
            },
            'required': ['action'],
            'additionalProperties': False,
        }

    # -- validation ------------------------------------------------------------

    @staticmethod
    def _parse(args: Any) -> Tuple[str, str, str, str]:
        if not isinstance(args, dict) or set(args) - set(_ARGUMENTS):
            raise ValueError('Invalid uiControl arguments.')
        action = _ALIASES.get(args.get('action'), args.get('action'))
        if action not in ACTIONS:
            raise ValueError('Unsupported action.')
        values = []
        for key in ('window', 'element', 'value'):
            value = args.get(key)
            if value is None:
                value = ''
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f'{key} must be text.')
            values.append(str(value))
        window, element, value = values
        if action in ('click', 'set_text', 'toggle', 'expand') and not element.strip():
            raise ValueError('Name the control, or give its id from a snapshot.')
        if action == 'select' and not element.strip():
            raise ValueError('Name the list, combo box or item to select in.')
        if action == 'menu' and not _menu_segments(value):
            raise ValueError('Give a menu path such as "File > Save As" in value.')
        return action, window, element, value

    # -- safety ----------------------------------------------------------------

    def _inspect(self, window: str, element: str, wants_texts) -> Optional[dict]:
        from ....platform.windows import ui_automation as ui
        from ....platform.windows._bounded import run_bounded
        try:
            return run_bounded(lambda: ui.inspect(window, element, wants_texts), _INSPECT_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 - classification falls back to the requested name
            debug_log(f'uiControl safety lookup unavailable ({type(exc).__name__}).', 'safety')
            return None

    @staticmethod
    def _menu_names(window: str, path: str) -> List[str]:
        """The real item names along a menu path, as far as they can be read without opening a menu."""
        from ....platform.windows import ui_automation as ui
        from ....platform.windows._bounded import run_bounded
        try:
            return list(run_bounded(lambda: ui.menu_item_names(window, path), _INSPECT_TIMEOUT_SEC))
        except Exception as exc:  # noqa: BLE001 - classification falls back to the requested words
            debug_log(f'uiControl menu lookup unavailable ({type(exc).__name__}).', 'safety')
            return []

    def classify_safety(self, args: Optional[Dict[str, Any]], cfg: Any):
        from ...confirmation import ConfirmationRequest, SafetyTier

        def request(tier, action='', consequence=None, reason=None):
            return ConfirmationRequest(tool_name=self.name, tier=tier, action=action or self.name, target='',
                                       parameters=dict(args or {}), consequence=consequence, reason=reason)

        try:
            action, window, element, value = self._parse(args)
        except ValueError:
            return request(SafetyTier.SAFE)
        if not getattr(cfg, 'windows_tools_enabled', True):
            return request(SafetyTier.SAFE)
        if action in ('set_text', 'read') and element.strip():
            info = self._inspect(window, element, lambda _name: False)
            if info and info.get('password'):
                return request(SafetyTier.DENY, action, reason='Typing into or reading password fields is not allowed.')
            return request(SafetyTier.SAFE, action)
        if action == 'menu':
            segments = _menu_segments(value)
            real = self._menu_names(window, value)
            if any(is_irreversible(segment) for segment in segments + real):
                path = _clip(' > '.join(real + segments[len(real):]))
                return request(SafetyTier.CONFIRM_VOICE, f'choose "{path}" from the menu',
                               consequence='This menu command may send, delete or commit something that cannot be undone.')
            return request(SafetyTier.SAFE, action)
        if action not in _COMMITTING:
            return request(SafetyTier.SAFE, action)
        info = self._inspect(window, element, is_generic_accept)
        name = (info or {}).get('name') or ('' if _is_id(element) else element)
        where = f' in {_clip(info["process"], 40)}' if info and info.get('process') else ''
        label = f'{action} "{_clip(name)}"{where}'
        if is_irreversible(name) or (info is None and is_irreversible(element)):
            return request(SafetyTier.CONFIRM_VOICE, label,
                           consequence='This control may send, delete or commit something that cannot be undone.')
        texts = (info or {}).get('texts') or []
        if is_generic_accept(name) and any(is_irreversible(text) for text in texts):
            return request(SafetyTier.CONFIRM_VOICE, label,
                           consequence=f'It answers: "{_clip(next(t for t in texts if is_irreversible(t)), 120)}"')
        return request(SafetyTier.SAFE, action)

    # -- execution -------------------------------------------------------------

    def run(self, args, context):
        try:
            if not context.cfg.windows_tools_enabled:
                raise ValueError("Windows control is disabled; set 'windows_tools_enabled' to true to use it.")
            action, window, element, value = self._parse(args)
            from ....platform.windows import ui_automation as ui
            from ....platform.windows._bounded import run_bounded
            if action == 'snapshot':
                data = snapshot_result(run_bounded(lambda: ui.snapshot(window), _TIMEOUT_SEC))
            else:
                data = run_bounded(lambda: ui.act(action, window, element, value, refuse_item=_reached_by_part),
                                   _TIMEOUT_SEC)
            window_data = dict(data.get('window') or {})
            if action in _REMEMBERED:
                _record_window(window_data, window)
            window_data.pop('top_hwnd', None)
            data = {**data, 'window': window_data}
            context.user_print(f'🧭 UI Automation {action} completed.')
            return ToolExecutionResult(success=True, reply_text=json.dumps(_redact_data(data), ensure_ascii=False))
        except PermissionError as exc:
            debug_log(f'{self.name} refused a password field.', 'safety')
            return ToolExecutionResult(success=False, reply_text=None,
                                       error_message=f'This action is prohibited: {exc}')
        except ValueError as exc:
            data = getattr(exc, 'data', None)
            debug_log(f'{self.name} could not act ({type(exc).__name__}).', 'windows')
            text = json.dumps(_redact_data(data), ensure_ascii=False) if data else None
            return ToolExecutionResult(success=False, reply_text=text, error_message=redact(str(exc)))
        except (OSError, TypeError) as exc:
            debug_log(f'{self.name} failed ({type(exc).__name__}).', 'windows')
            return ToolExecutionResult(success=False, reply_text=None, error_message=redact(str(exc)))


def _reached_by_part(requested: str, actual: str) -> bool:
    """An irreversible menu item named only by harmless words: classification saw those words, so it never
    asked for confirmation. Naming the item in full is classified (and confirmed) as what it is."""
    return is_irreversible(actual) and not is_irreversible(requested)


def _is_id(element: str) -> bool:
    return bool(re.fullmatch(r'e\d+', element.strip(), re.IGNORECASE))

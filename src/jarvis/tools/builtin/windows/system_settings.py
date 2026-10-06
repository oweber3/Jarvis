"""Windows Settings pages, brightness, power plans and the default audio output."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ....debug import debug_log
from ....platform.windows import audio_outputs, brightness, displays, power, settings_pages
from ....utils.redact import redact
from ...base import Tool, ToolContext
from ...types import ToolExecutionResult
from ._common import disabled_result, failure, parse_number

_ACTIONS = ['open_page', 'brightness', 'power_plan', 'audio_output', 'night_light', 'do_not_disturb']
_OPERATIONS = ['get', 'set', 'up', 'down', 'list']
_DEFAULT_STEP = 10
# Windows has no supported API for these two switches, so they open their settings page and say so.
_PAGE_ONLY = {'night_light': 'night_light', 'do_not_disturb': 'focus'}
_ALLOWED = {'action', 'operation', 'page', 'percent', 'amount', 'name', 'monitor'}


def _json(value: Any) -> str:
    def scrub(item):
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [scrub(entry) for entry in item]
        if isinstance(item, dict):
            return {key: scrub(entry) for key, entry in item.items()}
        return item
    return json.dumps(scrub(value), ensure_ascii=False)


def _ok(context: ToolContext, icon: str, line: str, payload: dict) -> ToolExecutionResult:
    context.user_print(f'{icon} {line}')
    return ToolExecutionResult(success=True, reply_text=_json(payload))


class SystemSettingsTool(Tool):
    """Open settings pages and change brightness, power plan or audio output."""

    @property
    def name(self) -> str:
        return 'systemSettings'

    @property
    def description(self) -> str:
        return (
            'Open a Windows Settings page (Bluetooth, display, sound, Wi-Fi, power, updates, apps, privacy); make the '
            'screen brighter or dimmer or set its brightness; switch the power plan or mode (balanced, high '
            'performance, power saver); switch the default audio output (speakers, headphones). NOT for volume or '
            'mute; that is systemVolume.'
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': _ACTIONS,
                           'description': ('open_page opens a settings page; brightness, power_plan and '
                                           'audio_output read or change that setting; night_light and '
                                           'do_not_disturb open their settings page (Windows offers no switch).')},
                'operation': {'type': 'string', 'enum': _OPERATIONS,
                              'description': ('OPTIONAL. get, set, up, down (brightness) or list. Defaults: '
                                              'brightness is set when percent is given, else get; power_plan is '
                                              'set when name is given, else get; audio_output is set when name '
                                              'is given, else list.')},
                'page': {'type': 'string',
                         'description': 'For open_page: bluetooth, display, sound, wifi, power, updates, apps, privacy ...'},
                'percent': {'type': 'number', 'description': 'Brightness 0 to 100 for set.'},
                'amount': {'type': 'number',
                           'description': f'Points to change brightness for up or down (default {_DEFAULT_STEP}).'},
                'name': {'type': 'string',
                         'description': ('Power plan (balanced, power_saver, high_performance or its name) or audio '
                                         'output device name, for set.')},
                'monitor': {'type': 'string',
                            'description': 'OPTIONAL display number or alias for brightness; default is all displays.'},
            },
            'required': ['action'],
            'additionalProperties': False,
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        blocked = disabled_result(context)
        if blocked:
            return blocked
        args = args if isinstance(args, dict) else {}
        if set(args) - _ALLOWED:
            return failure('Invalid systemSettings arguments.')
        action = str(args.get('action') or '').strip().lower()
        debug_log(f'systemSettings action={action}', 'windows')
        try:
            if action == 'open_page':
                return self._open_page(args, context)
            if action in _PAGE_ONLY:
                return self._page_only(action, context)
            if action == 'brightness':
                return self._brightness(args, context)
            if action == 'power_plan':
                return self._power_plan(args, context)
            if action == 'audio_output':
                return self._audio_output(args, context)
        except (ValueError, OSError, brightness.BrightnessError, power.PowerError,
                audio_outputs.AudioOutputError) as exc:
            debug_log(f'systemSettings {action} failed ({type(exc).__name__}).', 'windows')
            context.user_print('⚠️ Could not change that setting.')
            return failure(redact(str(exc)))
        return failure(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")

    def _open_page(self, args, context):
        page = args.get('page')
        if not isinstance(page, str) or not page.strip():
            raise ValueError('A settings page name is required, for example bluetooth, display or sound.')
        result = settings_pages.open_page(page)
        return _ok(context, '⚙️', f"Opened the {result['page'].replace('_', ' ')} settings page", result)

    def _page_only(self, action, context):
        result = settings_pages.open_page(_PAGE_ONLY[action])
        payload = {**result, 'action': action, 'changed': False,
                   'note': 'Windows has no supported way to switch this from outside Settings, so its settings '
                           'page was opened instead.'}
        return _ok(context, '⚙️', f"Opened the {result['page'].replace('_', ' ')} settings page", payload)

    def _brightness(self, args, context):
        operation = str(args.get('operation') or '').strip().lower() or ('set' if args.get('percent') is not None else 'get')
        if operation not in ('get', 'set', 'up', 'down'):
            raise ValueError('Brightness supports get, set, up and down.')
        device = None
        if args.get('monitor') not in (None, ''):
            device = displays.resolve_monitor(str(args['monitor']), displays.list_monitors(),
                                              getattr(context.cfg, 'windows_monitor_aliases', {})).device
        if operation == 'get':
            readings = brightness.read_levels(device)
        elif operation == 'set':
            percent = parse_number(args.get('percent'))
            if percent is None:
                raise ValueError('A brightness percentage between 0 and 100 is required.')
            readings = brightness.set_levels(percent, device)
        else:
            amount = parse_number(args.get('amount'))
            step = round(amount) if amount is not None and amount > 0 else _DEFAULT_STEP
            readings = brightness.adjust_levels(step if operation == 'up' else -step, device)
        monitors = [{'display': r.number, 'device': r.device, 'percent': r.percent, 'method': r.method,
                     'verified': r.verified} for r in readings if r.percent is not None]
        unsupported = [{'display': r.number, 'device': r.device, 'reason': r.error} for r in readings
                       if r.percent is None]
        payload: dict = {'action': 'brightness', 'operation': operation, 'monitors': monitors}
        if unsupported:
            payload['unsupported'] = unsupported
        if not monitors:
            context.user_print('⚠️ No display supports brightness control.')
            message = 'No display supports brightness control (DDC/CI or the built-in panel).'
            return ToolExecutionResult(success=False, reply_text=_json(payload), error_message=message)
        summary = ', '.join(f"display {m['display']} {m['percent']}%" for m in monitors)
        return _ok(context, '🔆', f'Brightness: {summary}', payload)

    def _power_plan(self, args, context):
        operation = str(args.get('operation') or '').strip().lower() or ('set' if args.get('name') else 'get')
        if operation == 'list':
            plans = power.list_plans()
            payload = {'action': 'power_plan', 'plans': [{'name': p.name, 'active': p.active} for p in plans]}
            return _ok(context, '🔋', 'Power plans listed', payload)
        if operation == 'get':
            return _ok(context, '🔋', 'Power plan read', {'action': 'power_plan', 'active': power.active_plan().name})
        if operation != 'set' or not args.get('name'):
            raise ValueError('Power plan supports get, list, and set with a name.')
        plan = power.set_plan(str(args['name']))
        return _ok(context, '🔋', f'Power plan set to {plan.name}',
                   {'action': 'power_plan', 'active': plan.name, 'changed': True})

    def _audio_output(self, args, context):
        operation = str(args.get('operation') or '').strip().lower() or ('set' if args.get('name') else 'list')
        if operation in ('list', 'get'):
            outputs = audio_outputs.list_outputs()
            payload = {'action': 'audio_output', 'outputs': [{'name': d.name, 'default': d.default} for d in outputs]}
            return _ok(context, '🔊', 'Audio outputs listed', payload)
        if operation != 'set' or not args.get('name'):
            raise ValueError('Audio output supports list, and set with a device name.')
        aliases = getattr(context.cfg, 'windows_audio_aliases', {}) or {}
        device = audio_outputs.set_default_output(str(args['name']), aliases)
        return _ok(context, '🔊', f'Audio output set to {device.name}',
                   {'action': 'audio_output', 'default': device.name, 'changed': True})

"""Press keyboard shortcuts and read or write the clipboard."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ....debug import debug_log
from ....platform.windows import input_control
from ....platform.windows._bounded import run_bounded
from ....utils.redact import redact
from ...base import Tool, ToolContext
from ...types import ToolExecutionResult
from ._common import disabled_result, failure

_ACTIONS = ['hotkey', 'clipboard_read', 'clipboard_write']
_ALLOWED = {'action', 'keys', 'text'}
# Clipboard calls are bounded; hotkeys are not, so an abandoned worker can never leave a key held.
_CLIPBOARD_TIMEOUT_SEC = 3.0
_CLIPBOARD_CHECK_SEC = 1.0


def _remember_clipboard(content_type=None, before=None) -> None:
    """What kind of thing the clipboard holds, for "paste it" (``memory/desktop_referents.spec.md``).

    A hotkey records the change counter from before the chord; the type is read only once the
    clipboard has changed, when the record is next read. Never the clipboard's data."""
    try:
        from ....memory.desktop_referents import get_desktop_referents
        store = get_desktop_referents()
        if content_type is not None:
            store.record_clipboard(content_type)
            return
        if before is None:
            return

        def changed():
            if run_bounded(input_control.clipboard_sequence, _CLIPBOARD_CHECK_SEC) == before:
                return None
            return run_bounded(input_control.clipboard_content_type, _CLIPBOARD_CHECK_SEC)

        store.record_clipboard_change(changed)
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f'clipboard referent not recorded ({type(exc).__name__}).', 'windows')


def _action(args) -> str:
    return str((args if isinstance(args, dict) else {}).get('action') or '').strip().lower()


def _sequence_or_none():
    try:
        return run_bounded(input_control.clipboard_sequence, _CLIPBOARD_CHECK_SEC)
    except Exception as exc:  # noqa: BLE001 - without the counter a hotkey records no clipboard entry
        debug_log(f'clipboard counter unavailable ({type(exc).__name__}).', 'windows')
        return None


class InputControlTool(Tool):
    """Send a hotkey to the active window, or use the clipboard."""

    @property
    def name(self) -> str:
        return 'inputControl'

    @property
    def description(self) -> str:
        return (
            'Press a keyboard shortcut (hotkey such as ctrl+shift+esc, alt+tab, win+d, ctrl+c) in the active '
            'window, or read or replace the clipboard text. Shortcuts that close or delete need confirmation. '
            'NOT for opening apps (appControl) or moving windows (windowControl).'
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': _ACTIONS,
                           'description': 'hotkey presses a chord; clipboard_read returns the clipboard text; '
                                          'clipboard_write replaces it.'},
                'keys': {'type': 'string',
                         'description': 'For hotkey: modifiers plus one key joined with +, e.g. ctrl+shift+esc.'},
                'text': {'type': 'string', 'description': 'For clipboard_write: the text to place on the clipboard.'},
            },
            'required': ['action'],
            'additionalProperties': False,
        }

    def returns_outside_content_for(self, args: Optional[Dict[str, Any]]) -> bool:
        # The clipboard holds whatever the user copied from anywhere (routines.spec.md).
        return _action(args) == 'clipboard_read'

    def classify_safety(self, args: Optional[Dict[str, Any]], cfg: Any) -> Any:
        from ...confirmation import ConfirmationRequest, SafetyTier
        args = dict(args) if isinstance(args, dict) else {}
        request = ConfirmationRequest(tool_name=self.name, tier=SafetyTier.SAFE,
                                      action=str(args.get('action') or self.name), target='', parameters=args)
        # Read the action exactly as run() does, so no casing or padding of 'hotkey' skips the check.
        if str(args.get('action') or '').strip().lower() == 'hotkey':
            try:
                chord = input_control.parse_chord(str(args.get('keys') or ''))
            except ValueError:
                return request  # the tool rejects it without sending anything
            if input_control.is_destructive(chord):
                keys = '+'.join(chord)
                # The chord is described in the action, not as a target: a key name is not a path.
                request.tier = SafetyTier.CONFIRM_VOICE
                request.action = f'press {keys}'
                request.consequence = (f"Pressing {keys} can close the active window or tab, quit "
                                       f"the app or delete the selected item.")
        return request

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        blocked = disabled_result(context)
        if blocked:
            return blocked
        args = args if isinstance(args, dict) else {}
        if set(args) - _ALLOWED:
            return failure('Invalid inputControl arguments.')
        action = _action(args)
        debug_log(f'inputControl action={action}', 'windows')
        try:
            if action == 'hotkey':
                chord = input_control.parse_chord(str(args.get('keys') or ''))
                before = _sequence_or_none()
                result = input_control.send_chord(chord)
                _remember_clipboard(before=before)
                context.user_print(f"⌨️ Pressed {result['keys']}")
                return ToolExecutionResult(True, json.dumps(result))
            if action == 'clipboard_read':
                # A hung clipboard owner can block GetClipboardData; never freeze the listener on it.
                clip = run_bounded(input_control.read_clipboard, _CLIPBOARD_TIMEOUT_SEC)
                try:
                    _remember_clipboard(run_bounded(input_control.clipboard_content_type, _CLIPBOARD_CHECK_SEC))
                except Exception as exc:  # noqa: BLE001 - the read itself succeeded
                    debug_log(f'clipboard type unavailable ({type(exc).__name__}).', 'windows')
                context.user_print('📋 Read the clipboard')
                return ToolExecutionResult(True, redact(json.dumps({'action': 'clipboard_read', **clip},
                                                                    ensure_ascii=False)))
            if action == 'clipboard_write':
                text = args.get('text')
                written = run_bounded(lambda: input_control.write_clipboard(text), _CLIPBOARD_TIMEOUT_SEC)
                _remember_clipboard('text')
                context.user_print('📋 Clipboard updated')
                return ToolExecutionResult(True, json.dumps({'action': 'clipboard_written', **written}))
        except (ValueError, OSError) as exc:
            debug_log(f'inputControl {action} failed ({type(exc).__name__}).', 'windows')
            context.user_print('⚠️ Could not complete that input action.')
            return failure(redact(str(exc)))
        return failure(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")

"""Keyboard chords and the clipboard.

Chords are sent with ``SendInput`` to whichever window already has the keyboard focus; nothing
here activates, moves or searches for a window. Key names, virtual-key codes, aliases and the
chords that close or delete are data in ``input_keys.json`` (tool vocabulary, not language
patterns). Held keys are always released, even if injection fails part way.

The clipboard sits behind a small backend interface so tests never touch the real clipboard.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import json
from pathlib import Path
import time
from typing import Callable, Iterable, Protocol

from ...debug import debug_log

MAX_CLIPBOARD_CHARS = 4000
_MAX_WRITE_CHARS = MAX_CLIPBOARD_CHARS * 10
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP = 0x0001, 0x0002

Event = tuple[int, bool]  # (virtual key, key-up?)


@lru_cache(maxsize=1)
def _table() -> dict:
    return json.loads((Path(__file__).parent / 'input_keys.json').read_text(encoding='utf-8'))


def canonical_key(name: str) -> str | None:
    """A key's canonical name from a name or alias, or ``None`` when it is unknown."""
    table = _table()
    key = str(name or '').strip().casefold()
    key = table['aliases'].get(key, key)
    return key if key in table['keys'] else None


def parse_chord(text: str) -> tuple[str, ...]:
    """Parse ``ctrl+shift+esc`` into canonical keys: modifiers first, at most one other key."""
    table = _table()
    parts = [part.strip() for part in str(text or '').split('+')]
    if not parts or any(not part for part in parts):
        raise ValueError('A hotkey such as ctrl+shift+esc is required.')
    keys = []
    for part in parts:
        key = canonical_key(part)
        if key is None:
            raise ValueError(f'Unknown key: {part}')
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError('A key appears twice in the hotkey.')
    modifiers = [key for key in table['modifiers'] if key in keys]
    others = [key for key in keys if key not in table['modifiers']]
    if len(others) > 1 or (not others and len(modifiers) != 1):
        raise ValueError('A hotkey is any modifiers (ctrl, shift, alt, win) plus one other key.')
    chord = (*modifiers, *others)
    if any(set(blocked) == set(chord) for blocked in table['unsendable']):
        raise ValueError('That key combination is reserved by Windows and cannot be sent.')
    return chord


def is_destructive(chord: Iterable[str]) -> bool:
    """Whether the chord closes a window or tab, quits an app or deletes. Key order is irrelevant."""
    pressed = set(chord)
    return any(set(pattern) <= pressed for pattern in _table()['destructive'])


class _KeyboardInput(ctypes.Structure):
    _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort), ('dwFlags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long), ('mouseData', ctypes.c_ulong),
                ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    # The mouse member makes the union as large as Windows expects (INPUT is 40 bytes on 64-bit).
    _fields_ = [('ki', _KeyboardInput), ('mi', _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ulong), ('union', _InputUnion)]


def _send_input(events: list[Event]) -> None:
    extended = {_table()['keys'][name] for name in _table()['extended']}
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int]
    user.SendInput.restype = ctypes.c_uint
    for vk, up in events:
        flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_EXTENDEDKEY if vk in extended else 0)
        item = _Input(1, _InputUnion(ki=_KeyboardInput(vk, 0, flags, 0, 0)))
        if user.SendInput(1, ctypes.byref(item), ctypes.sizeof(item)) != 1:
            raise OSError('Windows blocked the key press (a higher-privilege window may have focus).')
        time.sleep(0.005)


def send_chord(chord: tuple[str, ...], sender: Callable[[list[Event]], None] | None = None) -> dict:
    """Press the chord on the focused window: modifiers down, key down and up, modifiers up in reverse."""
    send = sender or _send_input
    codes = [_table()['keys'][key] for key in chord]
    modifiers = [code for key, code in zip(chord, codes) if key in _table()['modifiers']]
    pressed: list[int] = []
    try:
        for code in codes:
            send([(code, False)])
            pressed.append(code)
        for code in reversed(codes):
            send([(code, True)])
            pressed.remove(code)
    finally:
        for code in reversed(pressed):  # never leave a key held down
            try:
                send([(code, True)])
            except OSError:
                debug_log('Releasing a held key failed.', 'windows')
    debug_log(f'Hotkey sent ({len(chord)} keys, {len(modifiers)} modifiers).', 'windows')
    return {'action': 'hotkey_sent', 'keys': '+'.join(chord)}


class ClipboardBackend(Protocol):
    def get_text(self) -> str | None: ...
    def set_text(self, text: str) -> None: ...
    def sequence(self) -> int: ...
    def content_type(self) -> str: ...


# Standard clipboard formats (winuser.h) that say what kind of thing the clipboard holds.
_CF_TEXT, _CF_BITMAP, _CF_DIB, _CF_UNICODETEXT, _CF_HDROP, _CF_DIBV5 = 1, 2, 8, 13, 15, 17


class _Win32Clipboard:
    def _open(self, win32clipboard):
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                return
            except Exception:  # noqa: BLE001 - another process holds the clipboard briefly
                time.sleep(0.05)
        raise OSError('The clipboard is in use by another program.')

    def get_text(self) -> str | None:
        import win32clipboard
        self._open(win32clipboard)
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return None
        finally:
            win32clipboard.CloseClipboard()

    def set_text(self, text: str) -> None:
        import win32clipboard
        self._open(win32clipboard)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

    def sequence(self) -> int:
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())

    def content_type(self) -> str:
        """``files``, ``text``, ``image``, ``other`` or ``empty``, from the listed formats only: the
        clipboard is never opened and its data never read."""
        user = ctypes.windll.user32
        if not user.CountClipboardFormats():
            return 'empty'
        if user.IsClipboardFormatAvailable(_CF_HDROP):
            return 'files'
        if user.IsClipboardFormatAvailable(_CF_UNICODETEXT) or user.IsClipboardFormatAvailable(_CF_TEXT):
            return 'text'
        if any(user.IsClipboardFormatAvailable(f) for f in (_CF_DIB, _CF_DIBV5, _CF_BITMAP)):
            return 'image'
        return 'other'


_backend: ClipboardBackend | None = None


def set_clipboard_backend(backend: ClipboardBackend | None) -> None:
    """Install a clipboard backend (tests); ``None`` restores the Windows clipboard."""
    global _backend
    _backend = backend


def _clipboard() -> ClipboardBackend:
    return _backend or _Win32Clipboard()


def read_clipboard() -> dict:
    """The clipboard's text, capped. Redaction is the caller's job; this layer returns raw data."""
    text = _clipboard().get_text()
    if not text:
        return {'text': '', 'truncated': False, 'empty': True}
    return {'text': text[:MAX_CLIPBOARD_CHARS], 'truncated': len(text) > MAX_CLIPBOARD_CHARS}


def clipboard_sequence() -> int:
    """The clipboard's change counter: it changes whenever anything replaces the clipboard."""
    return int(_clipboard().sequence())


def clipboard_content_type() -> str:
    """What kind of thing the clipboard holds, never its data."""
    return str(_clipboard().content_type())


def write_clipboard(text) -> dict:
    if not isinstance(text, str):
        raise ValueError('Clipboard text must be a string.')
    if len(text) > _MAX_WRITE_CHARS:
        raise ValueError('That text is too long for the clipboard action.')
    _clipboard().set_text(text)
    debug_log('Clipboard text written.', 'windows')
    return {'length': len(text)}

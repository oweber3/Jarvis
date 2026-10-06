"""Hotkey chords and the clipboard, with input injection and the clipboard replaced."""
import pytest

from jarvis.platform.windows import input_control as ic


class Sent:
    """Collects (virtual key, key-up?) pairs instead of calling SendInput."""

    def __init__(self, fail_after=None):
        self.events, self.fail_after = [], fail_after

    def __call__(self, events):
        for index, event in enumerate(events):
            if self.fail_after is not None and len(self.events) >= self.fail_after:
                raise OSError('SendInput was blocked.')
            self.events.append(event)


VK = {'ctrl': 0x11, 'shift': 0x10, 'alt': 0x12, 'win': 0x5B, 'esc': 0x1B, 'tab': 0x09, 'c': 0x43, 'f4': 0x73,
      'delete': 0x2E, 'enter': 0x0D, 'left': 0x25}


@pytest.mark.parametrize('text,keys', [
    ('ctrl+shift+esc', ('ctrl', 'shift', 'esc')),
    ('Control + Shift + Escape', ('ctrl', 'shift', 'esc')),
    ('WIN+D', ('win', 'd')),
    ('alt+tab', ('alt', 'tab')),
    ('f11', ('f11',)),
    ('ctrl+c', ('ctrl', 'c')),
    ('windows+left', ('win', 'left')),
])
def test_chords_are_normalised_to_canonical_keys(text, keys):
    assert ic.parse_chord(text) == keys


@pytest.mark.parametrize('text', ['', '   ', '+', 'ctrl+', 'ctrl+nonsense', 'ctrl+shift+esc+tab+a+b+c', 'a+b',
                                  'ctrl+ctrl', 'alt+f99'])
def test_malformed_or_unknown_chords_are_rejected(text):
    with pytest.raises(ValueError):
        ic.parse_chord(text)


def test_ctrl_alt_delete_cannot_be_sent():
    with pytest.raises(ValueError):
        ic.parse_chord('ctrl+alt+delete')


def test_a_chord_presses_modifiers_first_then_releases_in_reverse():
    sent = Sent()
    ic.send_chord(('ctrl', 'shift', 'esc'), sender=sent)
    assert sent.events == [(VK['ctrl'], False), (VK['shift'], False), (VK['esc'], False),
                           (VK['esc'], True), (VK['shift'], True), (VK['ctrl'], True)]


def test_a_single_key_is_pressed_and_released():
    sent = Sent()
    ic.send_chord(('f11',), sender=sent)
    assert sent.events == [(0x7A, False), (0x7A, True)]


def test_held_keys_are_released_even_when_injection_fails_midway():
    sent = Sent()

    def flaky(events):
        for event in events:
            if event == (VK['esc'], False):
                raise OSError('blocked')
            sent.events.append(event)

    with pytest.raises(OSError):
        ic.send_chord(('ctrl', 'shift', 'esc'), sender=flaky)
    assert (VK['ctrl'], True) in sent.events and (VK['shift'], True) in sent.events


@pytest.mark.parametrize('chord', [
    'alt+f4', 'ctrl+w', 'ctrl+f4', 'ctrl+shift+w', 'shift+delete', 'delete', 'ctrl+shift+delete', 'ctrl+q',
    'win+ctrl+f4', 'alt+shift+f4', 'ctrl+d',
])
def test_chords_that_close_or_delete_are_flagged(chord):
    assert ic.is_destructive(ic.parse_chord(chord))


@pytest.mark.parametrize('chord', ['ctrl+c', 'ctrl+v', 'ctrl+z', 'alt+tab', 'win+d', 'ctrl+shift+esc', 'f11', 'ctrl+s'])
def test_ordinary_chords_are_not_flagged(chord):
    assert not ic.is_destructive(ic.parse_chord(chord))


def test_destructive_detection_ignores_key_order():
    assert ic.is_destructive(('f4', 'alt'))


class FakeClipboard:
    def __init__(self, text=None):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


@pytest.fixture
def clipboard():
    fake = FakeClipboard('hello')
    ic.set_clipboard_backend(fake)
    yield fake
    ic.set_clipboard_backend(None)


def test_clipboard_text_is_read_through_the_backend(clipboard):
    assert ic.read_clipboard() == {'text': 'hello', 'truncated': False}


def test_an_empty_or_non_text_clipboard_is_reported_as_empty(clipboard):
    clipboard.text = None
    assert ic.read_clipboard() == {'text': '', 'truncated': False, 'empty': True}


def test_long_clipboard_text_is_truncated_and_flagged(clipboard):
    clipboard.text = 'x' * (ic.MAX_CLIPBOARD_CHARS + 50)
    result = ic.read_clipboard()
    assert len(result['text']) == ic.MAX_CLIPBOARD_CHARS and result['truncated'] is True


def test_clipboard_write_replaces_the_text_and_confirms_the_length(clipboard):
    assert ic.write_clipboard('new text') == {'length': 8}
    assert clipboard.text == 'new text'


@pytest.mark.parametrize('value', [None, 5, ['a']])
def test_clipboard_write_requires_text(clipboard, value):
    with pytest.raises(ValueError):
        ic.write_clipboard(value)
    assert clipboard.text == 'hello'


def test_clipboard_write_rejects_oversized_text(clipboard):
    with pytest.raises(ValueError):
        ic.write_clipboard('x' * (ic.MAX_CLIPBOARD_CHARS * 20))
    assert clipboard.text == 'hello'


def test_the_native_input_record_has_the_size_windows_expects():
    import ctypes
    assert ctypes.sizeof(ic._Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

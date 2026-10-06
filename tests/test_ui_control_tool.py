"""uiControl adapter: validation, redaction, referents and central safety, with the OS layer faked."""
import json
from dataclasses import replace

import pytest

from jarvis.platform.windows import ui_automation as ui
from jarvis.tools.confirmation import SafetyTier, evaluate_safety, get_confirmation_store

WINDOW = {'hwnd': 4242, 'process': 'outlook', 'title': 'Inbox - person@example.com', 'top_hwnd': 4241}


class FakeUi:
    """Stands in for the UIA layer; records every action it was asked to perform."""

    def __init__(self, monkeypatch, *, name='Apply', kind='button', password=False, texts=(), menu=None):
        self.calls = []
        self.info = {'name': name, 'type': kind, 'password': password, 'texts': list(texts), 'process': 'outlook'}
        # The menu's real item names along the requested path; None when they cannot be read without opening it.
        self.menu = menu
        monkeypatch.setattr(ui, 'inspect', self.inspect)
        monkeypatch.setattr(ui, 'act', self.act)
        monkeypatch.setattr(ui, 'snapshot', self.snapshot)
        monkeypatch.setattr(ui, 'menu_item_names', self.menu_item_names)

    def inspect(self, window='', element='', wants_texts=lambda name: False):
        info = dict(self.info)
        if not wants_texts(info['name']):
            info['texts'] = []
        return info

    def menu_item_names(self, window='', path=''):
        if self.menu is None:
            raise OSError('menu not readable')
        return list(self.menu)

    def act(self, action, window='', element='', value='', refuse_item=None):
        if action == 'menu' and self.menu is not None and refuse_item is not None:
            for requested, actual in zip(value.split(' > '), self.menu):
                if refuse_item(requested, actual):
                    raise ValueError(f'"{requested}" is the menu item "{actual}".')
        self.calls.append((action, window, element, value))
        return {'action': action, 'method': 'invoke', 'window': dict(WINDOW),
                'element': {'type': self.info['type'], 'name': self.info['name']}}

    def snapshot(self, window=''):
        self.calls.append(('snapshot', window, '', ''))
        return {'window': {k: v for k, v in WINDOW.items() if k != 'top_hwnd'},
                'elements': [{'id': 'e1', 'type': 'button', 'name': 'Send'}], 'texts': [], 'truncated': False}


@pytest.fixture
def cfg():
    from jarvis.config import load_settings
    return load_settings()


@pytest.fixture
def registry(cfg):
    from jarvis.tools import registry as reg
    original = dict(reg.BUILTIN_TOOLS)
    reg.configure_windows_tools(cfg, platform='win32', start_index=False)
    get_confirmation_store().cancel_pending()
    yield reg
    get_confirmation_store().cancel_pending()
    reg.BUILTIN_TOOLS.clear()
    reg.BUILTIN_TOOLS.update(original)


@pytest.fixture(autouse=True)
def fresh_referents():
    from jarvis.memory.desktop_referents import get_desktop_referents
    get_desktop_referents().clear()
    yield
    get_desktop_referents().clear()


def _run(registry, cfg, args):
    return registry.run_tool_with_retries(None, cfg, 'uiControl', args, '', '', '', language='en')


def test_tools_are_registered_with_the_windows_tools(registry, cfg):
    assert {'uiControl', 'pdfNavigate'} <= registry.BUILTIN_TOOLS.keys()
    registry.configure_windows_tools(replace(cfg, windows_tools_enabled=False), platform='win32')
    assert not {'uiControl', 'pdfNavigate'} & registry.BUILTIN_TOOLS.keys()


def test_descriptions_route_within_120_characters(registry):
    ui_desc = registry.BUILTIN_TOOLS['uiControl'].description[:120].casefold()
    pdf_desc = registry.BUILTIN_TOOLS['pdfNavigate'].description[:120].casefold()
    assert 'click' in ui_desc and 'menu' in ui_desc
    assert 'pdf' in pdf_desc and 'page' in pdf_desc


def test_snapshot_returns_redacted_json_and_records_nothing(registry, cfg, monkeypatch):
    from jarvis.memory.desktop_referents import get_desktop_referents
    fake = FakeUi(monkeypatch)
    result = _run(registry, cfg, {'action': 'snapshot'})
    assert result.success
    data = json.loads(result.reply_text)
    assert data['elements'][0]['id'] == 'e1'
    assert 'changed' in data['note'] and 'uiControl' in data['note']
    assert 'person@example.com' not in result.reply_text
    assert fake.calls == [('snapshot', '', '', '')]
    assert get_desktop_referents().recent(300) == []


def test_routine_click_runs_and_records_the_application_window(registry, cfg, monkeypatch):
    from jarvis.memory.desktop_referents import get_desktop_referents
    fake = FakeUi(monkeypatch, name='Apply')
    result = _run(registry, cfg, {'action': 'click', 'element': 'Apply'})
    assert result.success, result.error_message
    assert fake.calls == [('click', '', 'Apply', '')]
    assert 'top_hwnd' not in json.loads(result.reply_text)['window']
    [referent] = get_desktop_referents().recent(300)
    assert (referent.hwnd, referent.process, referent.last_action) == (4241, 'outlook', 'control')


@pytest.mark.parametrize('name', ['Send', 'Delete file', 'Buy now', 'Uninstall', "Don't Save", 'Submit order'])
def test_irreversible_controls_need_voice_confirmation(cfg, monkeypatch, name):
    FakeUi(monkeypatch, name=name)
    from jarvis.tools.builtin.windows import UiControlTool
    request = evaluate_safety('uiControl', {'action': 'click', 'element': name}, cfg, tool=UiControlTool(),
                              language='en')
    assert request.tier in (SafetyTier.CONFIRM_VOICE, SafetyTier.CONFIRM_DIALOG)
    assert name in request.action


@pytest.mark.parametrize('name', ['Apply', 'Format', 'Sender details', 'Payment history', 'Save'])
def test_ordinary_controls_are_safe(cfg, monkeypatch, name):
    FakeUi(monkeypatch, name=name)
    from jarvis.tools.builtin.windows import UiControlTool
    request = evaluate_safety('uiControl', {'action': 'click', 'element': name}, cfg, tool=UiControlTool(),
                              language='en')
    assert request.tier == SafetyTier.SAFE


def test_acceptance_in_a_destructive_dialog_needs_confirmation(cfg, monkeypatch):
    from jarvis.tools.builtin.windows import UiControlTool
    FakeUi(monkeypatch, name='Yes', texts=['Permanently delete 3 items?'])
    request = evaluate_safety('uiControl', {'action': 'click', 'element': 'Yes'}, cfg, tool=UiControlTool(),
                              language='en')
    assert request.tier == SafetyTier.CONFIRM_VOICE
    FakeUi(monkeypatch, name='Yes', texts=['Do you want to keep these settings?'])
    request = evaluate_safety('uiControl', {'action': 'click', 'element': 'Yes'}, cfg, tool=UiControlTool(),
                              language='en')
    assert request.tier == SafetyTier.SAFE


def test_irreversible_menu_paths_need_confirmation(cfg, monkeypatch):
    from jarvis.tools.builtin.windows import UiControlTool
    FakeUi(monkeypatch)
    tool = UiControlTool()
    risky = evaluate_safety('uiControl', {'action': 'menu', 'value': 'File > Delete file'}, cfg, tool=tool,
                            language='en')
    routine = evaluate_safety('uiControl', {'action': 'menu', 'value': 'Format > Font'}, cfg, tool=tool,
                              language='en')
    assert risky.tier == SafetyTier.CONFIRM_VOICE and routine.tier == SafetyTier.SAFE


def test_menu_paths_are_classified_by_the_real_item_names(cfg, monkeypatch):
    from jarvis.tools.builtin.windows import UiControlTool
    FakeUi(monkeypatch, menu=['File', 'Delete file'])
    request = evaluate_safety('uiControl', {'action': 'menu', 'value': 'File > file'}, cfg, tool=UiControlTool(),
                              language='en')
    assert request.tier == SafetyTier.CONFIRM_VOICE
    assert 'Delete file' in request.action
    FakeUi(monkeypatch, menu=['File', 'Save As...'])
    request = evaluate_safety('uiControl', {'action': 'menu', 'value': 'File > as'}, cfg, tool=UiControlTool(),
                              language='en')
    assert request.tier == SafetyTier.SAFE


def test_an_irreversible_menu_item_reached_by_partial_words_is_never_run(registry, cfg, monkeypatch):
    """Names that only appear once the menu is opened are checked again when the item is picked."""
    fake = FakeUi(monkeypatch, menu=['File', 'Delete Permanently'])
    monkeypatch.setattr(ui, 'menu_item_names', lambda *a, **k: ['File'])  # the submenu opens only on screen
    result = _run(registry, cfg, {'action': 'menu', 'value': 'File > Permanently'})
    assert not result.success
    assert 'Delete Permanently' in result.error_message
    assert fake.calls == []
    confirmed = _run(registry, cfg, {'action': 'menu', 'value': 'File > Delete Permanently'})
    assert not confirmed.success and 'confirmation' in confirmed.reply_text
    store = get_confirmation_store()
    store.handle_voice_response('yes', 'en')
    assert store.run_approved(store.claim_approved()).success
    assert fake.calls == [('menu', '', '', 'File > Delete Permanently')]


def test_password_fields_are_denied_centrally(registry, cfg, monkeypatch):
    fake = FakeUi(monkeypatch, name='Password', kind='edit', password=True)
    for args in ({'action': 'set_text', 'element': 'Password', 'value': 'x'}, {'action': 'read', 'element': 'e3'}):
        result = _run(registry, cfg, args)
        assert not result.success and 'prohibited' in result.error_message
    assert fake.calls == []


def test_confirmed_click_runs_only_after_yes(registry, cfg, monkeypatch):
    fake = FakeUi(monkeypatch, name='Send')
    first = _run(registry, cfg, {'action': 'click', 'element': 'Send'})
    assert not first.success and fake.calls == []
    assert first.reply_text == 'I need your confirmation to click "Send" in outlook. Say yes or no.'
    store = get_confirmation_store()
    store.handle_voice_response('yes', 'en')
    approved = store.claim_approved()
    assert approved is not None
    result = store.run_approved(approved)
    assert result.success and fake.calls == [('click', '', 'Send', '')]


def test_ambiguous_names_return_candidates_with_ids(registry, cfg, monkeypatch):
    FakeUi(monkeypatch)

    def ambiguous(*_args, **_kwargs):
        raise ui.AmbiguousElementError('2 controls match "Save"; choose one by id.',
                                       {'error': 'ambiguous', 'candidates': [{'id': 'e1', 'type': 'button', 'name': 'Save'},
                                                                             {'id': 'e2', 'type': 'menuitem', 'name': 'Save'}]})
    monkeypatch.setattr(ui, 'act', ambiguous)
    result = _run(registry, cfg, {'action': 'click', 'element': 'Save'})
    assert not result.success
    assert [c['id'] for c in json.loads(result.reply_text)['candidates']] == ['e1', 'e2']


def test_arguments_are_validated_before_touching_windows(registry, cfg, monkeypatch):
    fake = FakeUi(monkeypatch)
    for args in ({'action': 'wiggle'}, {'action': 'click'}, {'action': 'click', 'element': 'A', 'target': 'x'},
                 {'action': 'menu'}):
        result = _run(registry, cfg, args)
        assert not result.success, args
    assert fake.calls == []


def test_disabled_windows_tools_touch_nothing(cfg, monkeypatch):
    from jarvis.tools.builtin.windows import UiControlTool
    fake = FakeUi(monkeypatch)
    result = UiControlTool().execute(None, replace(cfg, windows_tools_enabled=False), {'action': 'snapshot'},
                                     '', '', '', 1, lambda _m: None)
    assert not result.success and 'windows_tools_enabled' in result.error_message
    assert fake.calls == []


def test_os_failures_are_reported_not_raised(registry, cfg, monkeypatch):
    FakeUi(monkeypatch)

    def broken(*_args, **_kwargs):
        raise OSError('UI Automation call failed (0x80004005).')
    monkeypatch.setattr(ui, 'act', broken)
    result = _run(registry, cfg, {'action': 'click', 'element': 'Apply'})
    assert not result.success and '0x80004005' in result.error_message

"""Pure parts of the UI Automation layer: name matching and element selection."""
from jarvis.platform.windows import ui_automation as ui


def _node(name, kind='button', offscreen=False, order=0):
    return ui._Node(element=None, path=(order,), runtime_id=(order,), kind=kind, name=name, enabled=True,
                    password=False, offscreen=offscreen, automation_id='', patterns=frozenset({'invoke'}),
                    value=None, readonly=True, toggle=None, expand=None, selected=None, order=order)


def test_names_ignore_case_access_keys_colons_ellipses_and_accelerators():
    assert ui.normalise_name('Save &As...\tCtrl+Shift+S') == 'save as'
    assert ui.normalise_name('Subject:') == 'subject'
    assert ui.normalise_name('Rock && Roll…') == 'rock & roll'


def test_exact_names_win_over_partial_ones_and_partials_need_every_word():
    names = ['Save', 'Save as', 'Don\'t save', 'Sauvegarder', 'Größe ändern']
    assert ui.match_names('save', names) == [0]
    assert ui.match_names('as', names) == [1]
    assert ui.match_names('größe', names) == [4]
    assert ui.match_names('save all', names) == []
    assert ui.match_names('', names) == []


def test_snapshots_prefer_on_screen_elements_but_keep_document_order():
    nodes = [_node(f'b{i}', offscreen=i % 2 == 0, order=i) for i in range(10)]
    chosen, capped = ui.choose_elements(nodes, cap=6)
    assert capped
    assert [n.name for n in chosen] == ['b0', 'b1', 'b3', 'b5', 'b7', 'b9']


def test_window_chrome_and_static_text_are_not_targets():
    assert not _node('Title', kind='titlebar').interactive
    text = _node('Hello', kind='text')
    text.patterns = frozenset()
    assert not text.interactive


def test_snapshot_ids_are_recognised_case_insensitively():
    assert ui.is_element_id('e12') and ui.is_element_id('E3')
    assert not ui.is_element_id('Save') and not ui.is_element_id('e')


def test_a_name_matched_by_several_differently_named_controls_is_never_picked():
    """'click message' with Delete message on screen and Forward message off screen asks which."""
    nodes = [_node('Delete message', order=0), _node('Forward message', offscreen=True, order=1)]
    assert [n.name for n in ui.name_matches('message', nodes)] == ['Delete message', 'Forward message']


def test_on_screen_enabled_controls_break_ties_between_identical_names():
    nodes = [_node('Save', offscreen=True, order=0), _node('Save', order=1)]
    assert [n.order for n in ui.name_matches('save', nodes)] == [1]


def test_an_exact_name_still_wins_over_partial_ones():
    nodes = [_node('Save', offscreen=True, order=0), _node('Save as', order=1)]
    assert [n.name for n in ui.name_matches('save', nodes)] == ['Save']


def test_no_match_is_empty():
    assert ui.name_matches('print', [_node('Save')]) == []


def test_a_menu_item_the_guard_refuses_is_named_instead_of_picked():
    import pytest
    items = ['Open', 'Delete Permanently']
    refuse = lambda requested, actual: actual == 'Delete Permanently' and requested != actual
    assert ui._pick_menu_item(items, items, 'open', refuse) == 'Open'
    with pytest.raises(ValueError, match='Delete Permanently'):
        ui._pick_menu_item(items, items, 'permanently', refuse)
    assert ui._pick_menu_item(items, items, 'Delete Permanently', refuse) == 'Delete Permanently'

"""localFiles find, move, copy and rename, path resolution and their safety tiers, in a fake home folder."""
import json
import os
import sys
from datetime import datetime
from unittest.mock import Mock

import pytest

from jarvis.tools.builtin import local_files as lf
from jarvis.tools.confirmation import SafetyTier, evaluate_safety, get_confirmation_store
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries


class Cfg:
    voice_debug = False


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home folder whose known folders are plain subfolders (as a redirected PC's would be)."""
    root = tmp_path / 'home'
    root.mkdir()
    original = os.path.expanduser

    def expand(p):
        if isinstance(p, str) and (p == '~' or p.startswith(('~/', '~\\'))):
            return str(root) + p[1:]
        return original(p)

    monkeypatch.setattr(os.path, 'expanduser', expand)
    known = {'documents': root / 'OneDrive' / 'Documents', 'downloads': root / 'Downloads',
             'desktop': root / 'OneDrive' / 'Desktop', 'pictures': root / 'Pictures', 'music': root / 'Music',
             'videos': root / 'Videos'}
    for folder in known.values():
        folder.mkdir(parents=True)
    monkeypatch.setattr(lf, 'known_folder', lambda name: str(known[name]))
    get_confirmation_store().clear_pending()
    yield root
    get_confirmation_store().clear_pending()


def make(path, content=b'x', mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def run(args):
    """Through the registry, so the central safety policy applies exactly as in a reply."""
    return run_tool_with_retries(db=None, cfg=Cfg(), tool_name='localFiles', tool_args=args, system_prompt='',
                                 original_prompt='', redacted_text='', max_retries=0)


def data(result):
    assert result.success, result.reply_text or result.error_message
    return json.loads(result.reply_text)


def tier(args):
    return evaluate_safety('localFiles', args, Cfg(), tool=BUILTIN_TOOLS['localFiles'], language='en').tier


# --- paths ----------------------------------------------------------------------------------

def test_a_relative_path_is_inside_the_home_folder_not_the_working_directory(home, tmp_path, monkeypatch):
    make(home / 'notes' / 'a.txt', b'hello')
    elsewhere = tmp_path / 'cwd'
    (elsewhere / 'notes').mkdir(parents=True)
    (elsewhere / 'notes' / 'a.txt').write_text('wrong file', encoding='utf-8')
    monkeypatch.chdir(elsewhere)
    assert run({'operation': 'read', 'path': 'notes/a.txt'}).reply_text == 'hello'


def test_known_folder_names_reach_the_real_folders_even_when_redirected(home):
    make(home / 'OneDrive' / 'Documents' / 'Taxes' / 'return.txt', b'2025')
    assert run({'operation': 'read', 'path': 'Documents/Taxes/return.txt'}).reply_text == '2025'
    assert 'return.txt' in run({'operation': 'list', 'path': 'documents', 'recursive': True}).reply_text


# --- find -----------------------------------------------------------------------------------

def test_find_lists_matching_files_with_full_paths_dates_and_sizes(home):
    make(home / 'Downloads' / 'lease.pdf', b'p' * 1234)
    make(home / 'Downloads' / 'photo.jpg')
    result = data(run({'operation': 'find', 'path': 'Downloads', 'type': 'pdf'}))
    assert result['action'] == 'found' and result['count'] == 1 and result['complete'] is True
    assert result['scope'] == str(home / 'Downloads')
    item = result['items'][0]
    assert item['name'] == 'lease.pdf' and item['path'] == str(home / 'Downloads' / 'lease.pdf')
    assert item['kind'] == 'file' and item['size'] == 1234
    assert datetime.strptime(item['date'], '%Y-%m-%d %H:%M').date() == datetime.now().date()


def test_find_by_date_reports_the_range_and_excludes_other_days(home):
    make(home / 'Downloads' / 'today.pdf')
    result = data(run({'operation': 'find', 'path': 'Downloads', 'when': 'yesterday'}))
    assert result['count'] == 0 and result['items'] == []
    assert set(result['range']) == {'after', 'before'}
    assert data(run({'operation': 'find', 'path': 'Downloads', 'when': 'today'}))['count'] == 1


def test_find_sorts_largest_first_and_honours_the_limit(home):
    for name, size in (('small.bin', 10), ('big.iso', 5000), ('mid.zip', 900)):
        make(home / 'Downloads' / name, b'x' * size)
    result = data(run({'operation': 'find', 'path': 'Downloads', 'sort': 'largest', 'limit': 2}))
    assert [item['name'] for item in result['items']] == ['big.iso', 'mid.zip'] and result['count'] == 3


def test_find_by_name_words_and_folder_type(home):
    (home / 'OneDrive' / 'Documents' / 'Tax Returns').mkdir()
    make(home / 'OneDrive' / 'Documents' / 'tax returns notes.txt')
    result = data(run({'operation': 'find', 'path': 'Documents', 'name': 'tax returns', 'type': 'folder'}))
    assert [(item['name'], item['kind']) for item in result['items']] == [('Tax Returns', 'folder')]


@pytest.mark.parametrize('args,words', [
    ({'operation': 'find'}, 'name'),
    ({'operation': 'find', 'path': 'Downloads', 'when': 'fortnight'}, 'when'),
    ({'operation': 'find', 'path': 'Downloads', 'sort': 'random'}, 'sort'),
    ({'operation': 'find', 'path': 'Downloads', 'limit': 0}, 'limit'),
    ({'operation': 'find', 'path': 'Missing Folder', 'type': 'pdf'}, 'not found'),
])
def test_invalid_finds_fail_with_a_reason(home, args, words):
    result = run(args)
    assert result.success is False and words in (result.reply_text or '').casefold()


@pytest.mark.skipif(sys.platform != 'win32', reason='the Windows index is only used on Windows')
def test_find_everywhere_without_an_index_says_to_name_a_folder(home):
    result = run({'operation': 'find', 'type': 'pdf'})
    assert result.success is False and 'folder' in result.reply_text.casefold()


def test_find_is_safe(home):
    assert tier({'operation': 'find', 'type': 'pdf'}) == SafetyTier.SAFE


# --- move, copy, rename ---------------------------------------------------------------------

def test_move_into_a_known_folder_keeps_the_name(home):
    source = make(home / 'Downloads' / 'invoice.pdf', b'inv')
    result = data(run({'operation': 'move', 'path': str(source), 'destination': 'Documents'}))
    target = home / 'OneDrive' / 'Documents' / 'invoice.pdf'
    assert result == {'action': 'moved', 'kind': 'file', 'from': str(source), 'to': str(target)}
    assert not source.exists() and target.read_bytes() == b'inv'


def test_move_creates_a_missing_destination_folder_and_can_rename(home):
    source = make(home / 'Downloads' / 'scan.pdf')
    result = data(run({'operation': 'move', 'path': str(source), 'destination': 'Documents/Taxes/2025',
                       'new_name': 'return'}))
    assert result['to'] == str(home / 'OneDrive' / 'Documents' / 'Taxes' / '2025' / 'return.pdf')
    assert (home / 'OneDrive' / 'Documents' / 'Taxes' / '2025' / 'return.pdf').exists()


def test_copy_leaves_the_original(home):
    source = make(home / 'Downloads' / 'report.docx', b'doc')
    result = data(run({'operation': 'copy', 'path': str(source), 'destination': 'Desktop'}))
    assert result['action'] == 'copied' and source.exists()
    assert (home / 'OneDrive' / 'Desktop' / 'report.docx').read_bytes() == b'doc'


def test_copy_of_a_folder_copies_its_contents(home):
    make(home / 'Projects' / 'site' / 'index.html', b'<html>')
    data(run({'operation': 'copy', 'path': '~/Projects/site', 'destination': '~/Backups'}))
    assert (home / 'Backups' / 'site' / 'index.html').read_bytes() == b'<html>'
    assert (home / 'Projects' / 'site' / 'index.html').exists()


def test_rename_keeps_the_extension_when_the_new_name_has_none(home):
    source = make(home / 'OneDrive' / 'Desktop' / 'Screenshot 2026-10-05 141207.png')
    result = data(run({'operation': 'rename', 'path': str(source), 'new_name': 'wiring diagram'}))
    assert result['action'] == 'renamed'
    assert result['to'] == str(home / 'OneDrive' / 'Desktop' / 'wiring diagram.png')
    assert (home / 'OneDrive' / 'Desktop' / 'wiring diagram.png').exists() and not source.exists()


def test_rename_with_a_new_extension_uses_it(home):
    source = make(home / 'notes.txt')
    assert data(run({'operation': 'rename', 'path': str(source), 'new_name': 'notes.md'}))['to'].endswith('notes.md')


def test_a_rename_that_only_changes_letter_case_works(home):
    source = make(home / 'report.pdf')
    data(run({'operation': 'rename', 'path': str(source), 'new_name': 'Report.pdf'}))
    assert [p.name for p in home.iterdir() if p.is_file()] == ['Report.pdf']


@pytest.mark.parametrize('operation', ['move', 'copy'])
def test_nothing_is_ever_replaced(home, operation):
    source = make(home / 'Downloads' / 'a.txt', b'new')
    existing = make(home / 'OneDrive' / 'Documents' / 'a.txt', b'old')
    result = run({'operation': operation, 'path': str(source), 'destination': 'Documents'})
    assert result.success is False and 'already exists' in result.reply_text.casefold()
    assert existing.read_bytes() == b'old' and source.exists()


def test_rename_onto_an_existing_name_is_refused(home):
    source = make(home / 'a.txt', b'a')
    make(home / 'b.txt', b'b')
    result = run({'operation': 'rename', 'path': str(source), 'new_name': 'b'})
    assert result.success is False and (home / 'b.txt').read_bytes() == b'b'


@pytest.mark.parametrize('new_name', ['', '..', 'a/b', 'a\\b', 'what?', 'name.', 'CON', 'x:y'])
def test_unusable_new_names_are_refused(home, new_name):
    source = make(home / 'a.txt')
    result = run({'operation': 'rename', 'path': str(source), 'new_name': new_name})
    assert result.success is False and source.exists()


def test_a_folder_cannot_move_into_itself(home):
    (home / 'Projects' / 'site').mkdir(parents=True)
    result = run({'operation': 'move', 'path': '~/Projects', 'destination': '~/Projects/site'})
    assert result.success is False and (home / 'Projects' / 'site').is_dir()


def test_moving_an_item_to_where_it_already_is_is_refused(home):
    source = make(home / 'Downloads' / 'a.txt')
    result = run({'operation': 'move', 'path': str(source), 'destination': 'Downloads'})
    assert result.success is False and source.exists()


@pytest.mark.parametrize('args', [
    {'operation': 'move', 'path': '~/missing.txt', 'destination': 'Documents'},
    {'operation': 'move', 'path': '~/a.txt'},
    {'operation': 'rename', 'path': '~/a.txt'},
])
def test_missing_sources_and_arguments_fail(home, args):
    make(home / 'a.txt')
    assert run(args).success is False


def test_changes_outside_the_home_folder_are_refused(home, tmp_path):
    outside = make(tmp_path / 'outside' / 'a.txt')
    assert run({'operation': 'move', 'path': str(outside), 'destination': 'Documents'}).success is False
    source = make(home / 'b.txt')
    assert run({'operation': 'copy', 'path': str(source), 'destination': str(tmp_path / 'outside')}).success is False
    assert outside.exists() and not (tmp_path / 'outside' / 'b.txt').exists()


def test_large_copies_are_refused_before_anything_is_written(home, monkeypatch):
    monkeypatch.setattr(lf, 'MAX_TRANSFER_BYTES', 1000)
    source = make(home / 'Videos' / 'film.mp4', b'v' * 2000)
    result = run({'operation': 'copy', 'path': str(source), 'destination': 'Desktop'})
    assert result.success is False and 'too large' in result.reply_text.casefold()
    assert not (home / 'OneDrive' / 'Desktop' / 'film.mp4').exists()


def test_copies_of_folders_with_too_many_items_are_refused(home, monkeypatch):
    monkeypatch.setattr(lf, 'MAX_TRANSFER_ITEMS', 3)
    for index in range(5):
        make(home / 'Pictures' / 'trip' / f'{index}.jpg')
    result = run({'operation': 'copy', 'path': '~/Pictures/trip', 'destination': 'Desktop'})
    assert result.success is False and not (home / 'OneDrive' / 'Desktop' / 'trip').exists()


def test_move_copy_and_rename_need_no_confirmation(home):
    source = make(home / 'Downloads' / 'a.txt')
    assert tier({'operation': 'move', 'path': str(source), 'destination': 'Documents'}) == SafetyTier.SAFE
    assert tier({'operation': 'copy', 'path': str(source), 'destination': 'Documents'}) == SafetyTier.SAFE
    assert tier({'operation': 'rename', 'path': str(source), 'new_name': 'b'}) == SafetyTier.SAFE


def test_moving_out_of_or_into_an_important_location_needs_the_desktop_dialog(home):
    key = make(home / '.ssh' / 'id_ed25519')
    assert tier({'operation': 'move', 'path': str(key), 'destination': 'Documents'}) == SafetyTier.CONFIRM_DIALOG
    assert tier({'operation': 'rename', 'path': str(key), 'new_name': 'old key'}) == SafetyTier.CONFIRM_DIALOG
    note = make(home / 'note.txt')
    assert tier({'operation': 'copy', 'path': str(note), 'destination': '~/.ssh'}) == SafetyTier.CONFIRM_DIALOG
    assert tier({'operation': 'move', 'path': str(note), 'destination': '~/.config/app'}) == SafetyTier.CONFIRM_DIALOG


def test_without_a_desktop_the_important_move_does_not_happen(home):
    key = make(home / '.ssh' / 'id_ed25519')
    result = run({'operation': 'move', 'path': str(key), 'destination': 'Documents'})
    assert result.success is False and key.exists()


def test_results_are_printed_with_an_emoji_line(home):
    source = make(home / 'Downloads' / 'a.txt')
    context = Mock()
    lf.LocalFilesTool().run({'operation': 'find', 'path': str(home / 'Downloads'), 'name': 'a'}, context)
    lf.LocalFilesTool().run({'operation': 'rename', 'path': str(source), 'new_name': 'b'}, context)
    lines = [call.args[0] for call in context.user_print.call_args_list]
    assert len(lines) == 2 and all(not line[0].isascii() for line in lines)

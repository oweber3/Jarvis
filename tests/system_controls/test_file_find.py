"""Finding files by name, type and date: ranges, types, the folder scan and ranking, with indexes replaced."""
from datetime import datetime
import os
import time

import pytest

from jarvis.platform import file_find as ff
from jarvis.platform.file_find import FindCriteria, Found

# A fixed local "now": Wednesday 2026-10-07 15:30.
NOW = datetime(2026, 10, 7, 15, 30)


def ts(text):
    return datetime.fromisoformat(text).timestamp()


def found(path, date='2026-10-01 12:00', kind='file', size=100, created=None):
    return Found(path, kind, modified=ts(date), created=ts(created) if created else 0.0, size=size)


class StaticBackend:
    name = 'static'

    def __init__(self, items, complete=True):
        self.items, self.complete, self.criteria = items, complete, []

    def find(self, criteria, limit):
        self.criteria.append(criteria)
        return list(self.items), self.complete


class Broken:
    name = 'broken'

    def find(self, criteria, limit):
        raise ff.SearchUnavailable('index offline')


# --- dates ---------------------------------------------------------------------------------

@pytest.mark.parametrize('when,after,before', [
    ('today', '2026-10-07 00:00', '2026-10-08 00:00'),
    ('yesterday', '2026-10-06 00:00', '2026-10-07 00:00'),
    ('this_week', '2026-10-05 00:00', '2026-10-12 00:00'),
    ('last_week', '2026-09-28 00:00', '2026-10-05 00:00'),
    ('this_month', '2026-10-01 00:00', '2026-11-01 00:00'),
    ('last_month', '2026-09-01 00:00', '2026-10-01 00:00'),
    ('this_year', '2026-01-01 00:00', '2027-01-01 00:00'),
])
def test_named_ranges_are_whole_local_days_weeks_months_and_years(when, after, before):
    assert ff.date_range(when=when, now=NOW) == (ts(after), ts(before))


def test_last_month_in_january_is_december_of_the_previous_year():
    assert ff.date_range(when='last_month', now=datetime(2027, 1, 15)) == (ts('2026-12-01 00:00'), ts('2027-01-01 00:00'))


def test_rolling_ranges_count_back_from_now_with_no_end():
    assert ff.date_range(when='last_7_days', now=NOW) == (ts('2026-09-30 15:30'), None)
    assert ff.date_range(when='last_30_days', now=NOW) == (ts('2026-09-07 15:30'), None)


def test_explicit_dates_and_times_are_local_and_before_is_exclusive():
    assert ff.date_range(after='2026-03-01', before='2026-04-01', now=NOW) == (ts('2026-03-01 00:00'), ts('2026-04-01 00:00'))
    assert ff.date_range(after='2026-03-01T09:15', now=NOW) == (ts('2026-03-01 09:15'), None)
    assert ff.date_range(now=NOW) == (None, None)


@pytest.mark.parametrize('kwargs', [
    {'when': 'someday'}, {'when': 'today', 'after': '2026-01-01'}, {'after': 'March 1st'},
    {'after': '2026-05-01', 'before': '2026-04-01'},
])
def test_invalid_or_contradictory_dates_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ff.date_range(now=NOW, **kwargs)


def test_an_items_date_is_the_later_of_created_and_modified():
    copied_in = found(r'C:\h\a.pdf', date='2024-01-01 10:00', created='2026-10-06 09:00')
    edited = found(r'C:\h\b.pdf', date='2026-10-06 09:00', created='2024-01-01 10:00')
    assert copied_in.date == edited.date == ts('2026-10-06 09:00')


# --- types ---------------------------------------------------------------------------------

def test_a_category_names_its_extensions_and_returns_files_only():
    extensions, kind = ff.parse_type('Document')
    assert '.pdf' in extensions and '.docx' in extensions and kind == 'file'


def test_folder_type_returns_folders_only():
    assert ff.parse_type('folder') == (frozenset(), 'folder')


def test_extensions_may_be_listed_with_or_without_dots():
    assert ff.parse_type('PDF') == (frozenset({'.pdf'}), 'file')
    assert ff.parse_type('.docx, xlsx') == (frozenset({'.docx', '.xlsx'}), 'file')
    assert ff.parse_type(None) == (frozenset(), None)


@pytest.mark.parametrize('text', ['', 'p d f', '*.pdf', '../x'])
def test_unusable_types_are_rejected(text):
    with pytest.raises(ValueError):
        ff.parse_type(text)


# --- ranking and filtering -----------------------------------------------------------------

def test_results_are_filtered_by_every_criterion_and_sorted_newest_first():
    items = [found(r'C:\h\Downloads\lease.pdf', '2026-10-06 15:00'), found(r'C:\h\Downloads\old.pdf', '2026-09-01 10:00'),
             found(r'C:\h\Downloads\photo.jpg', '2026-10-06 16:00'), found(r'C:\h\Downloads\lease2.pdf', '2026-10-06 18:00')]
    after, before = ff.date_range(when='yesterday', now=NOW)
    criteria = FindCriteria(extensions=frozenset({'.pdf'}), kind='file', after=after, before=before)
    outcome = ff.find(criteria, StaticBackend(items))
    assert [item.name for item in outcome.items] == ['lease2.pdf', 'lease.pdf']
    assert outcome.count == 2 and outcome.complete is True and outcome.backend == 'static'


def test_name_words_must_all_appear_in_the_name():
    items = [found(r'C:\h\tax return 2025.pdf'), found(r'C:\h\return label.pdf'), found(r'C:\h\taxes\notes.txt')]
    outcome = ff.find(FindCriteria(tokens=('tax', 'return')), StaticBackend(items))
    assert [item.name for item in outcome.items] == ['tax return 2025.pdf']


def test_largest_and_oldest_sorting():
    items = [found(r'C:\h\a.iso', '2026-01-01 00:00', size=5_000), found(r'C:\h\b.zip', '2026-02-01 00:00', size=9_000),
             found(r'C:\h\c', '2025-01-01 00:00', kind='folder', size=None)]
    largest = ff.find(FindCriteria(scope=r'C:\h', sort='largest'), StaticBackend(items))
    assert [item.name for item in largest.items] == ['b.zip', 'a.iso', 'c']
    oldest = ff.find(FindCriteria(scope=r'C:\h', sort='oldest'), StaticBackend(items))
    assert [item.name for item in oldest.items] == ['c', 'a.iso', 'b.zip']


def test_the_limit_caps_items_but_count_reports_every_match():
    items = [found(rf'C:\h\scan {i}.pdf', f'2026-10-0{i + 1} 10:00') for i in range(6)]
    outcome = ff.find(FindCriteria(tokens=('scan',), limit=2), StaticBackend(items))
    assert outcome.count == 6 and [item.name for item in outcome.items] == ['scan 5.pdf', 'scan 4.pdf']


def test_a_scope_keeps_only_items_below_it():
    items = [found(r'C:\h\Downloads\a.pdf'), found(r'C:\h\Downloads2\b.pdf'), found(r'C:\h\Downloads\sub\c.pdf')]
    outcome = ff.find(FindCriteria(scope=r'C:\h\Downloads', extensions=frozenset({'.pdf'}), kind='file'),
                      StaticBackend(items))
    assert sorted(item.name for item in outcome.items) == ['a.pdf', 'c.pdf']


@pytest.mark.parametrize('path', [r'C:\Users\you\AppData\Local\Temp\scan.pdf', r'C:\Users\you\p\node_modules\x\scan.pdf',
                                  r'C:\$Recycle.Bin\S-1\scan.pdf'])
def test_noise_locations_are_never_results(path):
    outcome = ff.find(FindCriteria(tokens=('scan',)), StaticBackend([found(path), found(r'C:\Users\you\scan.pdf')]))
    assert [item.path for item in outcome.items] == [r'C:\Users\you\scan.pdf']


def test_programs_are_results_when_finding():
    outcome = ff.find(FindCriteria(tokens=('setup',)), StaticBackend([found(r'C:\Users\you\Downloads\setup.exe')]))
    assert outcome.count == 1


def test_an_incomplete_backend_answer_is_reported():
    assert ff.find(FindCriteria(tokens=('scan',)), StaticBackend([], complete=False)).complete is False


def test_a_failing_backend_falls_through_and_all_failing_is_unavailable():
    outcome = ff.find(FindCriteria(tokens=('scan',)), [Broken(), StaticBackend([found(r'C:\h\scan.pdf')])])
    assert outcome.count == 1 and outcome.backend == 'static'
    with pytest.raises(ff.SearchUnavailable):
        ff.find(FindCriteria(tokens=('scan',)), [Broken()])


def test_a_find_with_no_criterion_and_no_scope_is_refused():
    with pytest.raises(ValueError):
        ff.find(FindCriteria(), StaticBackend([]))


# --- the folder scan -----------------------------------------------------------------------

def make(root, relative, content=b'x', mtime=None):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_the_scan_finds_matching_files_below_the_scope(tmp_path):
    make(tmp_path, 'Downloads/lease.pdf', b'a' * 300)
    make(tmp_path, 'Downloads/receipts/march.pdf')
    make(tmp_path, 'Downloads/photo.jpg')
    make(tmp_path, 'Documents/other.pdf')
    criteria = FindCriteria(scope=str(tmp_path / 'Downloads'), extensions=frozenset({'.pdf'}), kind='file')
    outcome = ff.find(criteria, ff.ScanBackend())
    assert sorted(item.name for item in outcome.items) == ['lease.pdf', 'march.pdf']
    lease = next(item for item in outcome.items if item.name == 'lease.pdf')
    assert lease.size == 300 and lease.kind == 'file' and outcome.complete is True


def test_the_scan_reports_folders_and_skips_noise_folders(tmp_path):
    (tmp_path / 'Projects' / 'Taxes').mkdir(parents=True)
    make(tmp_path, 'Projects/app/node_modules/taxes/index.js')
    outcome = ff.find(FindCriteria(scope=str(tmp_path), tokens=('taxes',)), ff.ScanBackend())
    assert [(item.name, item.kind) for item in outcome.items] == [('Taxes', 'folder')]


def test_the_scan_dates_items_by_their_latest_timestamp(tmp_path):
    future = time.time() + 3 * 86400
    make(tmp_path, 'notes.txt', mtime=future)
    outcome = ff.find(FindCriteria(scope=str(tmp_path), tokens=('notes',)), ff.ScanBackend())
    assert outcome.items[0].date == pytest.approx(future, abs=2)


def test_the_scan_stops_at_its_entry_budget_and_says_it_is_incomplete(tmp_path):
    for index in range(30):
        make(tmp_path, f'f{index}.txt')
    outcome = ff.find(FindCriteria(scope=str(tmp_path), extensions=frozenset({'.txt'}), kind='file'),
                      ff.ScanBackend(max_entries=10))
    assert outcome.complete is False and outcome.count <= 10


def test_the_scan_needs_an_existing_scope(tmp_path):
    with pytest.raises(ff.SearchUnavailable):
        ff.find(FindCriteria(scope=str(tmp_path / 'missing'), tokens=('x',)), ff.ScanBackend())

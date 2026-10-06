"""Open-anything search: ranking, ambiguity and the two backends, with the indexes replaced."""
import pytest

from jarvis.platform.windows import file_search as fs
from jarvis.platform.windows.file_search import FileMatch


def item(path, modified=0.0, kind='file'):
    return FileMatch(path, kind, modified)


class StaticBackend:
    name = 'static'

    def __init__(self, matches):
        self.matches, self.queries = matches, []

    def search(self, tokens, limit):
        self.queries.append(tokens)
        return list(self.matches)


def test_a_query_is_split_into_letter_and_digit_tokens():
    assert fs.query_tokens("Open my Proposal_final-v2, please!") == ['open', 'my', 'proposal', 'final', 'v2', 'please']
    assert fs.query_tokens('  ') == []


def test_a_single_exact_name_match_is_chosen_even_when_others_contain_the_words():
    matches = [item(r'C:\Users\you\Documents\proposal.docx', 5), item(r'C:\Users\you\Documents\proposal notes.txt', 9)]
    outcome = fs.resolve('proposal', StaticBackend(matches))
    assert outcome.chosen and outcome.chosen.path.endswith('proposal.docx')


def test_exact_name_ignores_case_extension_and_separators():
    matches = [item(r'C:\x\Final_Report.PDF', 1), item(r'C:\x\final report backup.pdf', 2)]
    assert fs.resolve('final report', StaticBackend(matches)).chosen.path.endswith('Final_Report.PDF')


def test_several_exact_name_matches_are_candidates_ordered_by_recency_never_guessed():
    matches = [item(r'C:\a\proposal.docx', 1), item(r'C:\b\proposal.docx', 9), item(r'C:\c\proposal.docx', 5)]
    outcome = fs.resolve('proposal', StaticBackend(matches))
    assert outcome.chosen is None
    assert [m.path for m in outcome.candidates] == [r'C:\b\proposal.docx', r'C:\c\proposal.docx', r'C:\a\proposal.docx']


def test_one_partial_match_is_chosen():
    outcome = fs.resolve('proposal', StaticBackend([item(r'C:\Users\you\Documents\Proposal Final v3.docx', 3)]))
    assert outcome.chosen.path.endswith('Proposal Final v3.docx')


def test_several_partial_matches_are_candidates_with_a_cap():
    matches = [item(rf'C:\Users\you\Documents\proposal draft {i}.docx', i) for i in range(12)]
    outcome = fs.resolve('proposal', StaticBackend(matches))
    assert outcome.chosen is None and len(outcome.candidates) == fs.MAX_CANDIDATES
    assert outcome.candidates[0].modified == 11


def test_no_match_is_reported():
    outcome = fs.resolve('nothing here', StaticBackend([]))
    assert outcome.chosen is None and outcome.candidates == []


@pytest.mark.parametrize('path', [
    r'C:\Users\you\AppData\Local\Temp\proposal.tmp', r'C:\Users\you\proj\node_modules\proposal\index.js',
    r'C:\Users\you\proj\.git\objects\proposal', r'C:\$Recycle.Bin\S-1-5\proposal.docx',
    r'C:\Users\you\.mamba_env\Lib\site-packages\proposal.py',
])
def test_noise_locations_are_never_offered(path):
    outcome = fs.resolve('proposal', StaticBackend([item(path), item(r'C:\Users\you\Documents\proposal.docx')]))
    assert outcome.chosen.path == r'C:\Users\you\Documents\proposal.docx'


@pytest.mark.parametrize('name', ['proposal.exe', 'proposal.bat', 'proposal.ps1', 'proposal.lnk', 'proposal.py', 'proposal.msi'])
def test_programs_and_scripts_are_not_results_for_opening(name):
    outcome = fs.resolve('proposal', StaticBackend([item(rf'C:\Users\you\Documents\{name}')]))
    assert outcome.chosen is None and outcome.candidates == []
    assert outcome.programs_excluded is True


def test_folders_are_results_too():
    outcome = fs.resolve('proposal', StaticBackend([item(r'C:\Users\you\Documents\Proposal', 4, kind='folder')]))
    assert outcome.chosen.kind == 'folder'


def test_queries_without_usable_words_are_rejected_before_searching():
    backend = StaticBackend([])
    for query in ['', '  ', '!!', 'a']:
        with pytest.raises(ValueError):
            fs.resolve(query, backend)
    assert backend.queries == []


def test_the_search_receives_the_query_tokens():
    backend = StaticBackend([])
    fs.resolve('my proposal', backend)
    assert backend.queries == [['my', 'proposal']]


# --- Windows Search ---------------------------------------------------------------

def test_windows_search_sql_escapes_every_token_and_orders_by_recency():
    sql = fs.windows_search_sql(['proposal', "o'brien"], limit=50)
    assert "TOP 50" in sql and 'FROM SystemIndex' in sql
    assert "System.ItemName LIKE '%proposal%'" in sql
    assert "System.ItemName LIKE '%o''brien%'" in sql
    assert sql.index('ORDER BY System.DateModified DESC') > sql.index('WHERE')


def test_windows_search_rows_become_matches():
    from datetime import datetime, timezone
    rows = [(r'C:\Users\you\Documents\proposal.docx', datetime(2026, 1, 2, tzinfo=timezone.utc), False),
            (r'C:\Users\you\Documents\Proposal', datetime(2026, 1, 1, tzinfo=timezone.utc), True)]
    backend = fs.WindowsSearchBackend(execute=lambda sql: rows)
    matches = backend.search(['proposal'], 50)
    assert [(m.path, m.kind) for m in matches] == [
        (r'C:\Users\you\Documents\proposal.docx', 'file'), (r'C:\Users\you\Documents\Proposal', 'folder')]
    assert matches[0].modified > matches[1].modified


# --- Everything ------------------------------------------------------------------

class FakeEverything:
    """Records the SDK calls the backend makes and replays canned results."""

    def __init__(self, results):
        self.results, self.calls = results, []

    def Everything_SetSearchW(self, text):
        self.calls.append(('search', text))

    def Everything_SetMax(self, count):
        self.calls.append(('max', count))

    def Everything_SetSort(self, kind):
        self.calls.append(('sort', kind))

    def Everything_SetRequestFlags(self, flags):
        self.calls.append(('flags', flags))

    def Everything_QueryW(self, wait):
        self.calls.append(('query', wait))
        return 1

    def Everything_GetNumResults(self):
        return len(self.results)

    def full_path(self, index):
        return self.results[index][0]

    def is_folder(self, index):
        return self.results[index][1]

    def modified(self, index):
        return self.results[index][2]


def test_everything_backend_queries_with_quoted_tokens_and_reads_results():
    sdk = FakeEverything([(r'C:\Users\you\proposal.docx', False, 1700000000.0), (r'C:\Users\you\Proposal', True, 1600000000.0)])
    backend = fs.EverythingBackend(sdk)
    matches = backend.search(['proposal', 'final'], 20)
    assert ('search', '"proposal" "final"') in sdk.calls and ('max', 20) in sdk.calls
    assert [(m.path, m.kind) for m in matches] == [(r'C:\Users\you\proposal.docx', 'file'), (r'C:\Users\you\Proposal', 'folder')]
    assert matches[0].modified == 1700000000.0


def test_backend_choice_prefers_everything_when_it_is_running_and_falls_back_to_windows_search():
    sdk = FakeEverything([])
    chosen = fs.choose_backends(everything_running=lambda: True, everything_sdk=lambda: sdk)
    assert [b.name for b in chosen] == ['everything', 'windows-search']
    chosen = fs.choose_backends(everything_running=lambda: False, everything_sdk=lambda: sdk)
    assert [b.name for b in chosen] == ['windows-search']
    chosen = fs.choose_backends(everything_running=lambda: True, everything_sdk=lambda: None)
    assert [b.name for b in chosen] == ['windows-search']


def test_a_failing_backend_falls_through_to_the_next_one():
    class Broken:
        name = 'broken'

        def search(self, tokens, limit):
            raise fs.SearchUnavailable('index offline')

    good = StaticBackend([item(r'C:\Users\you\Documents\proposal.docx')])
    outcome = fs.resolve('proposal', [Broken(), good])
    assert outcome.chosen.path.endswith('proposal.docx')


def test_when_every_backend_fails_the_error_says_search_is_unavailable():
    class Broken:
        name = 'broken'

        def search(self, tokens, limit):
            raise fs.SearchUnavailable('index offline')

    with pytest.raises(fs.SearchUnavailable):
        fs.resolve('proposal', [Broken()])


@pytest.mark.integration
def test_the_real_windows_search_index_answers_a_read_only_query():
    backend = fs.WindowsSearchBackend()
    matches = backend.search(['jarvis'], 5)
    assert isinstance(matches, list) and all(isinstance(m.path, str) and m.path for m in matches)


# --- find: searching by name, type and date ----------------------------------------------

def _criteria(**kwargs):
    from jarvis.platform.file_find import FindCriteria
    return FindCriteria(**kwargs)


def test_windows_find_sql_combines_tokens_extensions_kind_and_the_date_range_in_utc():
    from datetime import datetime, timezone
    after = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc).timestamp()
    before = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc).timestamp()
    sql = fs.windows_find_sql(_criteria(tokens=('lease',), extensions=frozenset({'.pdf', '.docx'}), kind='file',
                                        after=after, before=before), limit=500)
    assert 'TOP 500' in sql and 'System.DateCreated' in sql and 'System.Size' in sql
    assert "System.ItemName LIKE '%lease%'" in sql
    assert "System.FileExtension = '.docx'" in sql and "System.FileExtension = '.pdf'" in sql and ' OR ' in sql
    assert 'System.IsFolder = FALSE' in sql
    # The later of created and modified falls in [after, before).
    assert ("(System.DateModified >= '2026-10-06 00:00:00' OR System.DateCreated >= '2026-10-06 00:00:00')" in sql)
    assert "System.DateModified < '2026-10-07 00:00:00' AND System.DateCreated < '2026-10-07 00:00:00'" in sql
    assert sql.endswith('ORDER BY System.DateModified DESC')


def test_windows_find_sql_orders_by_the_requested_sort_and_selects_folders():
    assert fs.windows_find_sql(_criteria(kind='folder', sort='oldest'), 10).endswith('ORDER BY System.DateModified ASC')
    assert 'System.IsFolder = TRUE' in fs.windows_find_sql(_criteria(kind='folder'), 10)
    assert fs.windows_find_sql(_criteria(tokens=('x',), sort='largest'), 10).endswith('ORDER BY System.Size DESC')


def test_windows_search_find_rows_carry_created_and_size_and_a_full_page_is_incomplete():
    from datetime import datetime, timezone
    from decimal import Decimal
    rows = [(r'C:\Users\you\Downloads\lease.pdf', datetime(2026, 10, 6, 9, tzinfo=timezone.utc), False,
             datetime(2026, 10, 6, 8, tzinfo=timezone.utc), Decimal(482113)),
            (r'C:\Users\you\Taxes', datetime(2026, 1, 1, tzinfo=timezone.utc), True,
             datetime(2025, 1, 1, tzinfo=timezone.utc), None)]
    backend = fs.WindowsSearchBackend(execute=lambda sql: rows)
    items, complete = backend.find(_criteria(tokens=('lease',)), 2)
    assert [(i.path, i.kind, i.size) for i in items] == [
        (r'C:\Users\you\Downloads\lease.pdf', 'file', 482113), (r'C:\Users\you\Taxes', 'folder', None)]
    assert items[0].created < items[0].modified and complete is False
    assert backend.find(_criteria(tokens=('lease',)), 3)[1] is True


class FakeEverythingFind(FakeEverything):
    """Everything results with creation dates and sizes: (path, is_folder, modified, created, size)."""

    def created(self, index):
        return self.results[index][3]

    def size(self, index):
        return self.results[index][4]


def test_everything_find_sends_quoted_tokens_extensions_kind_and_scope():
    sdk = FakeEverythingFind([(r'C:\Users\you\Downloads\lease.pdf', False, 1700000000.0, 1690000000.0, 482113)])
    backend = fs.EverythingBackend(sdk)
    items, complete = backend.find(_criteria(tokens=('lease',), extensions=frozenset({'.pdf', '.docx'}), kind='file',
                                             scope=r'C:\Users\you\Downloads'), 1000)
    search = next(value for call, value in sdk.calls if call == 'search')
    assert '"lease"' in search and 'ext:docx;pdf' in search and 'file:' in search
    assert '"C:\\Users\\you\\Downloads\\"' in search
    assert ('sort', fs.EverythingBackend._SORT_DATE_MODIFIED_DESCENDING) in sdk.calls
    assert [(i.path, i.created, i.size) for i in items] == [(r'C:\Users\you\Downloads\lease.pdf', 1690000000.0, 482113)]
    assert complete is True


def test_everything_find_sorts_by_size_for_largest_and_a_full_page_is_incomplete():
    sdk = FakeEverythingFind([(r'C:\a.iso', False, 1.0, 1.0, 9), (r'C:\b.iso', False, 1.0, 1.0, 5)])
    items, complete = fs.EverythingBackend(sdk).find(_criteria(kind='folder', sort='largest'), 2)
    assert ('sort', fs.EverythingBackend._SORT_SIZE_DESCENDING) in sdk.calls and complete is False
    assert 'folder:' in next(value for call, value in sdk.calls if call == 'search')


def test_find_backends_scan_a_named_folder_and_use_windows_search_everywhere():
    scoped = fs.find_backends(scoped=True, everything_running=lambda: False)
    assert [b.name for b in scoped] == ['scan']
    unscoped = fs.find_backends(scoped=False, everything_running=lambda: False)
    assert [b.name for b in unscoped] == ['windows-search']
    sdk = FakeEverythingFind([])
    assert [b.name for b in fs.find_backends(scoped=True, everything_running=lambda: True,
                                             everything_sdk=lambda: sdk)] == ['everything', 'scan']


@pytest.mark.integration
def test_the_real_windows_search_index_answers_a_read_only_find():
    from jarvis.platform.file_find import find
    outcome = find(_criteria(extensions=frozenset({'.pdf'}), kind='file', limit=3), [fs.WindowsSearchBackend()])
    assert all(item.path.casefold().endswith('.pdf') and item.size is not None for item in outcome.items)

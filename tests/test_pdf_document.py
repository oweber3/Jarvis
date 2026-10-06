"""Local PDF reading: outline, keyword search with snippets, bounded extraction."""
import time

import pytest

from pdf_fixtures import make_pdf

PAGES = [
    'Gardening for Beginners\nPreface and how to use this book.',
    'Chapter 1 Introduction\nTools, seasons and soil.',
    'Chapter 2 Soil Preparation\nDigging, composting and mulching the soil.',
    'Soil preparation timings are tabulated here.\nThe raised bed example.',
    'Chapter 3 Plant Care\nA seedling problem appears in spring.',
    'More on pots. The seedlings of a tomato plant are fragile.',
    'References\nSmith, J. Garden Systems.',
]
OUTLINE = [
    ('Preface', 0),
    ('Chapter 1 Introduction', 1),
    ('Chapter 2 Soil Preparation', 2, [('2.1 Timings', 3)]),
    ('Chapter 3 Plant Care', 4),
    ('References', 6),
]


@pytest.fixture
def book(tmp_path):
    from jarvis.utils import pdf_document
    pdf_document.clear_cache()
    yield make_pdf(tmp_path / 'garden.pdf', PAGES, OUTLINE)
    pdf_document.clear_cache()


def test_outline_lists_titles_with_one_based_pages_and_levels(book):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(book)
    assert doc.page_count == len(PAGES)
    entries = [(e.title, e.page, e.level) for e in doc.outline()]
    assert entries == [('Preface', 1, 0), ('Chapter 1 Introduction', 2, 0), ('Chapter 2 Soil Preparation', 3, 0),
                       ('2.1 Timings', 4, 1), ('Chapter 3 Plant Care', 5, 0), ('References', 7, 0)]


def test_outline_matches_need_every_query_word_case_insensitively(book):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(book)
    assert [e.page for e in doc.outline_matches('soil PREPARATION')] == [3]
    assert [e.page for e in doc.outline_matches('references')] == [7]
    assert doc.outline_matches('quantum') == []


def test_search_ranks_pages_containing_every_word_with_snippets(book):
    from jarvis.utils.pdf_document import open_document
    result = open_document(book).search('soil preparation')
    assert result.complete and result.pages_searched == result.page_count == len(PAGES)
    assert {hit.page for hit in result.hits} == {3, 4}
    assert all('soil' in hit.snippet.casefold() for hit in result.hits)
    assert all(len(hit.snippet) <= 200 for hit in result.hits)


def test_search_tolerates_small_inflections_without_language_rules(book):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(book)
    assert {hit.page for hit in doc.search('seedlings').hits} == {5, 6}
    assert {hit.page for hit in doc.search('seedling').hits} == {5, 6}
    # Short words must match exactly, so "soil" does not drag in unrelated words.
    assert doc.search('xyzzy').hits == []


def test_search_is_bounded_and_finishes_in_the_background(book):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(book)
    partial = doc.search('references', budget_sec=0)
    assert not partial.complete
    assert partial.pages_searched < partial.page_count
    deadline = time.monotonic() + 10
    result = partial
    while time.monotonic() < deadline and not result.complete:
        time.sleep(0.05)
        result = doc.search('references', budget_sec=0)
    assert result.complete
    assert 7 in {hit.page for hit in result.hits}


def test_documents_are_cached_until_the_file_changes(book):
    from jarvis.utils.pdf_document import open_document
    first = open_document(book)
    assert open_document(book) is first
    make_pdf(book, ['Only page about turbines'])
    changed = open_document(book)
    assert changed is not first
    assert changed.page_count == 1


def test_non_pdf_files_fail_clearly(tmp_path):
    from jarvis.utils.pdf_document import open_document
    bogus = tmp_path / 'notes.pdf'
    bogus.write_text('not a pdf')
    with pytest.raises(ValueError):
        open_document(bogus)
    with pytest.raises(ValueError):
        open_document(tmp_path / 'missing.pdf')

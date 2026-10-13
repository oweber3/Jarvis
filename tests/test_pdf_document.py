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
    assert {hit.page for hit in result.hits if not hit.partial} == {3, 4}
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


# A textbook laid out like most: each chapter's exercises are a child bookmark titled only
# "Problems", and some publishers pad bookmark titles with NUL characters.
TEXTBOOK_PAGES = [f'Page {n}' for n in range(1, 13)]
TEXTBOOK_OUTLINE = [
    ('Chapter 1 Introduction\x00\x00\x00', 0, [('1.1 Units', 1), ('1.6 Summary', 2), ('Problems\x00\x00', 3)]),
    ('Chapter 6 Inviscid Flow', 4, [('6.1 Euler Equation', 5), ('6.2 Bernoulli Equation', 6),
                                    ('Problems\x00\x00', 7)]),
    ('Chapter 7 Dimensional Analysis', 8, [('7.1 Similitude', 9), ('Problems', 10)]),
    ('Answers to Selected Problems', 11),
]


@pytest.fixture
def textbook(tmp_path):
    from jarvis.utils import pdf_document
    pdf_document.clear_cache()
    yield make_pdf(tmp_path / 'fluids.pdf', TEXTBOOK_PAGES, TEXTBOOK_OUTLINE)
    pdf_document.clear_cache()


def test_outline_titles_drop_padding_characters(textbook):
    from jarvis.utils.pdf_document import open_document
    titles = [e.title for e in open_document(textbook).outline()]
    assert 'Chapter 1 Introduction' in titles
    assert all('\x00' not in title for title in titles)


def test_outline_matches_count_the_chapters_above_an_entry(textbook):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(textbook)
    matches = doc.outline_matches('chapter 6 problems')
    assert [e.page for e in matches] == [8]
    assert matches[0].label == 'Chapter 6 Inviscid Flow > Problems'


def test_a_chapter_request_matches_the_chapter_not_every_section_in_it(textbook):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(textbook)
    assert [e.page for e in doc.outline_matches('chapter 6')] == [5]
    assert [e.page for e in doc.outline_matches('bernoulli')] == [7]


def test_closest_outline_entries_rank_by_how_many_query_words_they_share(textbook):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(textbook)
    # "practice" appears nowhere, so nothing matches every word; the chapter's problems share the most.
    assert doc.outline_matches('chapter 6 practice problems') == []
    closest = doc.outline_closest('chapter 6 practice problems', limit=4)
    assert closest[0].page == 8
    assert len(closest) <= 4
    # A section that adds nothing to its chapter's match is not listed separately from it.
    assert 6 not in [e.page for e in closest] and 7 not in [e.page for e in closest]
    assert doc.outline_closest('quantum chromodynamics', limit=4) == []


def test_section_numbers_match_whole(textbook):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(textbook)
    assert [e.page for e in doc.outline_matches('6.2')] == [7]
    assert [e.page for e in doc.outline_matches('section 1.6')] == []
    assert [e.page for e in doc.outline_matches('1.6')] == [3]


def test_a_bookmark_wrapping_the_whole_book_does_not_hide_matching_sections(tmp_path):
    from jarvis.utils.pdf_document import open_document
    path = make_pdf(tmp_path / 'algorithms.pdf', [f'Page {n}' for n in range(1, 6)], [
        ('Introduction to Algorithms', 0, [('Chapter 1 Sorting', 1, [('Introduction', 2)]),
                                           ('Chapter 2 Graphs', 3, [('Introduction', 4)])])])
    assert {e.page for e in open_document(path).outline_matches('introduction')} == {1, 3, 5}


def test_numbers_match_exactly(tmp_path):
    from jarvis.utils.pdf_document import open_document
    path = make_pdf(tmp_path / 'ds.pdf', [f'Page {n}' for n in range(1, 5)], [
        ('10.1 Stacks', 0), ('10.12 Queues', 1), ('10.123 Heaps', 2), ('2024 Edition', 3)])
    doc = open_document(path)
    assert [e.page for e in doc.outline_matches('10.12')] == [2]
    assert [e.page for e in doc.outline_matches('2024')] == [4]
    assert doc.outline_matches('202') == []


def test_the_file_is_released_once_every_page_is_read(book):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(book)
    assert doc.search('references').complete
    renamed = book.with_name('moved.pdf')
    book.rename(renamed)  # Windows refuses to rename a file that is still open
    assert renamed.exists()


def test_reading_the_outline_leaves_the_file_free(book):
    from jarvis.utils.pdf_document import open_document
    assert open_document(book).outline_matches('references')
    renamed = book.with_name('moved.pdf')
    book.rename(renamed)
    assert renamed.exists()


def test_a_file_removed_before_its_text_is_read_searches_as_empty(book):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(book)
    book.unlink()
    result = doc.search('references')
    assert result.hits == [] and result.complete


EXERCISE_PAGES = [
    'Problems for chapter four. Every problem below uses the momentum equation.',
    'P4.12 a tank drains.\n4.13 A rectangular air duct carries a flow of air.\n4.14 A jet strikes a vane.',
    'Problem solving tips: draw the control volume for every problem.',
    'Example 4.13 shows a lawn sprinkler; the same problem is considered later.',
    'Answers: 4.11 Q = 24, 4.13 V = 8 m/s',
    'Another problem set on energy.',
    'Problem statement review.',
]


@pytest.fixture
def exercises(tmp_path):
    from jarvis.utils import pdf_document
    pdf_document.clear_cache()
    yield make_pdf(tmp_path / 'exercises.pdf', EXERCISE_PAGES)
    pdf_document.clear_cache()


def test_pages_with_the_rarest_words_follow_pages_with_every_word(exercises):
    from jarvis.utils.pdf_document import open_document
    hits = open_document(exercises).search('problem 4.13').hits
    assert hits[0].page == 4 and not hits[0].partial  # the only page with both words
    partial = [hit.page for hit in hits if hit.partial]
    # "4.13" is on fewer pages than "problem", so the pages holding it come first.
    assert set(partial[:2]) == {2, 5}
    assert all(hit.page in (1, 3, 6, 7) for hit in hits if hit.partial and hit.page not in (2, 5))


def test_a_partial_hit_snippet_shows_its_rarest_word(exercises):
    from jarvis.utils.pdf_document import open_document
    hit = next(h for h in open_document(exercises).search('problem 4.13').hits if h.page == 2)
    assert '4.13 A rectangular air duct' in hit.snippet


def test_closest_entries_share_more_than_half_the_query_words(textbook):
    from jarvis.utils.pdf_document import open_document
    doc = open_document(textbook)
    # Only the common word "problems" is shared, which says nothing about where 4.13 is.
    assert doc.outline_closest('problem 4.13', limit=4) == []
    assert doc.outline_closest('chapter 6 practice problems', limit=4)[0].page == 8


def test_searching_a_large_book_stays_quick(tmp_path):
    from jarvis.utils import pdf_document
    pdf_document.clear_cache()
    filler = ' '.join(f'word{n} momentum equation control volume flow' for n in range(60))
    pages = [f'Page {n} {filler} problem' + (' 4.13 duct' if n == 900 else '') for n in range(1200)]
    doc = pdf_document.open_document(make_pdf(tmp_path / 'big.pdf', pages))
    assert doc.search('problem', budget_sec=30).complete
    started = time.monotonic()
    hits = doc.search('problem 4.13 duct', budget_sec=0).hits
    assert time.monotonic() - started < 1.0
    assert hits[0].page == 901
    pdf_document.clear_cache()

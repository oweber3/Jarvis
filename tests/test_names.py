"""Names and aliases of user-defined things (workspaces, routines) compare the same way everywhere."""
import pytest

from jarvis.utils import names


@pytest.mark.parametrize('a,b', [
    ('Movie Mode', 'movie mode'),
    ('  movie mode ', 'MOVIE MODE'),
    ('ＭＯＶＩＥ', 'movie'),  # full-width letters (NFKC)
    ('Straße', 'STRASSE'),  # case folding, not lower-casing
])
def test_names_compare_ignoring_case_width_and_surrounding_space(a, b):
    assert names.key(a) == names.key(b)


def test_different_names_stay_different():
    assert names.key('movie mode') != names.key('movie')


def test_names_claimed_once_are_all_kept():
    kept, dropped = names.drop_contested({'movie mode': ['film night'], 'tidy up': ['clean']})
    assert kept == {'movie mode': ['film night'], 'tidy up': ['clean']}
    assert dropped == 0


def test_a_contested_name_makes_its_owner_unavailable_and_a_contested_alias_is_removed():
    kept, dropped = names.drop_contested({
        'design': ['shared', 'mine'],
        'DESIGN': [],
        'essays': ['Shared', 'drafts'],
        'gaming': ['design'],
    })
    assert kept == {'essays': ['drafts'], 'gaming': []}
    # Two owners of a contested name, and the contested aliases of the owners that remain.
    assert dropped == 4


def test_an_alias_equal_to_its_own_name_is_not_a_contest():
    kept, _ = names.drop_contested({'Movie Mode': ['movie mode', 'film']})
    assert list(kept) == ['Movie Mode']


def test_a_name_or_alias_resolves_to_its_canonical_name():
    claims = {'Movie Mode': ['Film Night'], 'Tidy up': []}
    assert names.resolve('  movie MODE ', claims) == 'Movie Mode'
    assert names.resolve('FILM NIGHT', claims) == 'Movie Mode'
    assert names.resolve('tidy up', claims) == 'Tidy up'
    assert names.resolve('gaming', claims) is None
    assert names.resolve('   ', claims) is None
    assert names.resolve(None, claims) is None

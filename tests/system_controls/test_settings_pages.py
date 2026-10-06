"""Settings deep links come from a data file and open only through the shell."""
import pytest

from jarvis.platform.windows import settings_pages


def test_every_page_is_an_ms_settings_uri():
    pages = settings_pages.load_pages()
    assert pages, 'the page table must not be empty'
    for slug, uri in pages.items():
        assert slug == slug.casefold() and ' ' not in slug
        assert uri.startswith('ms-settings:') and ' ' not in uri


def test_common_pages_are_available():
    pages = settings_pages.load_pages()
    for slug in ('bluetooth', 'display', 'sound', 'wifi', 'power', 'updates', 'apps', 'privacy',
                 'night_light', 'focus'):
        assert slug in pages


def test_open_page_launches_the_mapped_uri_and_reports_the_page():
    launched = []
    result = settings_pages.open_page(' Bluetooth ', launch=launched.append)
    assert launched == [settings_pages.load_pages()['bluetooth']]
    assert result == {'action': 'settings_page_opened', 'page': 'bluetooth'}


def test_unknown_pages_are_rejected_without_launching_and_list_the_known_ones():
    launched = []
    with pytest.raises(ValueError) as error:
        settings_pages.open_page('nonsense', launch=launched.append)
    assert launched == []
    assert 'bluetooth' in str(error.value)


@pytest.mark.parametrize('raw', ['ms-settings:bluetooth', 'https://example.com', r'C:\Windows\notepad.exe', ''])
def test_raw_uris_and_paths_are_never_launched(raw):
    launched = []
    with pytest.raises(ValueError):
        settings_pages.open_page(raw, launch=launched.append)
    assert launched == []

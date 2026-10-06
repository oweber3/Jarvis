"""openPath resolves names that are not paths through the file search, without guessing."""
import json
from types import SimpleNamespace

import pytest

from jarvis.platform.windows import file_search, files
from jarvis.platform.windows.file_search import FileMatch, Resolution


@pytest.fixture
def opened(monkeypatch):
    launched = []
    monkeypatch.setattr(files, '_shell_open', launched.append)
    return launched


def found(path, kind='file'):
    return Resolution(chosen=FileMatch(str(path), kind, 1.0))


def test_a_bare_name_is_searched_and_the_single_match_is_opened(tmp_path, opened):
    document = tmp_path / 'Proposal Final.docx'
    document.write_text('x')
    asked = []
    result = files.open_path('proposal', searcher=lambda query: asked.append(query) or found(document))
    assert asked == ['proposal'] and opened == [str(document)]
    assert result['action'] == 'open_requested' and result['kind'] == 'file' and result['source'] == 'search'


def test_a_matching_folder_opens_as_a_folder(tmp_path, opened):
    result = files.open_path('projects', searcher=lambda query: found(tmp_path, 'folder'))
    assert result['kind'] == 'folder' and opened == [str(tmp_path)]


def test_several_matches_return_candidates_and_open_nothing(tmp_path, opened):
    candidates = [FileMatch(str(tmp_path / 'a.docx'), 'file', 2.0), FileMatch(str(tmp_path / 'b.docx'), 'file', 1.0)]
    with pytest.raises(files.AmbiguousTargetError) as error:
        files.open_path('proposal', searcher=lambda query: Resolution(candidates=candidates))
    assert opened == []
    assert [c['path'] for c in error.value.data['candidates']] == [str(tmp_path / 'a.docx'), str(tmp_path / 'b.docx')]
    assert error.value.data['candidates'][0]['kind'] == 'file'


def test_no_match_is_an_honest_not_found(opened):
    with pytest.raises(FileNotFoundError):
        files.open_path('zzzz nothing', searcher=lambda query: Resolution())
    assert opened == []


def test_a_name_that_only_matches_programs_points_to_the_application_tool(opened):
    with pytest.raises(ValueError) as error:
        files.open_path('notepad', searcher=lambda query: Resolution(programs_excluded=True))
    assert 'appControl' in str(error.value) and opened == []


def test_a_found_program_is_still_never_opened_as_a_document(tmp_path, opened):
    program = tmp_path / 'tool.exe'
    program.write_text('x')
    with pytest.raises(ValueError):
        files.open_path('tool', searcher=lambda query: found(program))
    assert opened == []


def test_a_found_path_that_vanished_is_not_opened(tmp_path, opened):
    with pytest.raises(FileNotFoundError):
        files.open_path('ghost', searcher=lambda query: found(tmp_path / 'gone.txt'))
    assert opened == []


@pytest.mark.parametrize('target', [r'C:\definitely\missing\file.txt', 'sub/dir/missing.txt', '~/missing-xyz.txt',
                                    r'.\missing.txt'])
def test_path_like_targets_are_never_searched(target, opened):
    def forbidden(query):
        pytest.fail('a path must not trigger a search')
    with pytest.raises(FileNotFoundError):
        files.open_path(target, searcher=forbidden)
    assert opened == []


def test_known_folders_and_existing_paths_do_not_search(tmp_path, opened, monkeypatch):
    def forbidden(query):
        pytest.fail('should not search')
    document = tmp_path / 'report.txt'
    document.write_text('x')
    monkeypatch.setattr(files, 'known_folder', lambda name: str(tmp_path))
    for target in ('Downloads', str(document)):
        files.open_path(target, searcher=forbidden)
    with pytest.raises(ValueError):
        files.open_path('https://example.com/a', searcher=forbidden)
    assert len(opened) == 2


def test_unavailable_search_is_reported_as_an_os_error(opened):
    def broken(query):
        raise file_search.SearchUnavailable('offline')
    with pytest.raises(OSError):
        files.open_path('proposal', searcher=broken)


# --- the tool adapter ----------------------------------------------------------------

def test_the_tool_returns_candidates_as_a_failure_the_model_can_ask_about(tmp_path, monkeypatch):
    from jarvis.tools.base import ToolContext
    from jarvis.tools.builtin.windows import OpenPathTool
    candidates = [FileMatch(str(tmp_path / 'a.docx'), 'file', 2.0), FileMatch(str(tmp_path / 'b.docx'), 'file', 1.0)]
    monkeypatch.setattr(file_search, 'find', lambda query: Resolution(candidates=candidates))
    ctx = ToolContext(None, SimpleNamespace(windows_tools_enabled=True, windows_path_aliases={}), '', '', '', 1, lambda text: None)
    result = OpenPathTool().run({'target': 'proposal'}, ctx)
    assert result.success is False
    data = json.loads(result.reply_text)
    assert data['action'] == 'candidates' and len(data['candidates']) == 2
    assert 'which' in result.error_message.casefold() or 'several' in result.error_message.casefold()


def test_the_tool_description_routes_name_lookups_and_points_apps_elsewhere():
    from jarvis.tools.builtin.windows import OpenPathTool
    tool = OpenPathTool()
    assert 'appControl' in tool.description
    assert 'name' in tool.target_description.casefold()

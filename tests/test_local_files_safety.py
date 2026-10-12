"""localFiles and the central policy keep to the home boundary, never touch network paths and match
important locations as Windows does.

Everything runs in a fake home folder under tmp_path; nothing reaches the network, and no app, window or
real file is touched.
"""
import ntpath
import os
import sys

import pytest

from jarvis.tools.builtin import local_files as lf
from jarvis.tools.confirmation import SafetyTier, evaluate_safety, get_confirmation_store
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries

windows_only = pytest.mark.skipif(sys.platform != 'win32', reason='Windows path semantics')


class Cfg:
    voice_debug = False
    windows_tools_enabled = True
    file_delete_enabled = False


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home folder whose known folders are plain subfolders (as in test_local_files_organise)."""
    root = tmp_path / 'home'
    root.mkdir()
    original = os.path.expanduser

    def expand(p):
        if isinstance(p, str) and (p == '~' or p.startswith(('~/', '~\\'))):
            return str(root) + p[1:]
        return original(p)

    monkeypatch.setattr(os.path, 'expanduser', expand)
    known = {name: root / name.title() for name in lf.KNOWN_FOLDERS}
    known['documents'] = root / 'OneDrive' / 'Documents'
    for folder in known.values():
        folder.mkdir(parents=True)
    monkeypatch.setattr(lf, 'known_folder', lambda name: str(known[name]))
    get_confirmation_store().clear_pending()
    yield root
    get_confirmation_store().clear_pending()


def run(args):
    """Through the registry, so the central safety policy applies exactly as in a reply."""
    return run_tool_with_retries(db=None, cfg=Cfg(), tool_name='localFiles', tool_args=args, system_prompt='',
                                 original_prompt='', redacted_text='', max_retries=0)


def tier(args, tool='localFiles'):
    return evaluate_safety(tool, args, Cfg(), tool=BUILTIN_TOOLS[tool], language='en').tier


# --- 1. list escapes the home boundary through its glob ---------------------------------------

def test_list_glob_cannot_reach_outside_the_home_folder(home, tmp_path):
    """local_files.spec.md: list acts only inside the home folder; a path outside it is refused."""
    outside = tmp_path / 'other_user' / 'Documents'
    outside.mkdir(parents=True)
    (outside / 'bank statement.pdf').write_bytes(b'secret')
    result = run({'operation': 'list', 'path': '~', 'glob': '../other_user/Documents/*'})
    assert 'bank statement.pdf' not in (result.reply_text or ''), result.reply_text


def test_recursive_list_glob_cannot_reach_outside_the_home_folder(home, tmp_path):
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 'elsewhere' / 'private.txt').write_text('x', encoding='utf-8')
    result = run({'operation': 'list', 'path': 'Downloads', 'glob': '../../elsewhere/*', 'recursive': True})
    assert 'private.txt' not in (result.reply_text or ''), result.reply_text


# --- 2. a network (UNC) path is contacted before the boundary refuses it ----------------------

_UNC = '\\\\203.0.113.7\\share'


@pytest.fixture
def network(monkeypatch):
    """Record every filesystem call that would open a UNC path (an SMB/WebDAV connection that hands
    the user's NTLM credentials to that host). The fake host is unreachable."""
    touched = []

    def is_unc(value):
        text = os.fspath(value) if isinstance(value, (str, os.PathLike)) else ''
        return text.startswith('\\\\') and not text.startswith(('\\\\?\\', '\\\\.\\'))

    def guard(real):
        def wrapper(path, *args, **kwargs):
            if is_unc(path):
                touched.append(os.fspath(path))
                raise FileNotFoundError(2, 'The network path was not found', os.fspath(path))
            return real(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(ntpath, '_getfinalpathname', guard(ntpath._getfinalpathname))
    monkeypatch.setattr(ntpath, '_nt_readlink', guard(ntpath._nt_readlink), raising=False)
    monkeypatch.setattr(os, 'stat', guard(os.stat))
    monkeypatch.setattr(os, 'lstat', guard(os.lstat))
    for name in ('exists', 'isdir', 'isfile'):
        monkeypatch.setattr(ntpath, name, guard(getattr(ntpath, name)))
    return touched


@windows_only
@pytest.mark.parametrize('args', [
    {'operation': 'read', 'path': '//203.0.113.7/share/notes.txt'},
    {'operation': 'read', 'path': r'\\?\UNC\203.0.113.7\share\notes.txt'},
    {'operation': 'read', 'path': _UNC + '\\notes.txt'},
    {'operation': 'list', 'path': _UNC},
    {'operation': 'write', 'path': _UNC + '\\notes.txt', 'content': 'x'},
])
def test_local_files_refuses_a_network_path_without_contacting_it(home, network, args):
    """local_files.spec.md: 'A path outside it is refused before anything is touched' and
    'Nothing is sent anywhere'. Resolving the path first opens a connection to the remote host."""
    result = run(args)
    assert result.success is False
    assert network == [], f'contacted the network host while refusing: {network[:2]}'


@windows_only
def test_find_never_scans_a_network_share(home, network):
    """local_files.spec.md, Privacy: searches use only local indexes and the local disk."""
    run({'operation': 'find', 'path': _UNC, 'type': 'pdf'})
    assert network == [], f'find contacted the network host: {network[:2]}'


@windows_only
def test_central_safety_classification_never_contacts_a_network_path(network):
    """Classifying a call is pure policy; it must not open the target. evaluate_safety resolves any
    'target' or 'path' argument of any tool, so merely classifying a window focus request with a UNC
    target makes an SMB connection."""
    evaluate_safety('appControl', {'action': 'focus', 'target': _UNC + '\\x'}, Cfg(),
                    tool=BUILTIN_TOOLS['appControl'], language='en')
    assert network == [], f'safety classification contacted the network host: {network[:2]}'


# --- 3. important-location check is case-sensitive on Windows ---------------------------------

@windows_only
@pytest.mark.parametrize('path', ['~/.SSH/authorized_keys', '~/.Ssh/config', '~/.AWS/credentials',
                                  '~/.ssh./authorized_keys', '~/.ssh /authorized_keys'])
def test_writing_into_ssh_in_another_letter_case_still_needs_desktop_confirmation(home, path):
    """local_files.spec.md: mutating operations in an important location (.ssh and similar) need desktop
    confirmation. Windows paths are case-insensitive, so ~/.SSH is the same folder as ~/.ssh."""
    lower = tier({'operation': 'write', 'path': path.lower(), 'content': 'ssh-ed25519 AAAA attacker'})
    assert lower == SafetyTier.CONFIRM_DIALOG  # the policy itself
    assert tier({'operation': 'write', 'path': path, 'content': 'ssh-ed25519 AAAA attacker'}) == lower


@windows_only
def test_a_key_written_to_dot_ssh_in_upper_case_is_not_written_without_confirmation(home):
    """The same, end to end: with no desktop to confirm on, nothing may be written into ~/.ssh."""
    result = run({'operation': 'write', 'path': '~/.SSH/authorized_keys', 'content': 'ssh-ed25519 AAAA attacker'})
    assert not (home / '.ssh' / 'authorized_keys').exists(), result.reply_text


@windows_only
@pytest.mark.parametrize('path,network', [
    (r'\\host\share\a.txt', True), ('//host/share/a.txt', True), (r'\\?\UNC\host\share', True),
    (r'\\.\pipe\x', True), (r'\\?\C:\Users\a.txt', False), (r'C:\Users\a.txt', False), ('~/notes.txt', False),
])
def test_network_and_device_paths_are_recognised_from_their_text(path, network):
    from jarvis.utils.path_safety import is_network_or_device_path
    assert is_network_or_device_path(path) is network

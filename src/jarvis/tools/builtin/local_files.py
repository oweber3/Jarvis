"""Local files tool: find, list, read, write, append, delete, move, copy and rename.

See ``local_files.spec.md``. Reading and changing files stays inside the home folder; ``find`` reports
names, dates and sizes from the local index or a folder scan and never changes anything.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Dict, Any, Optional

from ..base import Tool, ToolContext
from ..types import ToolExecutionResult
from ...debug import debug_log
from ...platform import file_find

KNOWN_FOLDERS = ('desktop', 'documents', 'downloads', 'pictures', 'music', 'videos')
OPERATIONS = ('find', 'list', 'read', 'write', 'append', 'delete', 'move', 'copy', 'rename')
# A copy, or a move to another drive, larger than this is refused so a reply never waits on a long transfer.
MAX_TRANSFER_BYTES = 2 * 1024 ** 3
MAX_TRANSFER_ITEMS = 10_000

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
_EXTENSION = re.compile(r'\.[^\W_]{1,10}', re.UNICODE)
_PAST = {'move': 'moved', 'copy': 'copied', 'rename': 'renamed'}
DELETE_OFF = ("File deletion is turned off, so no file was deleted. It can be turned on in Settings, "
              "Windows Control, Allow File Deletion.")


def _delete_allowed(cfg: Any) -> bool:
    """Deleting needs ``file_delete_enabled`` to be exactly true; anything else keeps it off."""
    return getattr(cfg, 'file_delete_enabled', False) is True


def known_folder(name: str) -> str:
    """The user's real Desktop, Documents, ... (redirection respected on Windows), else ``~/<Name>``."""
    if sys.platform == 'win32':
        from ...platform.windows.files import known_folder as windows_known_folder
        try:
            return windows_known_folder(name)
        except OSError:
            pass
    return os.path.join(os.path.expanduser('~'), name.title())


def _home() -> Path:
    return Path(os.path.expanduser('~')).resolve()


def resolve_path(raw: Any) -> Path:
    """An absolute, ``~`` or home-relative path; a leading known folder name is the real folder."""
    text = str(raw).strip()
    if text == '~' or text.startswith(('~/', '~\\')):
        return Path(os.path.expanduser('~'), text[2:]).resolve()
    path = Path(text)
    if path.is_absolute() or path.drive:
        return path.resolve()
    parts = path.parts
    if parts and parts[0].casefold() in KNOWN_FOLDERS:
        return Path(known_folder(parts[0].casefold()), *parts[1:]).resolve()
    return (_home() / path).resolve()


def _inside_home(path: Path) -> bool:
    home = _home()
    return path == home or home in path.parents


def _within_home(raw: Any) -> Path:
    resolved = resolve_path(raw)
    if not _inside_home(resolved):
        raise PermissionError(f'Path not allowed: {resolved}')
    return resolved


def _new_name(source: Path, new_name: Any) -> str:
    name = str(new_name if new_name is not None else '').strip()
    if (not name or name in ('.', '..') or _INVALID_NAME.search(name) or name[-1] in '. '
            or name.split('.')[0].casefold() in _RESERVED_NAMES):
        raise ValueError(f'"{name}" is not a usable name. Use a single name without folder separators '
                         'or any of <>:"/\\|?*.')
    if source.is_file() and source.suffix and not _EXTENSION.fullmatch(Path(name).suffix):
        name += source.suffix
    return name


def _plan(operation: str, args: Dict[str, Any]) -> tuple[Path, Path]:
    """The source and final path of a move, copy or rename, validated; nothing on disk changes."""
    if not args.get('path'):
        raise ValueError(f'{operation} needs the full path of the item; use find to get it.')
    source = _within_home(args['path'])
    if not source.exists():
        raise FileNotFoundError(f'Not found: {source}. Use find to get the full path.')
    if operation == 'rename':
        if not args.get('new_name'):
            raise ValueError('rename needs new_name.')
        folder = source.parent
    else:
        if not args.get('destination'):
            raise ValueError(f'{operation} needs a destination folder.')
        folder = _within_home(args['destination'])
        if folder.exists() and not folder.is_dir():
            raise NotADirectoryError(f'The destination is a file, not a folder: {folder}')
    name = _new_name(source, args['new_name']) if args.get('new_name') else source.name
    final = folder / name
    if str(final) == str(source):
        raise ValueError(f'The item is already there with that name: {source}')
    if source.is_dir() and (final == source or source in final.parents):
        raise ValueError('A folder cannot be moved or copied into itself.')
    if final.exists() and not (operation != 'copy' and os.path.normcase(str(final)) == os.path.normcase(str(source))):
        raise FileExistsError(f'Something called {final.name} already exists in {folder}; nothing was replaced.')
    return source, final


def _check_transfer_size(source: Path) -> None:
    """Refuse a transfer above the size or item caps, counting no further than the caps."""
    if source.is_file():
        if source.stat().st_size > MAX_TRANSFER_BYTES:
            raise ValueError('That is too large to copy here (over 2 GiB); use File Explorer for it.')
        return
    total = items = 0
    for root, folders, files in os.walk(source):
        items += len(folders) + len(files)
        for name in files:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                continue
        if items > MAX_TRANSFER_ITEMS or total > MAX_TRANSFER_BYTES:
            raise ValueError('That folder is too large to copy here (over 2 GiB or 10,000 items); '
                             'use File Explorer for it.')


def _same_drive(source: Path, folder: Path) -> bool:
    existing = folder
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return os.stat(source).st_dev == os.stat(existing).st_dev


def _copy_file(source: Path, final: Path) -> None:
    """Exclusive create, so an item that appears meanwhile is never replaced."""
    with source.open('rb') as reader, final.open('xb') as writer:
        shutil.copyfileobj(reader, writer, 1024 * 1024)
    shutil.copystat(source, final)


def _whole_number(value: Any) -> Optional[int]:
    """Model output such as 5, 5.0 or "5"; ``None`` for anything else."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _local_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')


def _json(data: Dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(success=True, reply_text=json.dumps(data, ensure_ascii=False))


def _find_backends(scope: Optional[str]) -> list:
    if sys.platform == 'win32':
        from ...platform.windows import file_search
        return file_search.find_backends(scoped=scope is not None)
    return [file_find.ScanBackend()]


class LocalFilesTool(Tool):
    """Find files, and read or change files inside the user's home folder."""

    # Its results carry outside content (routines.spec.md, Prompt-injection boundary).
    returns_outside_content = True

    @property
    def name(self) -> str:
        return "localFiles"

    @property
    def description(self) -> str:
        return ("Find files by name, type or date, and read, write, list, move, copy, rename or delete files in "
                "your home folder. find returns full paths with dates and sizes (e.g. the PDF downloaded "
                "yesterday, the biggest files in Downloads); move, copy and rename need a full path, so find "
                "the item first. Nothing is ever replaced by move, copy or rename. To open a file use openPath.")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": list(OPERATIONS), "description": "What to do."},
                "path": {"type": "string", "description": (
                    "File or folder: a full path, a path relative to your home folder, or Desktop, Documents, "
                    "Downloads, Pictures, Music or Videos (optionally followed by a subfolder, e.g. "
                    "Documents/Taxes). For find, the folder to search in; omit it to search everywhere.")},
                "content": {"type": "string", "description": "Text to write or append."},
                "glob": {"type": "string", "description": "list: glob pattern (default *)."},
                "recursive": {"type": "boolean", "description": "list: include subfolders."},
                "name": {"type": "string", "description": "find: words that must all appear in the name."},
                "type": {"type": "string", "description": (
                    "find: document, spreadsheet, presentation, image, video, audio, archive, program or "
                    "folder, or extensions such as \"pdf\" or \"docx, xlsx\".")},
                "when": {"type": "string", "enum": list(file_find.WHEN), "description": (
                    "find: a date range on the computer's clock. Not with after/before.")},
                "after": {"type": "string", "description": "find: on or after this local date, YYYY-MM-DD or YYYY-MM-DDTHH:MM."},
                "before": {"type": "string", "description": "find: strictly before this local date, YYYY-MM-DD or YYYY-MM-DDTHH:MM."},
                "sort": {"type": "string", "enum": list(file_find.SORTS), "description": "find: newest (default), oldest or largest."},
                "limit": {"type": "integer", "description": f"find: how many items to return, 1 to {file_find.MAX_LIMIT} (default {file_find.DEFAULT_LIMIT})."},
                "destination": {"type": "string", "description": "move/copy: the folder to put the item in (created if missing)."},
                "new_name": {"type": "string", "description": (
                    "rename, or move/copy: the new name. A file keeps its extension when none is given.")},
            },
            "required": ["operation"],
        }

    def classify_safety(self, args: Optional[Dict[str, Any]], cfg: Any) -> Any:
        from ..confirmation import ConfirmationRequest, SafetyTier, is_system_or_important_location

        def request(tier=SafetyTier.SAFE, action=self.name, target='', **extra):
            return ConfirmationRequest(tool_name=self.name, tier=tier, action=action, target=target,
                                       parameters=dict(args or {}), **extra)

        if not (args and isinstance(args, dict)):
            return request()
        operation = str(args.get("operation") or "").strip().lower()
        if operation in ('move', 'copy', 'rename'):
            try:
                source, final = _plan(operation, args)
            except (OSError, ValueError):
                # Refused by run before anything changes; the target still lets the central policy look.
                return request(action=f'{operation} file', target=str(args.get('path') or ''), mutates=True)
            changed = [final] if operation == 'copy' else [source, final]
            important = any(is_system_or_important_location(str(path)) for path in changed)
            target = str(final if operation == 'copy' else source)
            if important:
                return request(SafetyTier.CONFIRM_DIALOG, f'{operation} file', target, mutates=True,
                               consequence=f'This will {operation} an item in or into an important system or user folder.')
            return request(action=f'{operation} file', target=target, mutates=True)
        path_arg = args.get("path")
        if not operation or not path_arg:
            return request()
        try:
            target_path = resolve_path(path_arg)
        except (OSError, ValueError):
            target_path = Path(str(path_arg))

        if operation == "delete":
            if not _delete_allowed(cfg):
                # Refused before any confirmation, so no "yes" can lead to a deletion.
                return request(SafetyTier.DENY, "delete file", str(target_path), reason=DELETE_OFF)
            return request(SafetyTier.CONFIRM_VOICE, "delete file", str(target_path), mutates=True,
                           consequence="The file will be permanently deleted.")
        if operation == "write":
            if target_path.exists():
                return request(SafetyTier.CONFIRM_VOICE, "overwrite file", str(target_path), mutates=True,
                               consequence="The existing file will be overwritten with new content.")
            return request(action="write file", target=str(target_path), mutates=True)
        return request(action=f"{operation} file", target=str(target_path), mutates=operation == "append")

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        """Execute the local files tool."""
        try:
            if not (args and isinstance(args, dict)):
                return ToolExecutionResult(success=False, reply_text="localFiles requires a JSON object with at least 'operation' and 'path'.")

            operation = str(args.get("operation") or "").strip().lower()
            if operation == 'find':
                return self._find(args, context)
            if operation in ('move', 'copy', 'rename'):
                return self._transfer(operation, args, context)

            path_arg = args.get("path")
            if not operation or not path_arg:
                return ToolExecutionResult(success=False, reply_text="localFiles requires 'operation' and 'path'.")

            target = _within_home(path_arg)

            # list
            if operation == "list":
                if not target.exists():
                    return ToolExecutionResult(success=False, reply_text=f"Path not found: {target}")
                if target.is_file():
                    return ToolExecutionResult(success=True, reply_text=f"File: {target.name}")

                glob_pattern = args.get("glob", "*")
                recursive = bool(args.get("recursive", False))

                try:
                    if recursive:
                        files = list(target.rglob(glob_pattern))
                    else:
                        files = list(target.glob(glob_pattern))

                    if not files:
                        return ToolExecutionResult(success=True, reply_text=f"No files found matching '{glob_pattern}' in {target}")

                    file_list = []
                    for f in sorted(files)[:50]:  # Limit to 50 files
                        relative_path = f.relative_to(target)
                        file_type = "DIR" if f.is_dir() else "FILE"
                        file_list.append(f"  {file_type}: {relative_path}")

                    result = f"Contents of {target}:\n" + "\n".join(file_list)
                    if len(files) > 50:
                        result += f"\n... and {len(files) - 50} more files"

                    return ToolExecutionResult(success=True, reply_text=result)
                except Exception as e:
                    return ToolExecutionResult(success=False, reply_text=f"List failed: {e}")

            # read
            if operation == "read":
                if not target.exists() or not target.is_file():
                    return ToolExecutionResult(success=False, reply_text=f"File not found: {target}")
                try:
                    data = target.read_text(encoding="utf-8", errors="replace")
                    max_chars = 10000
                    if len(data) > max_chars:
                        data = data[:max_chars] + f"\n... (truncated, showing first {max_chars} chars)"
                    return ToolExecutionResult(success=True, reply_text=data)
                except Exception as e:
                    return ToolExecutionResult(success=False, reply_text=f"Read failed: {e}")

            # write
            if operation == "write":
                content = args.get("content")
                if not isinstance(content, str):
                    return ToolExecutionResult(success=False, reply_text="Write requires string 'content'.")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    return ToolExecutionResult(success=True, reply_text=f"Wrote {len(content)} characters to {target}")
                except Exception as e:
                    return ToolExecutionResult(success=False, reply_text=f"Write failed: {e}")

            # append
            if operation == "append":
                content = args.get("content")
                if not isinstance(content, str):
                    return ToolExecutionResult(success=False, reply_text="Append requires string 'content'.")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("a", encoding="utf-8", errors="replace") as f:
                        f.write(content)
                    return ToolExecutionResult(success=True, reply_text=f"Appended {len(content)} characters to {target}")
                except Exception as e:
                    return ToolExecutionResult(success=False, reply_text=f"Append failed: {e}")

            # delete
            if operation == "delete":
                # Checked again here: an action confirmed before the switch went off must not run.
                if not _delete_allowed(context.cfg):
                    debug_log("localFiles delete refused: file deletion is turned off", "tools")
                    return ToolExecutionResult(success=False, reply_text=DELETE_OFF)
                try:
                    if target.exists() and target.is_file():
                        target.unlink()
                        return ToolExecutionResult(success=True, reply_text=f"Deleted file: {target}")
                    return ToolExecutionResult(success=False, reply_text=f"File not found: {target}")
                except Exception as e:
                    return ToolExecutionResult(success=False, reply_text=f"Delete failed: {e}")

            return ToolExecutionResult(success=False, reply_text=f"Unknown localFiles operation: {operation}")
        except PermissionError as pe:
            return ToolExecutionResult(success=False, reply_text=f"Permission error: {pe}")
        except Exception as e:
            return ToolExecutionResult(success=False, reply_text=f"localFiles error: {e}")

    def _find(self, args: Dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        try:
            scope = None
            if args.get('path'):
                scope = resolve_path(args['path'])
                if not scope.is_dir():
                    return ToolExecutionResult(success=False, reply_text=f'Folder not found: {scope}')
            elif sys.platform != 'win32':
                scope = _home()
            limit = _whole_number(args.get('limit', file_find.DEFAULT_LIMIT))
            if limit is None or not 1 <= limit <= file_find.MAX_LIMIT:
                raise ValueError(f'limit must be a whole number from 1 to {file_find.MAX_LIMIT}.')
            extensions, kind = file_find.parse_type(args.get('type'))
            after, before = file_find.date_range(args.get('when'), args.get('after'), args.get('before'))
            criteria = file_find.FindCriteria(
                tokens=file_find.name_tokens(args.get('name') or ''), extensions=extensions, kind=kind, after=after,
                before=before, scope=str(scope) if scope else None, sort=str(args.get('sort') or 'newest'),
                limit=limit)
            outcome = file_find.find(criteria, _find_backends(criteria.scope))
        except file_find.SearchUnavailable:
            debug_log('localFiles find: no search backend answered.', 'tools')
            message = ('Searching everywhere is not available right now (no file index answered). '
                       'Name a folder to search, such as Downloads or Documents.' if scope is None else
                       'That folder could not be searched right now.')
            return ToolExecutionResult(success=False, reply_text=message)
        except ValueError as exc:
            return ToolExecutionResult(success=False, reply_text=str(exc))
        data: Dict[str, Any] = {'action': 'found', 'scope': criteria.scope or 'everywhere'}
        if after is not None or before is not None:
            data['range'] = {key: _local_time(value) for key, value in (('after', after), ('before', before))
                             if value is not None}
        data.update(count=outcome.count, complete=outcome.complete, items=[
            {'name': item.name, 'path': item.path, 'kind': item.kind, 'date': _local_time(item.date),
             **({'size': item.size} if item.kind == 'file' and item.size is not None else {})}
            for item in outcome.items])
        context.user_print(f'🔎 Found {outcome.count} item(s).')
        return _json(data)

    def _transfer(self, operation: str, args: Dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        try:
            source, final = _plan(operation, args)
            kind = 'folder' if source.is_dir() else 'file'
            same_drive = _same_drive(source, final.parent)
            if operation == 'copy' or not same_drive:
                _check_transfer_size(source)
            final.parent.mkdir(parents=True, exist_ok=True)
            if operation == 'copy':
                if kind == 'folder':
                    shutil.copytree(source, final)
                else:
                    _copy_file(source, final)
            elif same_drive:
                # A rename on disk: instant, and it refuses an existing target on Windows.
                os.rename(source, final)
            else:
                shutil.move(str(source), str(final))
        except (OSError, ValueError) as exc:
            debug_log(f'localFiles {operation} refused ({type(exc).__name__}).', 'tools')
            return ToolExecutionResult(success=False, reply_text=str(exc))
        debug_log(f'localFiles {operation} done ({kind}).', 'tools')
        context.user_print(f'📁 {_PAST[operation].capitalize()} 1 {kind}.')
        return _json({'action': _PAST[operation], 'kind': kind, 'from': str(source), 'to': str(final)})

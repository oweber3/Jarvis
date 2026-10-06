"""pdfNavigate: find and show pages of the PDF open in the user's viewer (``ui_automation.spec.md``)."""
from __future__ import annotations

import json
from pathlib import Path

from ...base import Tool
from ...types import ToolExecutionResult
from ....debug import debug_log
from ....utils.redact import redact
from .desktop_control import _redact_data

ACTIONS = ('goto', 'find', 'outline')
_ARGUMENTS = ('action', 'page', 'query', 'file')
_TIMEOUT_SEC = 12
_FAST_CHECK_SEC = 1.0
_SEARCH_BUDGET_SEC = 4.0
_MAX_CANDIDATES = 8
_MAX_OUTLINE = 80


class PdfNavigateTool(Tool):
    # Its results carry outside content (routines.spec.md, Prompt-injection boundary).
    returns_outside_content = True

    name = 'pdfNavigate'
    description = ('Go to a page, chapter or topic in the PDF open in PDFgear, Chrome or Edge: "go to page 42", "jump to '
                   'the chapter on heat transfer", "find where it mentions eigenvalues". NOT for other apps (use '
                   'uiControl).')

    @property
    def inputSchema(self):
        return {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': list(ACTIONS), 'description': (
                    'goto shows a page number; find looks up a chapter or topic and jumps when one chapter '
                    'matches, otherwise lists candidate pages; outline lists the chapters.')},
                'page': {'type': 'integer', 'description': 'Page number for goto.'},
                'query': {'type': 'string', 'description': 'Chapter title or words to find, for find.'},
                'file': {'type': 'string', 'description': 'Path of the PDF, only when a previous result listed candidates.'},
            },
            'required': ['action'],
            'additionalProperties': False,
        }

    def fast_available(self, cfg) -> bool:
        """Fast page routes apply only while the user is in a PDF viewer; anywhere else the model decides."""
        try:
            from ....platform.windows import pdf_viewer as pv
            from ....platform.windows._bounded import run_bounded
            return run_bounded(pv.foreground_viewer, _FAST_CHECK_SEC) is not None
        except Exception as exc:  # noqa: BLE001 - an unreadable foreground means no fast route
            debug_log(f'{self.name} foreground check failed ({type(exc).__name__}).', 'windows')
            return False

    @staticmethod
    def _parse(args):
        if not isinstance(args, dict) or set(args) - set(_ARGUMENTS):
            raise ValueError('Invalid pdfNavigate arguments.')
        action = args.get('action')
        if action not in ACTIONS:
            raise ValueError('Unsupported action.')
        page = None
        if action == 'goto':
            raw = args.get('page')
            try:
                page = int(str(raw).strip()) if not isinstance(raw, bool) and raw is not None else None
            except ValueError:
                page = None
            if page is None:
                raise ValueError('Give the page number to go to.')
        query = str(args.get('query') or '').strip()
        if action == 'find' and not query:
            raise ValueError('Say what to find.')
        file = str(args.get('file') or '').strip()
        # Never a network path: probing \\host\share makes Windows connect to that host with the
        # user's credentials, and a model can be led to one by text it read.
        if file and (Path(file).suffix.casefold() != '.pdf' or file.replace('/', '\\').startswith('\\\\')):
            raise ValueError('That file is not an existing PDF on this PC.')
        return action, page, query, file

    def run(self, args, context):
        try:
            if not context.cfg.windows_tools_enabled:
                raise ValueError("Windows control is disabled; set 'windows_tools_enabled' to true to use it.")
            action, page, query, file = self._parse(args)
            from ....platform.windows._bounded import run_bounded
            data = run_bounded(lambda: self._operate(action, page, query, file), _TIMEOUT_SEC)
            context.user_print(f'📄 PDF {action} completed.')
            return ToolExecutionResult(success=True, reply_text=json.dumps(_redact_data(data), ensure_ascii=False))
        except ValueError as exc:
            candidates = getattr(exc, 'candidates', None)
            debug_log(f'{self.name} could not act ({type(exc).__name__}).', 'windows')
            text = json.dumps({'error': 'ambiguous', 'candidates': [
                {'file': Path(path).name, 'folder': str(Path(path).parent), 'path': path} for path in candidates]},
                ensure_ascii=False) if candidates else None
            return ToolExecutionResult(success=False, reply_text=text, error_message=redact(str(exc)))
        except (OSError, TypeError) as exc:
            debug_log(f'{self.name} failed ({type(exc).__name__}).', 'windows')
            return ToolExecutionResult(success=False, reply_text=None, error_message=redact(str(exc)))

    def _operate(self, action, page, query, file):
        from ....platform.windows import pdf_viewer as pv
        from ....utils.pdf_document import open_document
        viewer = pv.find_viewer()
        if file:
            path = Path(file)
            if not path.is_file():
                raise ValueError('That file is not an existing PDF on this PC.')
            if not pv.shows_file(viewer, path):
                viewer = None
        elif viewer is None:
            raise ValueError('No PDF is open in PDFgear, Chrome or Edge; open one or give its path.')
        else:
            path = pv.document_for(viewer)
        document = open_document(path)
        base = {'file': path.name, 'page_count': document.page_count}
        if action == 'outline':
            entries = document.outline()
            return {**base, 'outline': [{'title': e.title, 'page': e.page, 'level': e.level}
                                        for e in entries[:_MAX_OUTLINE]], 'truncated': len(entries) > _MAX_OUTLINE}
        if action == 'goto':
            if not 1 <= page <= document.page_count:
                raise ValueError(f'The document has pages 1 to {document.page_count}.')
            return {**base, **pv.show_page(viewer, path, page, document.page_count)}
        chapters = document.outline_matches(query)
        if len({entry.page for entry in chapters}) == 1:
            entry = chapters[0]
            shown = pv.show_page(viewer, path, entry.page, document.page_count)
            return {**base, **shown, 'matched': 'outline', 'title': entry.title}
        search = document.search(query, budget_sec=_SEARCH_BUDGET_SEC, limit=_MAX_CANDIDATES)
        candidates = [{'page': e.page, 'title': e.title} for e in chapters]
        seen = {c['page'] for c in candidates}
        candidates += [{'page': hit.page, 'snippet': hit.snippet} for hit in search.hits if hit.page not in seen]
        debug_log(f'PDF find: {len(chapters)} outline and {len(search.hits)} page matches.', 'windows')
        return {**base, 'candidates': candidates[:_MAX_CANDIDATES], 'pages_searched': search.pages_searched,
                'complete': search.complete}

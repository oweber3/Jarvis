"""UI Automation client: compact snapshots and pattern-based actions on any application's controls.

Pure OS layer: knows nothing about tools, replies or models. Uses the system UIAutomationCore type
library through comtypes, imported lazily. Actions use UIA control patterns only; this module never
synthesises mouse or keyboard input and never focuses a window. Callers run every public function on
a bounded worker (``_bounded.run_bounded``). See ``ui_automation.spec.md``.
"""
from __future__ import annotations

import contextlib
import ctypes
import gc
import os
import re
import threading
import time
import traceback
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ...debug import debug_log

MAX_ELEMENTS = 60
MAX_DEPTH = 12
VISIT_BUDGET = 600
SEARCH_BUDGET = 2000
WALK_BUDGET_SEC = 5.0
NAME_CHARS = 60
VALUE_CHARS = 60
TITLE_CHARS = 80
TEXT_CHARS = 150
MAX_TEXTS = 8
READ_CHARS = 4000
SETTLE_SEC = 0.25
CONNECTION_TIMEOUT_MS = 2000
TRANSACTION_TIMEOUT_MS = 3000

_RPC_E_CHANGED_MODE = -2147417850
_UIA_E_ELEMENTNOTAVAILABLE = -2147220991
_WM_COMMAND = 0x0111

CONTROL_TYPES = {
    50000: 'button', 50001: 'calendar', 50002: 'checkbox', 50003: 'combobox', 50004: 'edit',
    50005: 'hyperlink', 50006: 'image', 50007: 'listitem', 50008: 'list', 50009: 'menu', 50010: 'menubar',
    50011: 'menuitem', 50012: 'progressbar', 50013: 'radiobutton', 50014: 'scrollbar', 50015: 'slider',
    50016: 'spinner', 50017: 'statusbar', 50018: 'tab', 50019: 'tabitem', 50020: 'text', 50021: 'toolbar',
    50022: 'tooltip', 50023: 'tree', 50024: 'treeitem', 50025: 'custom', 50026: 'group', 50027: 'thumb',
    50028: 'datagrid', 50029: 'dataitem', 50030: 'document', 50031: 'splitbutton', 50032: 'window',
    50033: 'pane', 50034: 'header', 50035: 'headeritem', 50036: 'table', 50037: 'titlebar',
    50038: 'separator', 50039: 'semanticzoom', 50040: 'appbar',
}
INTERACTIVE_TYPES = frozenset({
    'button', 'checkbox', 'combobox', 'edit', 'hyperlink', 'listitem', 'menuitem', 'radiobutton', 'slider',
    'spinner', 'tabitem', 'treeitem', 'dataitem', 'document', 'splitbutton'})
# Window chrome and composite controls whose internals are not separate targets.
_SKIP_SUBTREE = frozenset({'titlebar', 'scrollbar', 'combobox', 'edit'})
_NEVER_TARGETS = frozenset({'titlebar', 'scrollbar', 'thumb', 'separator', 'tooltip'})
_ACTION_PATTERNS = frozenset({'invoke', 'toggle', 'selectionitem', 'expandcollapse'})
_PATTERNS = ('invoke', 'value', 'rangevalue', 'toggle', 'selectionitem', 'expandcollapse', 'scroll', 'text',
             'selection')
_PATTERN_NAMES = {
    'invoke': ('Invoke', 'IUIAutomationInvokePattern'),
    'value': ('Value', 'IUIAutomationValuePattern'),
    'rangevalue': ('RangeValue', 'IUIAutomationRangeValuePattern'),
    'toggle': ('Toggle', 'IUIAutomationTogglePattern'),
    'selectionitem': ('SelectionItem', 'IUIAutomationSelectionItemPattern'),
    'expandcollapse': ('ExpandCollapse', 'IUIAutomationExpandCollapsePattern'),
    'scroll': ('Scroll', 'IUIAutomationScrollPattern'),
    'text': ('Text', 'IUIAutomationTextPattern'),
    'selection': ('Selection', 'IUIAutomationSelectionPattern'),
}
_TOGGLE_STATES = {0: 'off', 1: 'on', 2: 'mixed'}
_EXPAND_STATES = {0: 'collapsed', 1: 'expanded', 2: 'expanded'}
_ID_RE = re.compile(r'e(\d+)', re.IGNORECASE)
_WORD_RE = re.compile(r'\w+', re.UNICODE)
_MENU_SEPARATORS = re.compile(r'\s*(?:>|→|»)\s*')


class AmbiguousElementError(ValueError):
    """Several controls match a name. ``data`` lists them with fresh snapshot ids."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def normalise_name(name: str) -> str:
    """Case-folded control text without access-key ampersands, accelerator text, trailing colon or ellipsis."""
    text = str(name or '').split('\t', 1)[0].replace('&&', '\0').replace('&', '').replace('\0', '&')
    text = ' '.join(text.split()).casefold()
    while True:
        stripped = text.rstrip(' :.…').strip()
        if stripped == text:
            return text
        text = stripped


def name_words(name: str) -> List[str]:
    return _WORD_RE.findall(normalise_name(name))


def match_names(query: str, names: Sequence[str]) -> List[int]:
    """Indexes of ``names`` matching ``query``: exact normalised matches, else names containing every word."""
    wanted = normalise_name(query)
    if not wanted:
        return []
    exact = [index for index, name in enumerate(names) if normalise_name(name) == wanted]
    if exact:
        return exact
    words = set(name_words(query))
    if not words:
        return []
    return [index for index, name in enumerate(names) if words <= set(name_words(name))]


def _clip(text: Any, limit: int) -> str:
    value = ' '.join(str(text or '').split())
    return value if len(value) <= limit else value[:limit - 1] + '…'


# ---------------------------------------------------------------------------
# Snapshot store: ids are valid for the latest snapshot only
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ElementRef:
    hwnd: int
    path: Tuple[int, ...]
    runtime_id: Tuple[int, ...]
    kind: str
    name: str
    password: bool
    window_texts: Tuple[str, ...] = ()


_STORE_LOCK = threading.Lock()
_STORE: Dict[str, ElementRef] = {}


def _remember(refs: Dict[str, ElementRef]) -> None:
    with _STORE_LOCK:
        _STORE.clear()
        _STORE.update(refs)


def lookup(element_id: str) -> Optional[ElementRef]:
    """The element a snapshot id names in the latest snapshot, or ``None``."""
    with _STORE_LOCK:
        return _STORE.get(str(element_id or '').strip().casefold())


def is_element_id(value: str) -> bool:
    return bool(_ID_RE.fullmatch(str(value or '').strip()))


# ---------------------------------------------------------------------------
# COM and the UIA client
# ---------------------------------------------------------------------------

_MODULE_LOCK = threading.Lock()
_MODULE = None


def _uia_module():
    global _MODULE
    with _MODULE_LOCK:
        if _MODULE is None:
            import comtypes.client
            _MODULE = comtypes.client.GetModule('UIAutomationCore.dll')
        return _MODULE


@contextlib.contextmanager
def com_apartment():
    """COM for this worker thread, released after every UIA object is gone."""
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        owned = True
    except OSError as exc:
        if getattr(exc, 'winerror', None) != _RPC_E_CHANGED_MODE:
            raise
        owned = False  # comtypes already initialised this thread when it was imported here
    try:
        yield
    finally:
        gc.collect()
        if owned:
            comtypes.CoUninitialize()


class _Client:
    def __init__(self):
        import comtypes.client
        self.m = m = _uia_module()
        try:
            self.uia = comtypes.client.CreateObject(m.CUIAutomation8, interface=m.IUIAutomation2)
            self.uia.ConnectionTimeout = CONNECTION_TIMEOUT_MS
            self.uia.TransactionTimeout = TRANSACTION_TIMEOUT_MS
        except Exception:  # noqa: BLE001 - older UIA without timeouts still works
            self.uia = comtypes.client.CreateObject(m.CUIAutomation, interface=m.IUIAutomation)
        self.walker = self.uia.ControlViewWalker
        self.cache = self.uia.CreateCacheRequest()
        self.props = {
            'name': m.UIA_NamePropertyId, 'type': m.UIA_ControlTypePropertyId,
            'enabled': m.UIA_IsEnabledPropertyId, 'password': m.UIA_IsPasswordPropertyId,
            'offscreen': m.UIA_IsOffscreenPropertyId, 'automation_id': m.UIA_AutomationIdPropertyId,
            'runtime_id': m.UIA_RuntimeIdPropertyId, 'value': m.UIA_ValueValuePropertyId,
            'readonly': m.UIA_ValueIsReadOnlyPropertyId, 'toggle': m.UIA_ToggleToggleStatePropertyId,
            'expand': m.UIA_ExpandCollapseExpandCollapseStatePropertyId,
            'selected': m.UIA_SelectionItemIsSelectedPropertyId, 'range': m.UIA_RangeValueValuePropertyId,
            'range_readonly': m.UIA_RangeValueIsReadOnlyPropertyId,
        }
        self.available = {name: getattr(m, f'UIA_Is{_PATTERN_NAMES[name][0]}PatternAvailablePropertyId')
                          for name in _PATTERNS}
        for prop in (*self.props.values(), *self.available.values()):
            self.cache.AddProperty(prop)

    def pattern(self, element, name: str):
        label, interface = _PATTERN_NAMES[name]
        unknown = element.GetCurrentPattern(getattr(self.m, f'UIA_{label}PatternId'))
        if not unknown:
            return None
        return unknown.QueryInterface(getattr(self.m, interface))


def _with_uia(fn: Callable, *args):
    """Run ``fn(client, *args)`` in a COM apartment; UIA failures surface as ``OSError``."""
    from _ctypes import COMError
    with com_apartment():
        try:
            return fn(_Client(), *args)
        except COMError as exc:
            traceback.clear_frames(exc.__traceback__)
            hresult = exc.args[0] if exc.args else 0
            if hresult == _UIA_E_ELEMENTNOTAVAILABLE:
                raise OSError('The control is no longer available; take a new snapshot.') from None
            raise OSError(f'UI Automation call failed (0x{hresult & 0xFFFFFFFF:08X}).') from None
        except BaseException as exc:
            traceback.clear_frames(exc.__traceback__)
            raise


# ---------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    element: Any
    path: Tuple[int, ...]
    runtime_id: Tuple[int, ...]
    kind: str
    name: str
    enabled: bool
    password: bool
    offscreen: bool
    automation_id: str
    patterns: frozenset
    value: Optional[str]
    readonly: bool
    toggle: Optional[int]
    expand: Optional[int]
    selected: Optional[bool]
    range_value: Optional[float] = None
    order: int = 0

    @property
    def interactive(self) -> bool:
        if self.kind in _NEVER_TARGETS:
            return False
        if self.kind in INTERACTIVE_TYPES or self.patterns & _ACTION_PATTERNS:
            return True
        return 'value' in self.patterns and not self.readonly


def _cached(element, prop, default=None):
    try:
        value = element.GetCachedPropertyValue(prop)
    except Exception:  # noqa: BLE001 - a property the provider cannot supply is absent
        return default
    return default if value is None else value


def _node(client: _Client, element, path: Tuple[int, ...]) -> _Node:
    p = client.props
    patterns = frozenset(name for name, prop in client.available.items() if _cached(element, prop, False))
    password = bool(_cached(element, p['password'], False))
    value = None
    if 'value' in patterns and not password:
        raw = _cached(element, p['value'])
        value = str(raw) if raw not in (None, '') else None
    toggle = _cached(element, p['toggle']) if 'toggle' in patterns else None
    expand = _cached(element, p['expand']) if 'expandcollapse' in patterns else None
    selected = bool(_cached(element, p['selected'], False)) if 'selectionitem' in patterns else None
    range_value = _cached(element, p['range']) if 'rangevalue' in patterns else None
    readonly = bool(_cached(element, p['readonly'], False)) if 'value' in patterns else True
    if 'rangevalue' in patterns and 'value' not in patterns:
        readonly = bool(_cached(element, p['range_readonly'], False))
    runtime = _cached(element, p['runtime_id'], ())
    return _Node(
        element=element, path=path, runtime_id=tuple(int(v) for v in (runtime or ())),
        kind=CONTROL_TYPES.get(int(_cached(element, p['type'], 0) or 0), 'custom'),
        name=str(_cached(element, p['name'], '') or ''), enabled=bool(_cached(element, p['enabled'], True)),
        password=password, offscreen=bool(_cached(element, p['offscreen'], False)),
        automation_id=str(_cached(element, p['automation_id'], '') or ''), patterns=patterns, value=value,
        readonly=readonly, toggle=toggle, expand=expand, selected=selected,
        range_value=float(range_value) if isinstance(range_value, (int, float)) else None)


def _children(client: _Client, element, path: Tuple[int, ...]) -> Iterable[_Node]:
    child = client.walker.GetFirstChildElementBuildCache(element, client.cache)
    index = 0
    while child:
        yield _node(client, child, path + (index,))
        child = client.walker.GetNextSiblingElementBuildCache(child, client.cache)
        index += 1


def _walk(client: _Client, root, *, budget: int = VISIT_BUDGET, max_depth: int = MAX_DEPTH,
          seconds: float = WALK_BUDGET_SEC, root_path: Tuple[int, ...] = ()) -> Tuple[List[_Node], bool]:
    """Descendants of ``root`` in document order, bounded by node count, depth and time."""
    deadline = time.monotonic() + seconds
    nodes: List[_Node] = []
    stack: List[Tuple[Any, int]] = []

    def push_children(element, path, depth):
        try:
            kids = list(_children(client, element, path))
        except Exception as exc:  # noqa: BLE001 - one unreadable branch does not end the walk
            debug_log(f'UIA branch skipped ({type(exc).__name__}).', 'windows')
            return
        stack.extend((kid, depth) for kid in reversed(kids))

    push_children(root, root_path, 1)
    truncated = False
    while stack:
        if len(nodes) >= budget or time.monotonic() > deadline:
            truncated = True
            break
        node, depth = stack.pop()
        node.order = len(nodes)
        nodes.append(node)
        if depth < max_depth and node.kind not in _SKIP_SUBTREE:
            push_children(node.element, node.path, depth + 1)
    return nodes, truncated


def _follow(client: _Client, root, path: Sequence[int]):
    element = root
    for index in path:
        child = client.walker.GetFirstChildElementBuildCache(element, client.cache)
        for _ in range(index):
            if not child:
                break
            child = client.walker.GetNextSiblingElementBuildCache(child, client.cache)
        if not child:
            return None
        element = child
    return element


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _user32():
    dll = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'GetForegroundWindow': ([], wintypes.HWND),
        'GetLastActivePopup': ([wintypes.HWND], wintypes.HWND),
        'GetAncestor': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        'IsWindow': ([wintypes.HWND], wintypes.BOOL),
        'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
        'IsWindowEnabled': ([wintypes.HWND], wintypes.BOOL),
        'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        'GetWindowTextLengthW': ([wintypes.HWND], ctypes.c_int),
        'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        'IsIconic': ([wintypes.HWND], wintypes.BOOL),
        'GetMenu': ([wintypes.HWND], wintypes.HMENU),
        'GetMenuItemCount': ([wintypes.HMENU], ctypes.c_int),
        'GetMenuItemInfoW': ([wintypes.HMENU, wintypes.UINT, wintypes.BOOL, ctypes.c_void_p], wintypes.BOOL),
        'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return dll


def _window_pid(user, hwnd: int) -> int:
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _own_pids() -> set:
    """Jarvis's own process, and its parent when that runs the same executable (the desktop app)."""
    pids = {os.getpid()}
    try:
        import psutil
        me = psutil.Process()
        parent = me.parent()
        if parent is not None and os.path.normcase(parent.exe()) == os.path.normcase(me.exe()):
            pids.add(parent.pid)
    except Exception:  # noqa: BLE001 - without process data only this process is excluded
        pass
    return pids


def _user_window(user) -> int:
    """The window the user is working in: the foreground window unless it is Jarvis's own."""
    own = _own_pids()
    foreground = user.GetForegroundWindow()
    if foreground and _window_pid(user, foreground) not in own:
        return int(foreground)
    from .windows_mgmt import list_windows
    for window in list_windows():
        if window.pid not in own:
            return window.hwnd
    raise ValueError('No application window is open.')


def resolve_hwnd(window: str = '') -> int:
    """The window a request addresses; a modal dialog it has open takes its place."""
    user = _user32()
    target = str(window or '').strip()
    if not target:
        hwnd = _user_window(user)
    elif target.isdigit() and user.IsWindow(int(target)) and user.IsWindowVisible(int(target)):
        hwnd = int(target)
    else:
        from .windows_mgmt import list_windows, resolve_window
        hwnd = resolve_window(target, list_windows()).hwnd
    popup = user.GetLastActivePopup(hwnd)
    if popup and int(popup) != hwnd and user.IsWindowVisible(popup) and not user.IsWindowEnabled(hwnd):
        hwnd = int(popup)
    return hwnd


def _process_elevated(pid: int) -> bool:
    """Whether the process runs elevated; ``False`` when its token cannot be read."""
    kernel, advapi = ctypes.windll.kernel32, ctypes.windll.advapi32
    process = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    token = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return False
        try:
            elevated, size = wintypes.DWORD(), wintypes.DWORD()
            ok = advapi.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size))  # TokenElevation
            return bool(ok and elevated.value)
        finally:
            kernel.CloseHandle(token)
    finally:
        kernel.CloseHandle(process)


def window_blocked(hwnd: int) -> bool:
    """Whether UI privilege isolation hides the window from Jarvis: it runs elevated and Jarvis does not."""
    try:
        return _process_elevated(_window_pid(_user32(), hwnd)) and not _process_elevated(os.getpid())
    except (OSError, AttributeError):
        return False


def _require_readable(hwnd: int) -> None:
    if window_blocked(hwnd):
        debug_log('UIA target window runs as administrator; refused.', 'windows')
        raise ValueError('That window runs as administrator, so Windows does not let Jarvis read or control it '
                         'unless Jarvis also runs as administrator.')


# The desktop (Progman, WorkerW) and the taskbars are never what a request is about.
SHELL_WINDOW_CLASSES = frozenset({'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'})


def _foreground_hwnd() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def _window_pid_of(hwnd: int) -> int:
    return _window_pid(_user32(), hwnd)


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_minimised(hwnd: int) -> bool:
    return bool(_user32().IsIconic(hwnd))


def _z_order() -> List[int]:
    from .windows_mgmt import list_windows
    return [window.hwnd for window in list_windows()]


def _window_facts(hwnd: int) -> dict:
    """Process, application name, display and state of a window. Never its title."""
    import psutil
    from pathlib import Path
    from . import displays, windows_mgmt
    from .activity import app_name
    process = psutil.Process(_window_pid_of(hwnd))
    stem = Path(process.name()).stem
    try:
        exe = process.exe()
    except (psutil.Error, OSError):
        exe = ''
    try:
        monitor = displays.monitor_device_for_window(hwnd)
    except OSError:
        monitor = ''
    return {'process': stem, 'application': app_name(exe, stem), 'monitor': monitor,
            'state': windows_mgmt._window_state(hwnd)}


def foreground_target() -> Optional[dict]:
    """The window the user is looking at: ``hwnd``, ``process``, ``application``, ``monitor``, ``state``.

    The foreground window, unless it is Jarvis's own or the Windows shell's; then the highest application
    window behind it that is neither (and not minimised). ``None`` when there is no such window. See
    ``memory/desktop_referents.spec.md`` (Foreground window)."""
    own = _own_pids()

    def excluded(hwnd: int) -> bool:
        try:
            return _window_pid_of(hwnd) in own or _window_class(hwnd) in SHELL_WINDOW_CLASSES
        except OSError:
            return True

    hwnd = _foreground_hwnd()
    if not hwnd or excluded(hwnd):
        hwnd = next((h for h in _z_order() if not excluded(h) and not _window_minimised(h)), 0)
    if not hwnd:
        return None
    return {'hwnd': int(hwnd), **_window_facts(hwnd)}


def window_info(hwnd: int) -> dict:
    user = _user32()
    length = user.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user.GetWindowTextW(hwnd, title, len(title))
    process = ''
    try:
        import psutil
        from pathlib import Path
        process = Path(psutil.Process(_window_pid(user, hwnd)).name()).stem
    except Exception:  # noqa: BLE001 - a process that denies access stays unnamed
        pass
    owner = user.GetAncestor(hwnd, 3) or hwnd  # GA_ROOTOWNER: the application window for a dialog
    return {'hwnd': int(hwnd), 'process': process, 'title': _clip(title.value, TITLE_CHARS),
            'top_hwnd': int(owner)}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _state(node: _Node) -> Optional[str]:
    if node.toggle is not None:
        return _TOGGLE_STATES.get(int(node.toggle))
    if node.selected and node.kind in ('listitem', 'tabitem', 'treeitem', 'radiobutton', 'dataitem'):
        return 'selected'
    if node.expand is not None and node.kind not in ('combobox',):
        return _EXPAND_STATES.get(int(node.expand))
    return None


def describe(node: _Node, ident: str) -> dict:
    item = {'id': ident, 'type': node.kind, 'name': _clip(node.name, NAME_CHARS)}
    if not node.name and node.automation_id and not node.automation_id.isdigit():
        item['hint'] = _clip(node.automation_id, NAME_CHARS)
    if node.password:
        item['password'] = True
    elif node.value is not None and node.value != node.name:
        item['value'] = _clip(node.value, VALUE_CHARS)
    state = _state(node)
    if state:
        item['state'] = state
    if not node.enabled:
        item['enabled'] = False
    if node.offscreen:
        item['offscreen'] = True
    return item


def _window_texts(nodes: Sequence[_Node], exclude: Iterable[str] = ()) -> List[str]:
    taken = {normalise_name(name) for name in exclude}
    texts: List[str] = []
    for node in nodes:
        if node.kind != 'text' or not node.name.strip():
            continue
        key = normalise_name(node.name)
        if key in taken:
            continue
        taken.add(key)
        texts.append(_clip(node.name, TEXT_CHARS))
        if len(texts) >= MAX_TEXTS:
            break
    return texts


def choose_elements(nodes: Sequence[_Node], cap: int = MAX_ELEMENTS) -> Tuple[List[_Node], bool]:
    """Interactive nodes in document order; on-screen ones first when there are more than ``cap``."""
    interactive = [node for node in nodes if node.interactive]
    onscreen = [node for node in interactive if not node.offscreen]
    chosen = onscreen[:cap]
    if len(chosen) < cap:
        chosen += [node for node in interactive if node.offscreen][:cap - len(chosen)]
    keep = {id(node) for node in chosen}
    return [node for node in interactive if id(node) in keep], len(interactive) > cap


def _register(hwnd: int, nodes: Sequence[_Node], texts: Sequence[str]) -> List[dict]:
    refs, described = {}, []
    for index, node in enumerate(nodes, 1):
        ident = f'e{index}'
        refs[ident] = ElementRef(hwnd, node.path, node.runtime_id, node.kind, node.name, node.password, tuple(texts))
        described.append(describe(node, ident))
    _remember(refs)
    return described


def _snapshot(client: _Client, hwnd: int) -> dict:
    _require_readable(hwnd)
    root = client.uia.ElementFromHandle(hwnd)
    nodes, truncated = _walk(client, root)
    chosen, capped = choose_elements(nodes)
    texts = _window_texts(nodes, exclude=[node.name for node in chosen])
    elements = _register(hwnd, chosen, texts)
    debug_log(f'UIA snapshot: {len(elements)} elements of {len(nodes)} nodes'
              f'{" (truncated)" if truncated or capped else ""}.', 'windows')
    info = window_info(hwnd)
    info.pop('top_hwnd', None)
    return {'window': info, 'elements': elements, 'texts': texts, 'truncated': bool(truncated or capped)}


def snapshot(window: str = '') -> dict:
    """Compact list of the window's interactive elements; replaces the previous snapshot's ids."""
    return _with_uia(_snapshot, resolve_hwnd(window))


# ---------------------------------------------------------------------------
# Element resolution
# ---------------------------------------------------------------------------

def _rank(node: _Node) -> Tuple[bool, bool, bool]:
    return (node.interactive, node.enabled, not node.offscreen)


def _by_id(client: _Client, ref: ElementRef, ident: str) -> _Node:
    root = client.uia.ElementFromHandle(ref.hwnd)
    element = _follow(client, root, ref.path)
    if element is not None:
        node = _node(client, element, ref.path)
        if node.runtime_id == ref.runtime_id:
            return node
    nodes, _ = _walk(client, root, budget=SEARCH_BUDGET)
    for node in nodes:
        if node.runtime_id == ref.runtime_id:
            return node
    raise ValueError(f'Element {ident} is no longer in the window; take a new snapshot.')


def name_matches(query: str, candidates: Sequence[_Node]) -> List[_Node]:
    """The controls ``query`` names; one entry means it is resolved, several mean the user must choose.

    Enabled, on-screen, interactive controls break ties only between controls with the same name:
    differently named matches ("Delete message", "Forward message" for "message") are never picked
    between."""
    matches = [candidates[i] for i in match_names(query, [node.name for node in candidates])]
    if len({normalise_name(node.name) for node in matches}) != 1:
        return matches
    best = max(_rank(node) for node in matches)
    return [node for node in matches if _rank(node) == best]


def _by_name(client: _Client, hwnd: int, query: str, accept: Callable[[_Node], bool]) -> _Node:
    root = client.uia.ElementFromHandle(hwnd)
    nodes, _ = _walk(client, root, budget=SEARCH_BUDGET)
    matches = name_matches(query, [node for node in nodes if accept(node)])
    if not matches:
        raise ValueError(f'No control named "{_clip(query, NAME_CHARS)}" was found; take a snapshot to see '
                         'the controls.')
    if len(matches) == 1:
        return matches[0]
    texts = _window_texts(nodes)
    listed = _register(hwnd, matches[:MAX_ELEMENTS], texts)
    raise AmbiguousElementError(
        f'{len(matches)} controls match "{_clip(query, NAME_CHARS)}"; choose one by id.',
        {'error': 'ambiguous', 'candidates': listed})


def _accept_for(action: str) -> Callable[[_Node], bool]:
    if action == 'read':
        return lambda node: bool(node.name)
    if action == 'scroll':
        return lambda node: 'scroll' in node.patterns
    if action == 'select':
        return lambda node: node.interactive or bool(node.patterns & {'selection', 'selectionitem'})
    return lambda node: node.interactive


def _resolve(client: _Client, window: str, element: str, action: str) -> Tuple[int, _Node]:
    element = str(element or '').strip()
    if is_element_id(element):
        ref = lookup(element)
        if ref is None:
            raise ValueError(f'Element {element} is not in the latest snapshot; take a new snapshot.')
        if str(window or '').strip() and resolve_hwnd(window) != ref.hwnd:
            raise ValueError(f'Element {element} belongs to another window; take a snapshot of this one.')
        return ref.hwnd, _by_id(client, ref, element)
    hwnd = resolve_hwnd(window)
    _require_readable(hwnd)
    return hwnd, _by_name(client, hwnd, element, _accept_for(action))


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _require_enabled(node: _Node) -> None:
    if not node.enabled:
        raise ValueError(f'"{_clip(node.name, NAME_CHARS)}" is disabled.')


def _no_input_fallback(node: _Node, action: str) -> ValueError:
    return ValueError(f'"{_clip(node.name, NAME_CHARS) or node.kind}" offers no UI Automation way to {action}. '
                      'Jarvis does not move the mouse or type keys, so this needs your hands.')


def _current(element, prop, default=None):
    try:
        value = element.GetCurrentPropertyValue(prop)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _click(client: _Client, node: _Node) -> dict:
    _require_enabled(node)
    for name in ('invoke', 'toggle', 'selectionitem', 'expandcollapse'):
        if name not in node.patterns:
            continue
        pattern = client.pattern(node.element, name)
        if pattern is None:
            continue
        if name == 'invoke':
            pattern.Invoke()
        elif name == 'toggle':
            pattern.Toggle()
        elif name == 'selectionitem':
            pattern.Select()
        elif int(_current(node.element, client.props['expand'], 0)) == 0:
            pattern.Expand()
        else:
            pattern.Collapse()
        return {'method': name}
    raise _no_input_fallback(node, 'click it')


def _set_text(client: _Client, node: _Node, value: str) -> dict:
    if node.password:
        raise PermissionError('Typing into password fields is not allowed.')
    _require_enabled(node)
    if 'value' not in node.patterns:
        raise _no_input_fallback(node, 'type into it')
    if node.readonly:
        raise ValueError(f'"{_clip(node.name, NAME_CHARS)}" is read-only.')
    client.pattern(node.element, 'value').SetValue(value)
    time.sleep(SETTLE_SEC / 2)
    return {'value': _clip(_current(node.element, client.props['value'], ''), 200)}


def _find_item(client: _Client, node: _Node, option: str) -> Optional[_Node]:
    items, _ = _walk(client, node.element, budget=400, max_depth=4, root_path=node.path)
    items = [item for item in items if 'selectionitem' in item.patterns]
    found = [items[i] for i in match_names(option, [item.name for item in items])]
    if len(found) > 1:
        raise ValueError(f'Several items match "{_clip(option, NAME_CHARS)}".')
    return found[0] if found else None


def _combo_items(client: _Client, node: _Node) -> List[str]:
    items, _ = _walk(client, node.element, budget=400, max_depth=4, root_path=node.path)
    return [item.name for item in items if 'selectionitem' in item.patterns and item.name][:20]


def _select(client: _Client, node: _Node, option: str) -> dict:
    _require_enabled(node)
    if not option:
        if 'selectionitem' not in node.patterns:
            raise ValueError('Give the item to select, or select an item element.')
        client.pattern(node.element, 'selectionitem').Select()
        return {'selected': bool(_current(node.element, client.props['selected'], False))}
    # Composite controls keep their items out of the walk; search inside this one.
    item = _find_item(client, node, option)
    expanded = False
    if item is None and 'expandcollapse' in node.patterns:
        client.pattern(node.element, 'expandcollapse').Expand()
        expanded = True
        time.sleep(SETTLE_SEC)
        item = _find_item(client, node, option)
    try:
        if item is None:
            known = _combo_items(client, node)
            raise ValueError(f'No item "{_clip(option, NAME_CHARS)}"' + (f'; items: {", ".join(known)}.' if known else '.'))
        client.pattern(item.element, 'selectionitem').Select()
    finally:
        if expanded:
            try:
                client.pattern(node.element, 'expandcollapse').Collapse()
            except Exception:  # noqa: BLE001 - the selection usually closes the list itself
                pass
    time.sleep(SETTLE_SEC / 2)
    value = _current(node.element, client.props['value'], None) if 'value' in node.patterns else None
    return {'value': _clip(value if value else item.name, VALUE_CHARS)}


def _toggle(client: _Client, node: _Node, wanted: str) -> dict:
    _require_enabled(node)
    if 'toggle' not in node.patterns:
        raise _no_input_fallback(node, 'toggle it')
    pattern = client.pattern(node.element, 'toggle')
    wanted = wanted.strip().casefold()
    if wanted not in ('', 'on', 'off'):
        raise ValueError('A toggle state is on or off.')
    for _ in range(3 if wanted else 1):
        state = _TOGGLE_STATES.get(int(pattern.CurrentToggleState))
        if wanted and state == wanted:
            break
        pattern.Toggle()
    return {'state': _TOGGLE_STATES.get(int(pattern.CurrentToggleState))}


def _expand(client: _Client, node: _Node) -> dict:
    _require_enabled(node)
    if 'expandcollapse' not in node.patterns:
        raise _no_input_fallback(node, 'expand it')
    pattern = client.pattern(node.element, 'expandcollapse')
    pattern.Expand()
    return {'state': _EXPAND_STATES.get(int(pattern.CurrentExpandCollapseState), 'expanded')}


def _read_text(client: _Client, node: _Node) -> str:
    if 'text' in node.patterns:
        pattern = client.pattern(node.element, 'text')
        if pattern is not None:
            return str(pattern.DocumentRange.GetText(READ_CHARS + 1) or '')
    if node.value is not None:
        return node.value
    return node.name


def _read(client: _Client, node: _Node) -> dict:
    if node.password:
        raise PermissionError('Reading password fields is not allowed.')
    text = _read_text(client, node)
    return {'text': text[:READ_CHARS], 'truncated': len(text) > READ_CHARS}


def _read_window(client: _Client, hwnd: int) -> dict:
    nodes, _ = _walk(client, client.uia.ElementFromHandle(hwnd))
    parts = list(_window_texts(nodes))
    for node in nodes:
        if node.kind == 'document' and not node.password:
            parts.append(_read_text(client, node))
            break
    text = '\n'.join(part for part in parts if part)
    return {'text': text[:READ_CHARS], 'truncated': len(text) > READ_CHARS}


_SCROLL = {'down': (2, 3), 'up': (2, 0), 'right': (3, 2), 'left': (0, 2)}  # (horizontal, vertical) ScrollAmount


def _scroll(client: _Client, node: _Node, direction: str) -> dict:
    direction = direction.strip().casefold() or 'down'
    if 'scroll' not in node.patterns:
        raise _no_input_fallback(node, 'scroll it')
    pattern = client.pattern(node.element, 'scroll')
    if direction in _SCROLL:
        horizontal, vertical = _SCROLL[direction]
        pattern.Scroll(horizontal, vertical)
    elif direction in ('top', 'bottom'):
        pattern.SetScrollPercent(-1, 0 if direction == 'top' else 100)
    else:
        raise ValueError('Scroll up, down, left, right, top or bottom.')
    time.sleep(SETTLE_SEC / 2)
    vertical = float(pattern.CurrentVerticalScrollPercent)
    horizontal = float(pattern.CurrentHorizontalScrollPercent)
    return {'vertical_percent': round(vertical) if vertical >= 0 else None,
            'horizontal_percent': round(horizontal) if horizontal >= 0 else None}


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------

class _MENUITEMINFOW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('fMask', wintypes.UINT), ('fType', wintypes.UINT),
                ('fState', wintypes.UINT), ('wID', wintypes.UINT), ('hSubMenu', wintypes.HMENU),
                ('hbmpChecked', wintypes.HBITMAP), ('hbmpUnchecked', wintypes.HBITMAP),
                ('dwItemData', ctypes.c_size_t), ('dwTypeData', wintypes.LPWSTR), ('cch', wintypes.UINT),
                ('hbmpItem', wintypes.HBITMAP)]


_MIIM_STATE, _MIIM_ID, _MIIM_SUBMENU, _MIIM_STRING, _MIIM_FTYPE = 0x1, 0x2, 0x4, 0x40, 0x100
_MFT_SEPARATOR, _MFS_DISABLED = 0x800, 0x3


@dataclass(frozen=True)
class MenuItem:
    text: str
    command: int
    submenu: int
    disabled: bool


def win32_menu_items(user, menu: int) -> List[MenuItem]:
    items = []
    for index in range(max(0, user.GetMenuItemCount(menu))):
        info = _MENUITEMINFOW(cbSize=ctypes.sizeof(_MENUITEMINFOW),
                              fMask=_MIIM_STATE | _MIIM_ID | _MIIM_SUBMENU | _MIIM_STRING | _MIIM_FTYPE)
        buffer = ctypes.create_unicode_buffer(256)
        info.dwTypeData = ctypes.cast(buffer, wintypes.LPWSTR)
        info.cch = len(buffer)
        if not user.GetMenuItemInfoW(menu, index, True, ctypes.byref(info)) or info.fType & _MFT_SEPARATOR:
            continue
        items.append(MenuItem(buffer.value, int(info.wID), int(info.hSubMenu or 0), bool(info.fState & _MFS_DISABLED)))
    return items


def _segments(path: str) -> List[str]:
    segments = [segment.strip() for segment in _MENU_SEPARATORS.split(str(path or '')) if segment.strip()]
    if not segments:
        raise ValueError('Give a menu path such as "File > Save As".')
    return segments


def _plain(name: str) -> str:
    """A menu item's text as shown: no access-key ampersands, no accelerator after the tab."""
    return name.split('\t', 1)[0].replace('&&', '\0').replace('&', '').replace('\0', '&').strip()


def _display(name: str) -> str:
    return _clip(_plain(name), 30)


RefuseItem = Optional[Callable[[str, str], bool]]


def _pick_menu_item(items: Sequence[Any], names: Sequence[str], segment: str, refuse_item: RefuseItem = None):
    """The one item ``segment`` names. ``refuse_item(requested, actual)`` vetoes an item the request reached
    only by some of its words, so a safety check made on the requested words cannot be bypassed."""
    found = match_names(segment, names)
    if len(found) == 1:
        actual = _plain(names[found[0]])
        if refuse_item is not None and refuse_item(segment, actual):
            raise ValueError(f'"{_clip(segment, NAME_CHARS)}" is the menu item "{_clip(actual, NAME_CHARS)}", '
                             'which may not be undoable. Ask for it by its full name.')
        return items[found[0]]
    shown = ', '.join(normalise_name(name).capitalize() if not name else _clip(name.split('\t')[0].replace('&', ''), 30)
                      for name in names if name)
    if not found:
        raise ValueError(f'No menu item "{_clip(segment, NAME_CHARS)}". Items here: {shown}.')
    raise ValueError(f'Several menu items match "{_clip(segment, NAME_CHARS)}". Items here: {shown}.')


def _win32_menu(hwnd: int, segments: Sequence[str], refuse_item: RefuseItem = None) -> Optional[dict]:
    """Run a command from a classic menu bar by posting WM_COMMAND; ``None`` when UIA must open it."""
    user = _user32()
    menu = user.GetMenu(hwnd)
    if not menu:
        return None
    for position, segment in enumerate(segments):
        items = win32_menu_items(user, menu)
        if not items:
            return None  # built lazily when opened: only the UI can show it
        item = _pick_menu_item(items, [item.text for item in items], segment, refuse_item)
        last = position == len(segments) - 1
        if item.submenu:
            if last:
                return None  # "open the Format menu" needs the menu on screen
            menu = item.submenu
            continue
        if not last:
            raise ValueError(f'"{_clip(segment, NAME_CHARS)}" is a command, not a menu.')
        if item.disabled:
            raise ValueError(f'"{_display(item.text)}" is disabled.')
        if not user.PostMessageW(hwnd, _WM_COMMAND, item.command & 0xFFFF, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'method': 'command'}
    return None


def _menu_scopes(client: _Client, hwnd: int) -> List[Any]:
    """The window and the other top-level windows of its process: open menus and flyouts live there."""
    scopes = [client.uia.ElementFromHandle(hwnd)]
    m = client.m
    condition = client.uia.CreatePropertyCondition(m.UIA_ProcessIdPropertyId, _window_pid(_user32(), hwnd))
    popups = client.uia.GetRootElement().FindAll(m.TreeScope_Children, condition)
    for index in range(popups.Length if popups else 0):
        popup = popups.GetElement(index)
        if int(popup.CurrentNativeWindowHandle or 0) != hwnd:
            scopes.append(popup)
    return scopes


def _menu_items(client: _Client, hwnd: int) -> List[_Node]:
    items: List[_Node] = []
    for scope in _menu_scopes(client, hwnd):
        nodes, _ = _walk(client, scope, budget=800)
        items += [node for node in nodes if node.kind == 'menuitem']
    return items


def _uia_menu(client: _Client, hwnd: int, segments: Sequence[str], refuse_item: RefuseItem = None) -> dict:
    for position, segment in enumerate(segments):
        items = _menu_items(client, hwnd)
        if position and not match_names(segment, [node.name for node in items]):
            time.sleep(SETTLE_SEC)  # a submenu that was just expanded can take a moment to appear
            items = _menu_items(client, hwnd)
        item = _pick_menu_item(items, [node.name for node in items], segment, refuse_item)
        _require_enabled(item)
        last = position == len(segments) - 1
        if last and 'invoke' in item.patterns:
            client.pattern(item.element, 'invoke').Invoke()
            return {'method': 'ui', 'opened': False}
        if 'expandcollapse' not in item.patterns:
            raise _no_input_fallback(item, 'open it')
        client.pattern(item.element, 'expandcollapse').Expand()
        time.sleep(SETTLE_SEC)
        if last:
            return {'method': 'ui', 'opened': True}
    return {'method': 'ui'}


def _menu(client: _Client, hwnd: int, path: str, refuse_item: RefuseItem = None) -> dict:
    segments = _segments(path)
    result = _win32_menu(hwnd, segments, refuse_item)
    if result is None:
        result = _uia_menu(client, hwnd, segments, refuse_item)
    return {**result, 'path': ' > '.join(segments)}


def _win32_menu_names(hwnd: int, segments: Sequence[str]) -> Optional[List[str]]:
    user = _user32()
    menu = user.GetMenu(hwnd)
    if not menu:
        return None
    names: List[str] = []
    for segment in segments:
        items = win32_menu_items(user, menu) if menu else []
        found = match_names(segment, [item.text for item in items])
        if len(found) != 1:
            break
        names.append(_plain(items[found[0]].text))
        menu = items[found[0]].submenu
    return names


def _menu_names(client: _Client, hwnd: int, segments: Sequence[str]) -> List[str]:
    names = _win32_menu_names(hwnd, segments)
    if names is not None:
        return names
    names = []
    items = _menu_items(client, hwnd)  # only what is on screen: a submenu is never opened to look inside
    for segment in segments:
        found = match_names(segment, [node.name for node in items])
        if len(found) != 1:
            break
        names.append(_plain(items[found[0]].name))
    return names


def menu_item_names(window: str = '', path: str = '') -> List[str]:
    """The real names of the menu items ``path`` leads to, as far as they can be read without opening a menu
    (a classic menu bar is read whole; otherwise only items already on screen). Shorter than the path when the
    rest is unknown."""
    segments = _segments(path)
    return _with_uia(lambda client: _menu_names(client, resolve_hwnd(window), segments))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

ACTIONS = ('click', 'set_text', 'select', 'toggle', 'expand', 'menu', 'read', 'scroll')


def _act(client: _Client, action: str, window: str, element: str, value: str,
         refuse_item: RefuseItem = None) -> dict:
    if action == 'menu':
        hwnd = resolve_hwnd(window)
        result = _menu(client, hwnd, value, refuse_item)
        target = None
    elif not element and action in ('read', 'scroll'):
        hwnd = resolve_hwnd(window)
        if action == 'read':
            return {'action': action, **_read_window(client, hwnd), 'window': window_info(hwnd)}
        nodes, _ = _walk(client, client.uia.ElementFromHandle(hwnd), budget=SEARCH_BUDGET)
        scrollable = [node for node in nodes if 'scroll' in node.patterns]
        if not scrollable:
            raise ValueError('Nothing in this window can be scrolled through UI Automation.')
        target = scrollable[0]
        result = _scroll(client, target, value)
    else:
        if not element:
            raise ValueError('Name the control, or give its id from a snapshot.')
        hwnd, target = _resolve(client, window, element, action)
        handler = {
            'click': lambda: _click(client, target),
            'set_text': lambda: _set_text(client, target, value),
            'select': lambda: _select(client, target, value),
            'toggle': lambda: _toggle(client, target, value),
            'expand': lambda: _expand(client, target),
            'read': lambda: _read(client, target),
            'scroll': lambda: _scroll(client, target, value),
        }[action]
        result = handler()
    debug_log(f'UIA {action} done{f" on a {target.kind}" if target else ""}.', 'windows')
    data = {'action': action, **result, 'window': window_info(hwnd)}
    if target is not None:
        data['element'] = {'type': target.kind, 'name': _clip(target.name, NAME_CHARS)}
    return data


def act(action: str, window: str = '', element: str = '', value: str = '', refuse_item: RefuseItem = None) -> dict:
    """Perform one pattern-based action. Raises ``ValueError`` for requests that cannot be carried out (including
    a menu item ``refuse_item`` vetoes), ``PermissionError`` for password fields and ``OSError`` for UI
    Automation failures."""
    action = 'click' if action == 'invoke' else action
    if action not in ACTIONS:
        raise ValueError('Unsupported action.')
    return _with_uia(_act, action, window, str(element or ''), str(value or ''), refuse_item)


def _inspect(client: _Client, window: str, element: str, wants_texts: Callable[[str], bool]) -> dict:
    hwnd, node = _resolve(client, window, element, 'inspect')
    texts: List[str] = []
    if wants_texts(node.name):
        nodes, _ = _walk(client, client.uia.ElementFromHandle(hwnd))
        texts = _window_texts(nodes)
    info = window_info(hwnd)
    return {'name': node.name, 'type': node.kind, 'password': node.password, 'texts': texts,
            'process': info['process']}


def inspect(window: str = '', element: str = '', wants_texts: Callable[[str], bool] = lambda _name: False) -> dict:
    """Name, type and password flag of the addressed element (and, when ``wants_texts(name)``, the static
    texts of its window). A snapshot id is answered from the store without touching the window."""
    ref = lookup(element) if is_element_id(element) else None
    if ref is not None:
        return {'name': ref.name, 'type': ref.kind, 'password': ref.password,
                'texts': list(ref.window_texts) if wants_texts(ref.name) else [],
                'process': window_info(ref.hwnd)['process']}
    return _with_uia(_inspect, window, str(element or ''), wants_texts)


# ---------------------------------------------------------------------------
# Viewer page boxes and address bars (used by the PDF navigator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageBoxCandidate:
    automation_id: str
    value: Optional[float]


def choose_page_box(candidates: Sequence[PageBoxCandidate], page_count: int) -> Optional[int]:
    """Index of the one control showing a page number, preferring one whose automation id names a page."""
    numeric = [index for index, candidate in enumerate(candidates)
               if candidate.value is not None and float(candidate.value).is_integer()
               and 1 <= candidate.value <= page_count]
    preferred = [index for index in numeric if 'page' in candidates[index].automation_id.casefold()]
    for group in (preferred, numeric):
        if len(group) == 1:
            return group[0]
    return None


def _numeric(text: Optional[str]) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _page_boxes(client: _Client, hwnd: int) -> List[_Node]:
    nodes, _ = _walk(client, client.uia.ElementFromHandle(hwnd), budget=SEARCH_BUDGET)
    return [node for node in nodes if node.kind in ('edit', 'spinner', 'combobox') and not node.password
            and node.enabled and not node.readonly and node.patterns & {'value', 'rangevalue'}]


def _box_value(node: _Node) -> Optional[float]:
    return node.range_value if 'rangevalue' in node.patterns and node.range_value is not None else _numeric(node.value)


def _set_page(client: _Client, hwnd: int, page: int, page_count: int) -> dict:
    boxes = _page_boxes(client, hwnd)
    index = choose_page_box([PageBoxCandidate(node.automation_id, _box_value(node)) for node in boxes], page_count)
    if index is None:
        raise LookupError('No page number box was found in the viewer.')
    box = boxes[index]
    if 'rangevalue' in box.patterns:
        client.pattern(box.element, 'rangevalue').SetValue(float(page))
    else:
        client.pattern(box.element, 'value').SetValue(str(page))
    time.sleep(SETTLE_SEC)
    if 'rangevalue' in box.patterns:
        shown = _current(box.element, client.props['range'], None)
    else:
        shown = _numeric(_current(box.element, client.props['value'], None))
    if shown is None or int(shown) != page:
        raise LookupError('The viewer did not accept the page number.')
    debug_log('PDF page set through the viewer page box.', 'windows')
    return {'page': page, 'method': 'page_box'}


def set_page_box(hwnd: int, page: int, page_count: int) -> dict:
    """Set the viewer's page-number box. ``LookupError`` when there is no usable one."""
    return _with_uia(_set_page, int(hwnd), int(page), int(page_count))


def _address_values(client: _Client, hwnd: int) -> List[str]:
    nodes, _ = _walk(client, client.uia.ElementFromHandle(hwnd), budget=400, max_depth=10)
    return [node.value for node in nodes if node.kind == 'edit' and node.value and not node.password]


def address_bar_values(hwnd: int) -> List[str]:
    """Values of the window's text fields (a browser's address bar among them)."""
    return _with_uia(_address_values, int(hwnd))


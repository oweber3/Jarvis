"""Thin builtin tool adapters for local Windows control."""
from dataclasses import dataclass
import json
from pathlib import PureWindowsPath
from typing import Callable, Optional

from ...base import Tool
from ...types import ToolExecutionResult
from ....debug import debug_log
from ....platform.windows.apps import PartialPlacementError
from ....platform.windows.files import AmbiguousTargetError
from ....utils.redact import redact

PLACEMENT_FIELDS = ('monitor', 'zone', 'state')
PLACEMENT_PROPERTIES = {
    'monitor': {'type': 'string', 'description': (
        'Which display: "1", "2", "primary", "left", "right", or an identifier or alias from '
        'windowControl displays. Omit for the main display.')},
    'zone': {'type': 'string', 'description': (
        'Zone name or number on that display, e.g. "left" or "2", as listed by windowControl displays. '
        'Not with state maximise.')},
    'state': {'type': 'string', 'enum': ['restore', 'maximise'], 'description': (
        'restore (default) places a normal window; maximise fills the destination display.')},
}
# The display a placement uses when none is named.
DEFAULT_MONITOR = 'primary'


def placement_request(given, always=False):
    """Validate the given monitor/zone/state; ``None`` when nothing asks for a placement.

    Without a monitor the main display is the destination. ``always`` places even with no field."""
    if any(not isinstance(value, str) for value in given.values()):
        raise ValueError('Placement options must be text.')
    if not given and not always:
        return None
    state = given.get('state', 'restore')
    if state not in ('restore', 'maximise'):
        raise ValueError('Unsupported state; use restore or maximise.')
    if state == 'maximise' and 'zone' in given:
        raise ValueError('A zone cannot be combined with maximise.')
    return {'monitor': given.get('monitor', DEFAULT_MONITOR), 'zone': given.get('zone'), 'state': state}


def _redact_data(value):
    """Scrub scalar strings without truncating or corrupting the JSON envelope."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_data(item) for key, item in value.items()}
    return value


# Bounded like every OS call; a pending launch is resolved when a follow-up reads the referents.
_RESOLVE_TIMEOUT_SEC = 2.0
_REFERENT_STATES = {'restore': 'normal', 'maximise': 'maximised', 'minimise': 'minimised',
                    'maximised': 'maximised', 'close_requested': 'closing'}
_PLACED_ACTIONS = ('opened_and_placed', 'website_opened_and_placed', 'placed')
_REFERENT_ACTIONS = {'opened_and_placed': 'open', 'website_opened_and_placed': 'open', 'placed': 'place',
                     'close_requested': 'close', 'focus': 'focus', 'minimise': 'minimise', 'maximise': 'maximise',
                     'restore': 'restore', 'moved_to_desktop': 'move_desktop'}


@dataclass(frozen=True)
class _Launched:
    """A plain launch: the model-facing result, and how to find its window later (kept local)."""
    data: dict
    process: str = ''
    resolve: Optional[Callable[[], Optional[dict]]] = None


def _bounded(resolve):
    from ....platform.windows._bounded import run_bounded
    return lambda: run_bounded(resolve, _RESOLVE_TIMEOUT_SEC)


def _remember(target, data, launch=None):
    """Record which window an action affected, so a follow-up can refer to it without naming it.

    Only outcomes that changed or opened a window are recorded, never titles or paths. See
    ``memory/desktop_referents.spec.md``."""
    from ....memory.desktop_referents import DesktopReferent, get_desktop_referents
    named = '' if target.strip().isdigit() else target.strip()
    if launch is not None:
        referent = DesktopReferent(application=data.get('application') or named, process=launch.process,
                                   last_action='open')
        get_desktop_referents().record(referent, resolve=_bounded(launch.resolve) if launch.resolve else None)
        return
    if data.get('launch') == 'accepted':
        # The application opened but was not placed: it is still what the user is talking about.
        referent = DesktopReferent(application=data.get('application') or named, hwnd=data.get('hwnd'),
                                   last_action='open')
    elif data.get('action') in _REFERENT_ACTIONS and data.get('hwnd') is not None:
        action = data['action']
        state = data.get('state', action) if action in _PLACED_ACTIONS else action
        referent = DesktopReferent(
            application=data.get('application') or named, process=data.get('process') or '',
            hwnd=int(data['hwnd']), monitor=data.get('monitor') or '', zone=data.get('zone') or '',
            state=_REFERENT_STATES.get(state, ''), last_action=_REFERENT_ACTIONS[action])
    else:
        return
    get_desktop_referents().record(referent)


def _remember_safely(target, data, launch=None):
    try:
        _remember(target, data, launch)
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f'desktop referent not recorded ({type(exc).__name__}).', 'windows')


class WindowsTool(Tool):
    """Validate the small action surface and scrub OS data before model consumption."""
    actions = ()
    # Actions that accept monitor/zone/state, and those that always place (on the main display by default).
    placement_actions = ()
    placement_required = ()
    untargeted_actions = ('list',)
    # Extra text arguments (name -> schema) and the actions that accept each.
    extra_properties = {}
    extra_actions = {}
    # Window actions leave a desktop referent for follow-ups (memory/desktop_referents.spec.md).
    remembers_windows = False
    timeout_sec = 12
    target_description = 'Application name or decimal window handle from list. Use an empty target for list.'
    action_description = ''

    @property
    def inputSchema(self):
        properties = {'target': {'type': 'string', 'description': self.target_description}}
        if self.actions:
            properties['action'] = {'type': 'string', 'enum': list(self.actions),
                                    'description': self.action_description}
        if self.placement_actions:
            properties.update(PLACEMENT_PROPERTIES)
        properties.update(self.extra_properties)
        return {'type': 'object', 'properties': properties, 'additionalProperties': False,
                'required': ['action', 'target'] if self.actions else ['target']}

    def returns_outside_content_for(self, args):
        # Window titles are written by the applications and the pages they show (routines.spec.md).
        return 'list' in self.actions and isinstance(args, dict) and args.get('action') == 'list'

    def _placement_request(self, action, args):
        """Validate monitor/zone/state for this action; ``None`` when no placement is requested."""
        given = {key: args[key] for key in PLACEMENT_FIELDS if args.get(key) not in (None, '')}
        if action not in self.placement_actions:
            if given:
                raise ValueError('Placement options are not supported for this action. To move a window that is '
                                 'already open, use windowControl place; to launch and place, use appControl open.')
            return None
        return placement_request(given, always=action in self.placement_required)

    def _extra_request(self, action, args):
        """Validate the extra text arguments: each is only accepted by the actions that declare it."""
        given = {key: args[key] for key in self.extra_properties if args.get(key) not in (None, '')}
        for key, value in given.items():
            if action not in self.extra_actions.get(key, ()):
                raise ValueError(f'The {key} option is not supported for this action.')
            if not isinstance(value, str):
                raise ValueError(f'The {key} option must be text.')
        return given

    def run(self, args, context):
        try:
            if not context.cfg.windows_tools_enabled:
                raise ValueError('Windows control is disabled.')
            # `match` is a routing hint for deterministic fast commands. It is
            # intentionally absent from the model-facing schema.
            allowed = ({'action', 'target', 'match', *(PLACEMENT_FIELDS if self.placement_actions else ()),
                        *self.extra_properties} if self.actions
                       else {'target', *(PLACEMENT_FIELDS if self.placement_actions else ())})
            if not isinstance(args, dict) or set(args) - allowed:
                raise ValueError('Invalid Windows tool arguments.')
            action = args.get('action')
            if self.actions and action not in self.actions:
                raise ValueError('Unsupported action.')
            target = args.get('target', '')
            if not isinstance(target, str) or (action not in self.untargeted_actions and not target.strip()):
                raise ValueError('A target is required.')
            if action == 'displays' and target.strip():
                raise ValueError('The displays action takes an empty target.')
            placement = self._placement_request(action, args) if self.actions or self.placement_actions else None
            match = args.get('match')
            if match not in (None, 'process'):
                raise ValueError('Unsupported match mode.')
            options = {'process_only': True} if match == 'process' else {}
            if placement:
                options['placement'] = placement
            options.update(self._extra_request(action, args))
            from ....platform.windows._bounded import run_bounded
            outcome = run_bounded(lambda: self._operate(action, target, context.cfg, **options), self.timeout_sec)
            launch = outcome if isinstance(outcome, _Launched) else None
            data = launch.data if launch else outcome
            text = json.dumps(_redact_data(data), ensure_ascii=False)
            if self.remembers_windows:
                _remember_safely(target, data, launch)
            context.user_print('🪟 Windows operation completed.')
            return ToolExecutionResult(success=True, reply_text=text)
        except PartialPlacementError as exc:
            debug_log(f'{self.name} launched but did not complete ({exc.data.get("placement") or "partial"}).', 'windows')
            if exc.data.get('candidates'):
                # The candidates carry window titles: outside content (routines.spec.md).
                from ...request_scope import note_outside_content
                note_outside_content()
            text = json.dumps(_redact_data(exc.data), ensure_ascii=False)
            _remember_safely(target, exc.data)
            return ToolExecutionResult(success=False, reply_text=text, error_message=redact(str(exc)))
        except AmbiguousTargetError as exc:
            debug_log(f'{self.name} found several matches ({len(exc.data["candidates"])}).', 'windows')
            return ToolExecutionResult(success=False, reply_text=json.dumps(_redact_data(exc.data), ensure_ascii=False),
                                       error_message=redact(str(exc)))
        except (OSError, ValueError, TypeError) as exc:
            debug_log(f'{self.name} failed ({type(exc).__name__}).', 'windows')
            message = redact(str(exc))
            return ToolExecutionResult(success=False, reply_text=None, error_message=message)


def _window_target(target, cfg):
    """An application alias names its executable stem; other targets pass through."""
    aliases = {key.casefold(): value for key, value in cfg.windows_app_aliases.items()}
    if target.strip().casefold() not in aliases:
        return target
    from ....platform.windows.apps import APP_INDEX, resolve_application
    app = resolve_application(target, APP_INDEX.applications(), cfg.windows_app_aliases)
    if not app.executable:
        raise ValueError('This application alias has no discovered executable; use a window handle from list.')
    return PureWindowsPath(app.executable).stem


def _fancyzone_sets(cfg, monitors):
    """FancyZones zones per display, or none when the user switched them off or PowerToys has none."""
    if not cfg.windows_fancyzones_enabled:
        return {}
    from ....platform.windows import displays
    return displays.fancyzone_sets(monitors, (*cfg.fast_commands_locales, 'en'))


def _resolve_placement(cfg, placement):
    """Resolve the requested display and zone against the live monitors, configuration and FancyZones."""
    from ....platform.windows import displays
    monitors = displays.list_monitors()
    monitor = displays.resolve_monitor(placement['monitor'], monitors, cfg.windows_monitor_aliases)
    label = rectangle = None
    if placement.get('zone'):
        fancy = _fancyzone_sets(cfg, monitors).get(monitor.device)
        label, rectangle = displays.resolve_zone(cfg.windows_window_zones, monitor, placement['zone'], fancy)
    return monitor, rectangle, label, placement['state']


class AppControlTool(WindowsTool):
    name = 'appControl'
    remembers_windows = True
    description = 'Open Word, Chrome, MATLAB, a Steam game or other installed apps. Switch to/focus Spotify or another running app. Close apps; list windows.'
    actions = ('open', 'close', 'focus', 'list')
    placement_actions = ('open',)
    action_description = ('open launches an installed app (with monitor, optionally zone and state, it also '
                          'places its window); focus switches to an already open app; close requests graceful '
                          'close; list shows open windows.')
    target_description = 'Friendly application name, e.g. Word, Chrome, MATLAB or Spotify. For existing windows a decimal handle is also accepted. Empty for list.'

    def fast_targets(self, cfg):
        """Expose ready catalogue names to routing without OS calls or waiting."""
        from ....fastpath.matcher import FastTarget
        from ....platform.windows.apps import APP_INDEX, resolve_application
        apps = APP_INDEX.snapshot()
        if apps is None:
            return ()
        alias_names = {}
        alias_targets = {}
        for alias in cfg.windows_app_aliases:
            try:
                app = resolve_application(alias, apps, cfg.windows_app_aliases)
                alias_names.setdefault(app.target, []).append(alias)
                alias_targets[alias.casefold()] = app
            except ValueError:
                continue
        # Index the resolver's exact-name and executable precedence once.
        exact_targets, launcher_targets = {}, {}
        for app in apps:
            for value in (app.name, app.target):
                exact_targets.setdefault(value.casefold(), set()).add(app.target)
            if app.executable:
                stem = PureWindowsPath(app.executable).stem.casefold()
                launcher_targets.setdefault(stem, set()).add(app.target)
        alias_keys = {key.casefold() for key in cfg.windows_app_aliases}
        grouped = {}
        for app in apps:
            stem = PureWindowsPath(app.executable).stem if app.executable else ''
            names = [app.name, *alias_names.get(app.target, [])]
            if stem:
                names.append(stem)
            # The executor applies aliases too; every offered name must resolve
            # back to this application under that same contract.
            faithful_names = []
            for name in names:
                key = name.casefold()
                if key in alias_keys:
                    resolved = alias_targets.get(key)
                    resolved_targets = {resolved.target} if resolved else set()
                else:
                    resolved_targets = exact_targets.get(key) or launcher_targets.get(key, set())
                if resolved_targets == {app.target}:
                    faithful_names.append(name)
            launch_name = next((name for name in (app.name, stem)
                                if name and name in faithful_names), '')
            window_stem = stem
            if stem.casefold() in alias_keys:
                resolved = alias_targets.get(stem.casefold())
                if resolved is None or resolved.executable.casefold() != app.executable.casefold():
                    window_stem = ''
            identity = str(PureWindowsPath(app.executable or app.target)).casefold()
            if identity in grouped:
                previous = grouped[identity]
                grouped[identity] = FastTarget(previous.names + tuple(faithful_names),
                                               previous.open_target or launch_name, previous.window_target,
                                               previous.display)
            else:
                grouped[identity] = FastTarget(tuple(faithful_names), launch_name, window_stem, app.name)
        return tuple(grouped.values())

    def _operate(self, action, target, cfg, process_only=False, placement=None):
        if action == 'open' and placement:
            from ....platform.windows.apps import open_application_placed
            monitor, rectangle, label, state = _resolve_placement(cfg, placement)
            extra = {'zone': label} if label else {}
            try:
                result = open_application_placed(target, cfg.windows_app_aliases, monitor, rectangle, state)
            except PartialPlacementError as exc:
                exc.data.update(monitor=monitor.device, **extra)
                raise
            return {**result, **extra}
        if action == 'open':
            from ....platform.windows.apps import open_application, track_launch
            try:
                process, resolve = track_launch(target, cfg.windows_app_aliases)
            except Exception as exc:  # noqa: BLE001 - an unknown app fails in the launch below
                debug_log(f'Launch tracking unavailable ({type(exc).__name__}).', 'windows')
                process, resolve = '', None
            return _Launched(open_application(target, cfg.windows_app_aliases), process, resolve)
        from ....platform.windows.windows_mgmt import control_window
        if action != 'list':
            target = _window_target(target, cfg)
        return control_window(action, target, process_only=process_only)


class WindowControlTool(WindowsTool):
    name = 'windowControl'
    remembers_windows = True
    description = ('Minimise Chrome or another app window to the taskbar; maximise or restore its window size; '
                   'list windows; list displays with their zones; place an existing window on a chosen display and '
                   'zone; switch, create or close virtual desktops and move a window to another desktop.')
    actions = ('minimise', 'maximise', 'restore', 'list', 'displays', 'place',
               'desktops', 'desktop_switch', 'desktop_new', 'desktop_close', 'move_to_desktop')
    placement_actions = ('place',)
    placement_required = ('place',)
    untargeted_actions = ('list', 'displays', 'desktops', 'desktop_switch', 'desktop_new', 'desktop_close')
    extra_properties = {'desktop': {'type': 'string', 'description': (
        'Virtual desktop: a number such as "2", "next" or "previous". Needed by desktop_switch and '
        'move_to_desktop; optional for desktop_close (default is the current desktop).')}}
    extra_actions = {'desktop': ('desktop_switch', 'desktop_close', 'move_to_desktop')}
    action_description = ('minimise hides a window in the taskbar; maximise fills its current display; restore '
                          'returns normal size; list shows open windows; displays lists connected displays, '
                          'their work areas, aliases and zones (empty target); place moves the target window onto '
                          'a monitor, optionally into a zone, or maximised on it; desktops reports how many '
                          'virtual desktops exist and which is current; desktop_switch goes to a desktop; '
                          'desktop_new creates one and switches to it; desktop_close closes one (its windows move '
                          'to a neighbour, none is closed); move_to_desktop moves the target window to a desktop.')

    def _operate(self, action, target, cfg, process_only=False, placement=None, desktop=None):
        from ....platform.windows import displays, virtual_desktops
        if action in ('desktops', 'desktop_switch', 'desktop_new', 'desktop_close', 'move_to_desktop'):
            return self._operate_desktops(action, target, cfg, process_only, desktop, virtual_desktops)
        if action == 'displays':
            monitors = displays.list_monitors()
            return displays.describe_displays(monitors, cfg.windows_monitor_aliases, cfg.windows_window_zones,
                                              _fancyzone_sets(cfg, monitors))
        if action == 'place':
            from ....platform.windows.windows_mgmt import place_target_window
            monitor, rectangle, label, state = _resolve_placement(cfg, placement)
            result = place_target_window(_window_target(target, cfg), monitor, rectangle, state,
                                         process_only=process_only)
            return {**result, **({'zone': label} if label else {})}
        from ....platform.windows.windows_mgmt import control_window
        if action != 'list':
            target = _window_target(target, cfg)
        return control_window(action, target, process_only=process_only)

    def _operate_desktops(self, action, target, cfg, process_only, desktop, virtual_desktops):
        if action in ('desktop_switch', 'move_to_desktop') and not desktop:
            raise ValueError('A desktop number, next or previous is required.')
        if action == 'desktops':
            state = virtual_desktops.state()
            return {'count': state.count, 'current': state.current}
        if action == 'move_to_desktop':
            from ....platform.windows import windows_mgmt
            window = windows_mgmt.resolve_window(_window_target(target, cfg), windows_mgmt.list_windows(),
                                                 process_only=process_only)
            moved = virtual_desktops.move_window(window.hwnd, desktop)
            return {'action': 'moved_to_desktop', 'hwnd': window.hwnd, 'process': window.process,
                    'desktop': moved['desktop']}
        if action == 'desktop_switch':
            state, label = virtual_desktops.switch(desktop), 'desktop_switched'
        elif action == 'desktop_new':
            state, label = virtual_desktops.create(), 'desktop_created'
        else:
            state, label = virtual_desktops.close(desktop or ''), 'desktop_closed'
        return {'action': label, 'count': state.count, 'current': state.current}


class OpenWebsiteTool(Tool):
    """One web address, opened plainly or in a new browser window placed on a display and zone."""
    name = 'openWebsite'
    description = ('Open a website or web page (YouTube, Gmail, a URL) in the browser, optionally in its own '
                   'window on a chosen display and zone such as the right side. NOT for files or apps.')
    timeout_sec = 12

    @property
    def inputSchema(self):
        return {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': (
                'The page to open: a full http(s) address or a site such as "youtube.com".')},
            'browser': {'type': 'string', 'enum': ['chrome', 'edge', 'brave', 'vivaldi'], 'description': (
                'Only when the user names a browser; otherwise the default browser is used.')},
            **PLACEMENT_PROPERTIES},
            'required': ['url'], 'additionalProperties': False}

    def run(self, args, context):
        from ....platform.windows.workspaces import BrowserWindowError
        try:
            if not context.cfg.windows_tools_enabled:
                raise ValueError('Windows control is disabled.')
            if not isinstance(args, dict) or set(args) - {'url', 'browser', *PLACEMENT_FIELDS}:
                raise ValueError('Invalid openWebsite arguments; give url and optionally browser, monitor, '
                                 'zone and state.')
            browser = args.get('browser') or None
            if browser is not None and not isinstance(browser, str):
                raise ValueError('The browser must be text.')
            placement = placement_request({key: args[key] for key in PLACEMENT_FIELDS
                                           if args.get(key) not in (None, '')})
            from ....platform.windows._bounded import run_bounded
            data = run_bounded(lambda: self._operate(args.get('url'), browser, placement, context.cfg),
                               self.timeout_sec)
            _remember_safely('', {**data, 'application': data['browser']})
            context.user_print('🌐 Website opened.')
            return ToolExecutionResult(success=True, reply_text=json.dumps(_redact_data(data), ensure_ascii=False))
        except BrowserWindowError as exc:
            debug_log(f'openWebsite launched but did not place its window ({exc.data["placement"]}).', 'windows')
            _remember_safely('', {**exc.data, 'application': exc.data.get('browser', '')})
            return ToolExecutionResult(success=False, reply_text=json.dumps(_redact_data(exc.data), ensure_ascii=False),
                                       error_message=redact(str(exc)))
        except (OSError, ValueError, TypeError) as exc:
            debug_log(f'openWebsite failed ({type(exc).__name__}).', 'windows')
            return ToolExecutionResult(success=False, reply_text=None, error_message=redact(str(exc)))

    @staticmethod
    def _operate(url, browser, placement, cfg):
        from ....platform.windows import websites
        from ....platform.windows.workspaces import BrowserWindowError
        if placement is None:
            return websites.open_website(url, browser)
        websites.normalise_url(url)  # an invalid address fails before the displays are read
        monitor, rectangle, label, state = _resolve_placement(cfg, placement)
        extra = {'monitor': monitor.device, **({'zone': label} if label else {})}
        try:
            result = websites.open_website(url, browser, (monitor, rectangle, state))
        except BrowserWindowError as exc:
            exc.data.update(extra)
            raise
        return {**result, **extra}


class OpenPathTool(WindowsTool):
    name = 'openPath'
    remembers_windows = True
    # No action argument: with monitor, zone or state the item's window is also placed.
    placement_actions = (None,)
    description = ('Open a folder or file in its default app, or find a file or folder by name ("my thesis") '
                   'and open it, optionally placed on a display and zone such as the left side. Opening '
                   'only: NOT for searching by type or date, listing, reading, moving, renaming or deleting '
                   'files (localFiles), websites (openWebsite) or installed apps and games (appControl).')
    target_description = ('Downloads, Documents, Desktop, Pictures, Music or Videos for common folders (never '
                          'guess their paths); a '
                          'full path; or just the name of a file or folder to look up, e.g. "thesis". Several '
                          'matches come back as candidates to choose from.')

    def _operate(self, action, target, cfg, placement=None):
        from ....platform.windows import files
        aliases = cfg.windows_path_aliases
        if not placement:
            return files.open_path(target, aliases=aliases)
        # Resolved once (a saved name or lookup included), before the displays are read.
        path, _kind, source = files.resolve_path(target, aliases=aliases)
        monitor, rectangle, label, state = _resolve_placement(cfg, placement)
        extra = {'zone': label} if label else {}
        try:
            result = files.open_path_placed(str(path), monitor, rectangle, state)
        except PartialPlacementError as exc:
            exc.data.update(monitor=monitor.device, **extra)
            raise
        return {**result, **extra, **({'source': source} if source != 'path' else {})}

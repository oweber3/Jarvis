"""The workspaceControl tool: open a named, configured desk set-up in one request.

A thin adapter over ``platform.windows.workspaces`` (see ``workspaces.spec.md``). Results carry
item labels and outcomes only, never the paths or URLs held in the user's configuration.
"""
from .desktop_control import WindowsTool, _fancyzone_sets, _REFERENT_STATES
from ....debug import debug_log


def _remember_items(data: dict) -> None:
    """Record each window the workspace opened or reused under its item label, so a follow-up
    such as "move the chat one to the second monitor" can name it. Never titles or paths."""
    from ....memory.desktop_referents import DesktopReferent, get_desktop_referents
    for item in data.get('items', []):
        if item.get('hwnd') is None:
            continue
        ok = item['outcome'] != 'failed'
        get_desktop_referents().record(DesktopReferent(
            application=item['label'], process=item.get('process') or '', hwnd=int(item['hwnd']),
            monitor=item.get('monitor') or '', zone=item.get('zone') or '',
            state=_REFERENT_STATES.get(item.get('state', ''), '') if ok else '',
            last_action='place' if item['outcome'] == 'placed_existing' else 'open'))


def _remember_safely(data: dict) -> None:
    try:
        _remember_items(data)
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f'workspace referents not recorded ({type(exc).__name__}).', 'windows')


class WorkspaceControlTool(WindowsTool):
    name = 'workspaceControl'
    description = ('Open one of the user\'s named workspaces: sets up a whole desk of windows (documents, sites, '
                   'apps) on chosen displays in one go. List the configured workspaces.')
    actions = ('open', 'list')
    untargeted_actions = ('list',)
    # The workspace deadline (45 s) sits just inside this outer limit.
    timeout_sec = 50
    action_description = 'open sets up the named workspace; list shows the configured workspaces.'
    target_description = 'The workspace name or alias, e.g. "research". Empty for list.'

    def fast_targets(self, cfg):
        """Offer each workspace's name and aliases to deterministic routing; no OS calls."""
        from ....fastpath.matcher import FastTarget
        return tuple(FastTarget((name, *definition['aliases']), name, '', name)
                     for name, definition in cfg.windows_workspaces.items())

    def _operate(self, action, target, cfg, process_only=False, placement=None):
        from ....platform.windows import displays, workspaces
        if action == 'list':
            return workspaces.list_workspaces(cfg.windows_workspaces)
        from ....platform.windows.apps import APP_INDEX
        name, definition = workspaces.resolve_workspace(target, cfg.windows_workspaces)
        monitors = displays.list_monitors()
        try:
            result = workspaces.open_workspace(
                name, definition, monitors=monitors, monitor_aliases=cfg.windows_monitor_aliases,
                zones=cfg.windows_window_zones, fancy=lambda: _fancyzone_sets(cfg, monitors),
                applications=APP_INDEX.applications, app_aliases=cfg.windows_app_aliases)
        except workspaces.WorkspaceError as exc:
            _remember_safely(exc.data)
            raise
        _remember_safely(result)
        return result

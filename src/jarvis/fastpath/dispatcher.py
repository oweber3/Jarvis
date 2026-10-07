"""Fast routing context and presentation; execution belongs to the registry."""
import json

from ..debug import debug_log
from ..utils.redact import redact
from .matcher import match, FastMatch

# The reply when a tool raises instead of returning a result.
UNEXPECTED_FAILURE = 'That did not work: the action failed with an unexpected error.'


def match_command(text, cfg, language=None, addressed=True):
    """Check availability and the central policy before offering a routine route. ``addressed`` is false for a
    voice request engaged only by the hot window, which never gets a route marked ``needs_wake_word``."""
    if not getattr(cfg, 'fast_commands_enabled', True):
        return None
    try:
        lang = (language or 'en').casefold().replace('_', '-').split('-')[0]
        if lang not in getattr(cfg, 'fast_commands_locales', ('en',)):
            return None
        from ..tools.registry import BUILTIN_TOOLS
        from ..tools.confirmation import evaluate_safety, SafetyTier
        available = set(BUILTIN_TOOLS)
        if not cfg.windows_tools_enabled:
            available -= {'appControl', 'windowControl', 'workspaceControl', 'systemVolume', 'mediaControl',
                          'systemInfo', 'openPath', 'systemSettings', 'inputControl', 'pdfNavigate'}
        tv_apps = BUILTIN_TOOLS['tvControl'].fast_targets(cfg) if 'tvControl' in available else ()
        routines = BUILTIN_TOOLS['routineControl'].fast_targets(cfg) if 'routineControl' in available else ()
        result = match(redact(text), lang, tv_apps=tv_apps, routines=routines, available_tools=available)
        # A routine may share its name with an application ("start chrome"): with every target offered, the
        # single-match rule makes such an utterance fall through to the model.
        if ((result is None or result.tool_name == 'routineControl') and cfg.windows_tools_enabled
                and available & {'appControl', 'workspaceControl'}):
            targets = BUILTIN_TOOLS['appControl'].fast_targets(cfg) if 'appControl' in available else ()
            workspaces = (BUILTIN_TOOLS['workspaceControl'].fast_targets(cfg)
                          if 'workspaceControl' in available else ())
            result = match(redact(text), lang, targets=targets, workspaces=workspaces, tv_apps=tv_apps,
                           routines=routines, available_tools=available)
        if result is not None and result.needs_wake_word and not addressed:
            debug_log(f'Fast route {result.command_id} needs the wake word; using LLM_ROUTE.', 'routing')
            return None
        # A tool can make its routes depend on live state (pdfNavigate: a PDF viewer is in front).
        ready = getattr(BUILTIN_TOOLS[result.tool_name], 'fast_available', None) if result else None
        if ready is not None and not ready(cfg):
            debug_log(f'Fast route {result.command_id} not available now; using LLM_ROUTE.', 'routing')
            return None
        if result is not None and evaluate_safety(
                result.tool_name, result.args, cfg, tool=BUILTIN_TOOLS[result.tool_name],
                language=lang).tier == SafetyTier.SAFE:
            return result
    except Exception as exc:
        debug_log(f'Fast matcher unavailable ({type(exc).__name__}); using LLM_ROUTE.', 'routing')
    return None


def dispatch(result: FastMatch, db, cfg, text, language=None, *, executor=None, quiet=False):
    """Execute through central validation/confirmation, including reclassification."""
    from ..tools.registry import BUILTIN_TOOLS
    if executor is None:
        from ..tools.registry import run_tool_with_retries
        executor = run_tool_with_retries
    # Targets can contain local identifiers. Log redacted normalised arguments, or only their names for a
    # tool whose arguments are the user's own names (routineControl).
    if getattr(BUILTIN_TOOLS.get(result.tool_name), 'log_arguments', True):
        args_log = redact(json.dumps(result.args, ensure_ascii=False, sort_keys=True))
    else:
        args_log = json.dumps(sorted(result.args))
    debug_log(f'FAST_ROUTE family={result.family} tool={result.tool_name} args={args_log}', 'routing')
    try:
        outcome = executor(db=db, cfg=cfg, tool_name=result.tool_name, tool_args=result.args,
                           system_prompt='', original_prompt=text, redacted_text=redact(text),
                           max_retries=1, language=language, quiet=quiet)
    except Exception as exc:  # noqa: BLE001 - a tool's unexpected error is a failed action, not a failed turn
        # The message can carry paths or device details, so only its type is logged and none of it is said.
        debug_log(f'FAST_ROUTE {result.command_id} raised {type(exc).__name__}; reporting failure.', 'routing')
        return UNEXPECTED_FAILURE
    if not outcome.success:
        return redact(outcome.reply_text or outcome.error_message or 'Action failed.')
    if result.reply_template:
        return redact(result.reply_template.format(**result.slots))
    return redact(outcome.reply_text or 'Action completed.')

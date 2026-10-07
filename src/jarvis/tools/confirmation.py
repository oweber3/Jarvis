"""Centralised confirmation and safety policy for tool actions.

This module provides one shared confirmation and safety mechanism for tool actions,
ensuring destructive operations are governed by a central safety tier policy
rather than ad hoc checks inside individual tools.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import enum
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from ..debug import debug_log


class SafetyTier(str, enum.Enum):
    """Safety classification tiers for tool actions."""
    SAFE = "SAFE"
    CONFIRM_VOICE = "CONFIRM_VOICE"
    CONFIRM_DIALOG = "CONFIRM_DIALOG"
    DENY = "DENY"


_TIER_RANK = {
    SafetyTier.SAFE: 0,
    SafetyTier.CONFIRM_VOICE: 1,
    SafetyTier.CONFIRM_DIALOG: 2,
    SafetyTier.DENY: 3,
}


@dataclass
class ConfirmationRequest:
    """Metadata describing a pending or evaluated action for safety governance."""
    tool_name: str
    tier: SafetyTier
    action: str
    target: str
    parameters: Dict[str, Any]
    consequence: Optional[str] = None
    reason: Optional[str] = None
    # True when the action creates, changes or removes its filesystem target.
    mutates: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


class VoiceResponseStatus(str, enum.Enum):
    """Outcome of evaluating user voice input against a pending confirmation."""
    AFFIRMATIVE = "AFFIRMATIVE"
    NEGATIVE = "NEGATIVE"
    UNRELATED = "UNRELATED"
    EXPIRED = "EXPIRED"


DIALOG_TIMEOUT_SEC = 60.0


@dataclass
class PendingConfirmation:
    """A registered confirmation awaiting the user's answer."""
    request: ConfirmationRequest
    expires_at: float
    authorised: bool = False
    consumed: bool = False
    # Set once the approval has been taken for execution (single use).
    claimed: bool = False
    authorised_at: float = 0.0
    execution_context: Dict[str, Any] = field(default_factory=dict)
    # Desktop dialog for CONFIRM_DIALOG requests; ``close()`` must not block.
    dialog_handle: Any = None
    expiry_timer: Any = None
    # An outcome has been reported to the store's observers (once per request).
    notified: bool = False

    @property
    def authorized(self) -> bool:
        return self.authorised

    @authorized.setter
    def authorized(self, val: bool) -> None:
        self.authorised = val


@dataclass(frozen=True)
class ApprovedAction:
    """An approval taken for execution: exactly one holder can run it."""
    request: ConfirmationRequest
    execution_context: Dict[str, Any]


# ---------------------------------------------------------------------------
# Locale phrase management for voice confirmation
# ---------------------------------------------------------------------------

_PHRASE_DIR = Path(__file__).resolve().parent / "phrases"
_PHRASE_CACHE: Dict[str, Dict[str, List[str]]] = {}
_PHRASE_LOCK = threading.Lock()


def get_locale_phrases(language: Optional[str] = "en") -> Optional[Dict[str, List[str]]]:
    """Load affirmative and negative confirmation phrases for a given language.

    Returns None if no phrase table exists for the language, triggering fallback.
    """
    lang = (language or "en").strip().lower()
    # Strip region suffix (e.g. en-US -> en)
    if "-" in lang:
        lang = lang.split("-")[0]
    if "_" in lang:
        lang = lang.split("_")[0]

    with _PHRASE_LOCK:
        if lang in _PHRASE_CACHE:
            return _PHRASE_CACHE[lang]

        phrase_file = _PHRASE_DIR / f"{lang}.json"
        if not phrase_file.exists():
            return None

        try:
            data = json.loads(phrase_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "affirmative" in data and "negative" in data:
                _PHRASE_CACHE[lang] = data
                return data
        except Exception as exc:
            debug_log(f"Failed to load phrase table for {lang}: {exc}", "safety")

        return None


# A short answer ("ok never mind", "yes please go ahead") is read phrase by phrase; anything longer
# is an answer only when it is exactly one phrase from the table.
SHORT_ANSWER_MAX_WORDS = 4

# Apostrophes join a word ("don't" -> "dont"); every other punctuation mark separates words.
_APOSTROPHES = re.compile(r"['’ʼ`´]")


def _phrase_words(text: str) -> tuple:
    """The words of ``text`` as the matcher compares them: lower case, apostrophes removed."""
    joined = _APOSTROPHES.sub("", (text or "").lower())
    return tuple(re.sub(r"[^\w\s]", " ", joined).split())


def _contains_phrase(words: tuple, phrase: tuple) -> bool:
    size = len(phrase)
    return size > 0 and any(words[i:i + size] == phrase for i in range(len(words) - size + 1))


def _is_short_approval(words: tuple, affirmatives: List[tuple], fillers: Set[str]) -> bool:
    """True when the words are affirmative phrases and politeness fillers only, with at least one
    affirmative phrase. Any other word makes the answer unclear, and an unclear answer is no approval."""
    longest_first = sorted(affirmatives, key=len, reverse=True)
    index, approved = 0, False
    while index < len(words):
        phrase = next((p for p in longest_first if p and words[index:index + len(p)] == p), None)
        if phrase is not None:
            approved = True
            index += len(phrase)
        elif words[index] in fillers:
            index += 1
        else:
            return False
    return approved


def match_voice_response(text: str, language: Optional[str] = "en") -> VoiceResponseStatus:
    """Classify an answer to a pending confirmation as affirmative, negative or unrelated.

    Uses the locale phrase table (``phrases/<language>.json``). A refusal anywhere in a short answer
    wins over any affirmative word beside it ("okay never mind" is a refusal). A short answer is
    approval only when every word belongs to an affirmative phrase or the table's politeness fillers;
    anything else is unrelated.
    """
    if not text or not text.strip():
        return VoiceResponseStatus.UNRELATED

    phrases = get_locale_phrases(language)
    if not phrases:
        # Without a phrase table, voice input cannot authorise the action
        return VoiceResponseStatus.UNRELATED

    words = _phrase_words(text)
    if not words:
        return VoiceResponseStatus.UNRELATED

    affirmatives = [w for w in (_phrase_words(p) for p in phrases.get("affirmative", [])) if w]
    negatives = [w for w in (_phrase_words(p) for p in phrases.get("negative", [])) if w]
    fillers = {word for p in phrases.get("filler", []) for word in _phrase_words(p)}

    if words in negatives:
        return VoiceResponseStatus.NEGATIVE
    if words in affirmatives:
        return VoiceResponseStatus.AFFIRMATIVE
    if len(words) > SHORT_ANSWER_MAX_WORDS:
        return VoiceResponseStatus.UNRELATED

    if any(_contains_phrase(words, phrase) for phrase in negatives):
        return VoiceResponseStatus.NEGATIVE
    if _is_short_approval(words, affirmatives, fillers):
        return VoiceResponseStatus.AFFIRMATIVE
    return VoiceResponseStatus.UNRELATED


# ---------------------------------------------------------------------------
# Centralised safety policy thresholds
# ---------------------------------------------------------------------------

CRITICAL_PROCESSES: Set[str] = {
    "system",
    "system idle process",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "winlogon.exe",
    "explorer.exe",
    "svchost.exe",
    "dwm.exe",
    "taskhostw.exe",
    "spoolsv.exe",
    "kernel",
    "init",
    "systemd",
    "launchd",
    "kthreadd",
    "sshd",
}


def is_critical_process(process_name: str) -> bool:
    """Check if a process is a critical system process."""
    if not process_name:
        return False
    name = Path(process_name).name.lower().strip()
    base_name = name.replace(".exe", "")
    return name in CRITICAL_PROCESSES or base_name in {p.replace(".exe", "") for p in CRITICAL_PROCESSES}


def _normalise_target(target: str) -> str:
    """Normalise target resource strings for binding comparisons."""
    if not target:
        return ""
    try:
        p = Path(os.path.expanduser(target)).resolve()
        normalised = str(p)
        if sys.platform == "win32":
            normalised = normalised.lower()
        return normalised
    except Exception:
        return target.strip().lower() if sys.platform == "win32" else target.strip()


def is_system_or_important_location(path_str: str) -> bool:
    """Check whether a path references a system directory or important configuration file."""
    if not path_str or not isinstance(path_str, str):
        return False
    try:
        p = Path(os.path.expanduser(path_str)).resolve()
    except Exception:
        return True

    # Root of drive or filesystem
    if p == p.parent:
        return True

    p_str = str(p).lower() if sys.platform == "win32" else str(p)

    # Windows system directories
    if sys.platform == "win32":
        win_dir = os.environ.get("SystemRoot", "C:\\Windows").lower()
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files").lower()
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)").lower()
        prog_data = os.environ.get("ProgramData", "C:\\ProgramData").lower()
        for sys_path in (win_dir, prog_files, prog_files_x86, prog_data):
            if p_str == sys_path or p_str.startswith(sys_path + os.sep):
                return True

    # Unix system directories
    unix_roots = ("/etc", "/bin", "/sbin", "/usr", "/var", "/boot", "/sys", "/proc", "/dev", "/lib", "/lib64")
    for r in unix_roots:
        if str(p) == r or str(p).startswith(r + "/"):
            return True

    # Startup folders run their contents at sign-in.
    for variable in ("APPDATA", "ProgramData"):
        root = os.environ.get(variable)
        if not root:
            continue
        try:
            startup = (Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup").resolve()
        except Exception:
            continue
        startup_str = str(startup).lower() if sys.platform == "win32" else str(startup)
        if p_str == startup_str or p_str.startswith(startup_str + os.sep):
            return True

    # Sensitive user directories
    try:
        user_home = Path(os.path.expanduser("~")).resolve()
        if p == user_home:
            return True
        sensitive_subdirs = (".ssh", ".gnupg", ".aws", ".azure", ".config")
        for sensitive in sensitive_subdirs:
            sens_path = user_home / sensitive
            if p == sens_path or str(p).startswith(str(sens_path) + os.sep):
                return True
    except Exception:
        pass

    return False


def is_bulk_operation(path_str: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """Check whether an operation targets multiple files or an entire directory."""
    if not path_str or not isinstance(path_str, str):
        return False
    if "*" in path_str or "?" in path_str:
        return True
    try:
        p = Path(os.path.expanduser(path_str)).resolve()
        if p.exists() and p.is_dir():
            return True
    except Exception:
        pass
    if args and isinstance(args, dict):
        if args.get("recursive") or args.get("bulk"):
            return True
    return False


def is_prohibited_action(action: str, target: str, args: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Check whether an action is unconditionally prohibited (DENY)."""
    action_lower = (action or "").lower()
    target_lower = (target or "").lower()

    if any(kw in action_lower for kw in ("format_drive", "format drive", "diskpart", "partition")):
        return "Formatting drives or disk partitioning is strictly prohibited."
    if "format" in action_lower and ("c:" in target_lower or "drive" in target_lower):
        return "Formatting drives is strictly prohibited."

    if target:
        try:
            p = Path(os.path.expanduser(target)).resolve()
            if p == p.parent and any(kw in action_lower for kw in ("delete", "remove", "erase", "unlink")):
                return "Deleting the drive or filesystem root is strictly prohibited."
        except Exception:
            pass

    return None


def evaluate_safety(
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    cfg: Any,
    tool: Optional[Any] = None,
    language: Optional[str] = None,
) -> ConfirmationRequest:
    """Centrally classify a tool call into SAFE, CONFIRM_VOICE, CONFIRM_DIALOG, or DENY."""
    if tool is None:
        try:
            from .registry import BUILTIN_TOOLS
            tool = BUILTIN_TOOLS.get(tool_name)
        except Exception:
            pass

    action_arg = ""
    target_arg = ""
    if isinstance(tool_args, dict):
        action_arg = str(tool_args.get("action") or tool_args.get("operation") or "").strip()
        target_arg = str(tool_args.get("target") or tool_args.get("path") or "").strip()

    # Obtain initial classification from tool metadata if implemented
    if tool is not None and hasattr(tool, "classify_safety"):
        req = tool.classify_safety(tool_args, cfg)
    else:
        req = ConfirmationRequest(
            tool_name=tool_name,
            tier=SafetyTier.SAFE,
            action=action_arg or tool_name,
            target=target_arg,
            parameters=dict(tool_args or {}),
        )

    if not req.target and target_arg:
        req.target = target_arg
    if (req.action == tool_name) and action_arg:
        req.action = action_arg

    # 1. Prohibited check (DENY)
    deny_reason = is_prohibited_action(req.action, req.target, req.parameters)
    if deny_reason:
        return ConfirmationRequest(
            tool_name=tool_name,
            tier=SafetyTier.DENY,
            action=req.action,
            target=req.target,
            parameters=req.parameters,
            reason=deny_reason,
        )

    def _at_least(tier: SafetyTier, consequence: str) -> ConfirmationRequest:
        """Heuristics only raise a tool's declared tier, never lower it."""
        if _TIER_RANK[tier] <= _TIER_RANK[req.tier]:
            return req
        return ConfirmationRequest(
            tool_name=tool_name,
            tier=tier,
            action=req.action,
            target=req.target or ("local system" if shutting_down else ""),
            parameters=req.parameters,
            consequence=consequence,
            mutates=req.mutates,
        )

    action_lower = req.action.lower()
    shutting_down = any(kw in action_lower for kw in ("shutdown", "shut down", "restart", "reboot"))

    # 2. High-risk system actions (CONFIRM_DIALOG)
    if shutting_down:
        return _at_least(
            SafetyTier.CONFIRM_DIALOG,
            "The computer will shut down or restart, closing all running programs.",
        )

    if "uninstall" in action_lower:
        return _at_least(
            SafetyTier.CONFIRM_DIALOG,
            f"Software '{req.target}' will be uninstalled from your computer.",
        )

    # 3. Process termination
    if any(kw in action_lower for kw in ("terminate", "kill", "force_close", "process_kill")):
        if is_critical_process(req.target):
            return _at_least(
                SafetyTier.CONFIRM_DIALOG,
                f"Terminating critical system process '{req.target}' may cause system instability or immediate crash.",
            )
        return _at_least(SafetyTier.CONFIRM_VOICE, f"Process '{req.target}' will be terminated.")

    # 4. File operations: any mutation, and any voice-confirmed action, is
    # checked against important locations.
    if req.mutates or req.tier == SafetyTier.CONFIRM_VOICE:
        if req.target and is_system_or_important_location(req.target):
            return _at_least(
                SafetyTier.CONFIRM_DIALOG,
                "Modifying or deleting files in an important system or user directory may damage your system.",
            )
    if req.tier == SafetyTier.CONFIRM_VOICE:
        if req.target and is_bulk_operation(req.target, req.parameters):
            return _at_least(
                SafetyTier.CONFIRM_DIALOG,
                f"Broad or bulk deletion on '{req.target}' affects multiple items and cannot be easily undone.",
            )
        # Unsupported language fallback per spec
        if language and get_locale_phrases(language) is None:
            return _at_least(
                SafetyTier.CONFIRM_DIALOG,
                req.consequence or "Voice confirmation is unavailable for this language; desktop confirmation required.",
            )

    return req


# ---------------------------------------------------------------------------
# Run grants: the confirmed steps of an approved routine
# ---------------------------------------------------------------------------

_run_grants: ContextVar[Optional[List[ConfirmationRequest]]] = ContextVar("jarvis_run_grants", default=None)


@contextmanager
def run_grants(requests):
    """Authorise each of ``requests`` once, for exactly its tool, action, target and parameters, while the
    block runs on this thread. An approved routine grants the steps its question named; nothing else is
    authorised and nothing outlives the block."""
    token = _run_grants.set(list(requests))
    try:
        yield
    finally:
        _run_grants.reset(token)


def consume_run_grant(
    tool_name: str,
    action: str,
    target: str,
    parameters: Dict[str, Any],
    required_tier: SafetyTier = SafetyTier.CONFIRM_VOICE,
) -> bool:
    """Use up the grant for exactly this action at ``required_tier`` or stronger, if the run holds one."""
    grants = _run_grants.get()
    for index, grant in enumerate(grants or ()):
        if (grant.tool_name == tool_name and grant.action == action
                and _normalise_target(grant.target) == _normalise_target(target)
                and grant.parameters == parameters and _TIER_RANK[grant.tier] >= _TIER_RANK[required_tier]):
            del grants[index]
            debug_log(f"Run grant consumed for {tool_name}", "safety")
            return True
    return False


# ---------------------------------------------------------------------------
# ConfirmationStore: the single pending confirmation
# ---------------------------------------------------------------------------

class ConfirmationStore:
    """Thread-safe store managing a single pending confirmation.

    A voice request waits for a spoken answer; a dialog request waits for the
    desktop layer. Neither blocks any caller. Approval is single use: it is
    claimed atomically and the claimed action runs on a worker thread.
    """

    def __init__(self, default_timeout_sec: float = 30.0):
        self._default_timeout_sec = default_timeout_sec
        self._pending: Optional[PendingConfirmation] = None
        self._lock = threading.RLock()
        self._observers: List[Callable[[str, str], None]] = []

    # -- observers ---------------------------------------------------------

    def add_observer(self, observer: Callable[[str, str], None]) -> None:
        """Report each request's outcome as ``observer(request_id, outcome)``.

        Outcomes: ``approved`` (claimed for execution), ``denied``, ``expired``,
        ``replaced`` and ``cancelled``. Each request reports exactly once. Observers
        run on the thread that caused the outcome while the store lock is held, so
        they must return quickly and never call back into the store.
        """
        with self._lock:
            if observer not in self._observers:
                self._observers.append(observer)

    def remove_observer(self, observer: Callable[[str, str], None]) -> None:
        with self._lock:
            if observer in self._observers:
                self._observers.remove(observer)

    def _notify_locked(self, pending: PendingConfirmation, outcome: str) -> None:
        if pending.notified:
            return
        pending.notified = True
        for observer in list(self._observers):
            try:
                observer(pending.request.id, outcome)
            except Exception as exc:
                debug_log(f"Confirmation observer failed: {exc}", "safety")

    def pending_for_ref(self, request_ref: str) -> Optional[PendingConfirmation]:
        """The active pending confirmation raised for ``request_ref``, if any."""
        pending = self.get_pending()
        if pending is not None and pending.execution_context.get("request_ref") == request_ref:
            return pending
        return None

    # -- lifecycle ---------------------------------------------------------

    def _drop_locked(self, reason: str = "cancelled") -> None:
        """Remove the pending request, stop its timer and close its dialog."""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        self._notify_locked(pending, reason)
        if pending.expiry_timer is not None:
            pending.expiry_timer.cancel()
        handle, pending.dialog_handle = pending.dialog_handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception as exc:
                debug_log(f"Closing confirmation dialog failed: {exc}", "safety")

    def _expired_locked(self, pending: PendingConfirmation) -> bool:
        """Expiry is judged when the user acted, so scheduling delay never voids an approval."""
        moment = pending.authorised_at if pending.authorised else time.time()
        return moment > pending.expires_at

    def set_pending(
        self,
        request: ConfirmationRequest,
        execution_context: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[float] = None,
    ) -> PendingConfirmation:
        """Register a pending confirmation, replacing any previous request."""
        with self._lock:
            self._drop_locked("replaced")
            timeout = timeout_sec if timeout_sec is not None else self._default_timeout_sec
            pending = PendingConfirmation(
                request=request,
                expires_at=time.time() + timeout,
                execution_context=execution_context or {},
            )
            if request.tier == SafetyTier.CONFIRM_DIALOG:
                timer = threading.Timer(timeout, self._expire, args=(request.id,))
                timer.daemon = True
                pending.expiry_timer = timer
                timer.start()
            self._pending = pending
            # Tool and tier only: the action and target can name a control, a file or a window.
            debug_log(
                f"Pending confirmation set: {request.tool_name} "
                f"(tier={request.tier.value}, timeout={timeout}s)",
                "safety",
            )
            return pending

    def _expire(self, request_id: str) -> None:
        with self._lock:
            pending = self._pending
            if pending is None or pending.request.id != request_id or pending.claimed:
                return
            debug_log(f"Pending confirmation expired ({pending.request.tool_name})", "safety")
            self._drop_locked("expired")

    def get_pending(self) -> Optional[PendingConfirmation]:
        """Active, unclaimed pending confirmation, expiring old ones."""
        with self._lock:
            pending = self._pending
            if pending is None or pending.claimed:
                return None
            if pending.consumed:
                self._drop_locked("approved")
                return None
            if time.time() > pending.expires_at and not pending.authorised:
                debug_log(f"Pending confirmation expired ({pending.request.tool_name})", "safety")
                self._drop_locked("expired")
                return None
            return pending

    def clear_pending(self, reason: str = "cancelled") -> None:
        """Clear active pending confirmation."""
        with self._lock:
            if self._pending:
                debug_log(f"Pending confirmation cleared ({self._pending.request.tool_name})", "safety")
            self._drop_locked(reason)

    def discard_pending(self, request_id: str, reason: str = "cancelled") -> None:
        """Clear the pending confirmation only if it is still request ``request_id``."""
        with self._lock:
            if self._pending is not None and self._pending.request.id == request_id:
                self._drop_locked(reason)

    def cancel_pending(self) -> None:
        self.clear_pending()

    def cancel_dialog_pending(self) -> None:
        """Drop an unclaimed dialog request (the desktop layer is going away)."""
        with self._lock:
            pending = self._pending
            if pending is not None and not pending.claimed and pending.request.tier == SafetyTier.CONFIRM_DIALOG:
                debug_log("Dialog confirmation cancelled: desktop layer unavailable", "safety")
                self._drop_locked()

    def has_pending(self) -> bool:
        return self.get_pending() is not None

    def has_pending_voice(self) -> bool:
        """True only for requests that speech or typed text can answer."""
        pending = self.get_pending()
        return pending is not None and pending.request.tier == SafetyTier.CONFIRM_VOICE

    # -- voice -------------------------------------------------------------

    def handle_voice_response(self, transcript: str, language: Optional[str] = "en") -> VoiceResponseStatus:
        """Process spoken transcript against the active voice confirmation."""
        with self._lock:
            pending = self._pending
            if pending is None or pending.claimed or pending.request.tier != SafetyTier.CONFIRM_VOICE:
                return VoiceResponseStatus.UNRELATED
            if time.time() > pending.expires_at:
                self._drop_locked("expired")
                return VoiceResponseStatus.EXPIRED

            status = match_voice_response(transcript, language)
            if status == VoiceResponseStatus.AFFIRMATIVE:
                pending.authorised = True
                pending.authorised_at = time.time()
                debug_log(f"Pending confirmation authorised via voice: {pending.request.tool_name}", "safety")
                return VoiceResponseStatus.AFFIRMATIVE
            elif status == VoiceResponseStatus.NEGATIVE:
                self._drop_locked("denied")
                debug_log(f"Pending confirmation rejected via voice: {pending.request.tool_name}", "safety")
                return VoiceResponseStatus.NEGATIVE
            else:
                self._drop_locked()
                debug_log(
                    f"Pending confirmation abandoned due to unrelated input: '{transcript}'",
                    "safety",
                )
                return VoiceResponseStatus.UNRELATED

    # -- dialog ------------------------------------------------------------

    def begin_dialog(
        self,
        request: ConfirmationRequest,
        execution_context: Dict[str, Any],
        present: Callable[[ConfirmationRequest, Callable[[bool], Any]], Any],
        timeout_sec: Optional[float] = None,
    ) -> bool:
        """Register a dialog request and ask the desktop layer to show it.

        Returns immediately. ``present`` must not block; it returns a handle
        whose ``close()`` is safe from any thread, or ``None`` if it cannot
        show the dialog (the request is then dropped).
        """
        pending = self.set_pending(
            request, execution_context,
            timeout_sec if timeout_sec is not None else DIALOG_TIMEOUT_SEC,
        )
        request_id = request.id
        try:
            handle = present(request, lambda approved: self.resolve_dialog(request_id, bool(approved)))
        except Exception as exc:
            debug_log(f"Presenting confirmation dialog failed: {exc}", "safety")
            handle = None
        if handle is None:
            with self._lock:
                if self._pending is pending:
                    self._drop_locked()
            return False
        with self._lock:
            if self._pending is pending and not pending.claimed:
                pending.dialog_handle = handle
                return True
        # Answered, expired or replaced while the dialog was being opened.
        try:
            handle.close()
        except Exception:
            pass
        return True

    def resolve_dialog(self, request_id: str, approved: bool) -> bool:
        """Apply the user's dialog answer; schedule the action when approved.

        Stale answers (expired, replaced, already answered) change nothing.
        """
        with self._lock:
            pending = self._pending
            if (pending is None or pending.request.id != request_id or pending.claimed
                    or pending.request.tier != SafetyTier.CONFIRM_DIALOG):
                debug_log("Ignored stale dialog confirmation answer", "safety")
                return False
            if time.time() > pending.expires_at or not approved:
                debug_log(f"Dialog confirmation {'expired' if approved else 'cancelled'}", "safety")
                self._drop_locked("denied" if not approved else "expired")
                return False
            pending.authorised = True
            pending.authorised_at = time.time()
            action = self._claim_locked()
        if action is None:
            return False
        self.schedule(action)
        return True

    # -- authorisation -----------------------------------------------------

    def is_authorised(
        self,
        tool_name: str,
        action: str,
        target: str,
        parameters: Dict[str, Any],
        required_tier: SafetyTier = SafetyTier.CONFIRM_VOICE,
    ) -> bool:
        """Check if exact action, target and parameters are authorised at ``required_tier`` or stronger."""
        with self._lock:
            pending = self._pending
            if pending is None or pending.consumed or not pending.authorised:
                return False
            if self._expired_locked(pending):
                self._drop_locked()
                return False
            req = pending.request
            if _TIER_RANK[req.tier] < _TIER_RANK[required_tier]:
                return False
            if req.tool_name != tool_name:
                return False
            if req.action != action:
                return False
            if _normalise_target(req.target) != _normalise_target(target):
                return False
            if req.parameters != parameters:
                return False

            return True

    is_authorized = is_authorised

    def consume_authorisation(
        self,
        tool_name: str,
        action: str,
        target: str,
        parameters: Dict[str, Any],
        required_tier: SafetyTier = SafetyTier.CONFIRM_VOICE,
    ) -> bool:
        """Consume single-use authorisation token."""
        with self._lock:
            if self.is_authorised(tool_name, action, target, parameters, required_tier):
                self._pending.consumed = True
                self._drop_locked("approved")
                debug_log(f"Authorisation consumed for {tool_name}", "safety")
                return True
            return False

    consume_authorization = consume_authorisation

    # -- execution ---------------------------------------------------------

    def _claim_locked(self) -> Optional[ApprovedAction]:
        pending = self._pending
        if pending is None or pending.claimed or pending.consumed or not pending.authorised:
            return None
        if self._expired_locked(pending):
            self._drop_locked("expired")
            return None
        pending.claimed = True
        self._notify_locked(pending, "approved")
        if pending.expiry_timer is not None:
            pending.expiry_timer.cancel()
        # The dialog has done its job; it must not outlive the decision.
        handle, pending.dialog_handle = pending.dialog_handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        return ApprovedAction(pending.request, dict(pending.execution_context))

    def claim_approved(self) -> Optional[ApprovedAction]:
        """Atomically take the approved pending action; ``None`` if there is none."""
        with self._lock:
            return self._claim_locked()

    def run_approved(self, action: ApprovedAction, db=None, cfg=None) -> Any:
        """Execute an approved action in the calling thread (never the listener's)."""
        from .registry import run_tool_with_retries
        ctx = action.execution_context
        return run_tool_with_retries(
            db=db or ctx.get("db"),
            cfg=cfg or ctx.get("cfg"),
            tool_name=ctx.get("tool_name", action.request.tool_name),
            tool_args=ctx.get("tool_args", action.request.parameters),
            system_prompt=ctx.get("system_prompt", ""),
            original_prompt=ctx.get("original_prompt", ""),
            redacted_text=ctx.get("redacted_text", ""),
            max_retries=ctx.get("max_retries", 1),
            language=ctx.get("language"),
            quiet=ctx.get("quiet", False),
            allowed_tools=ctx.get("allowed_tools"),
        )

    def schedule(self, action: ApprovedAction, db=None, cfg=None) -> None:
        """Run an approved action on its own worker thread and deliver the result."""

        def _worker() -> None:
            try:
                result = self.run_approved(action, db=db, cfg=cfg)
                reply = result.reply_text or (
                    "Action completed." if result.success else (result.error_message or "Action failed.")
                )
                success = bool(result.success)
            except Exception as exc:
                debug_log(f"Confirmed action failed: {exc}", "safety")
                reply, success = "The confirmed action failed.", False
            deliver_result(reply, success, origin=action.execution_context.get("origin", ORIGIN_VOICE))

        threading.Thread(target=_worker, name="jarvis-confirmed-action", daemon=True).start()

    def execute_pending_async(self, db=None, cfg=None) -> bool:
        """Claim the approved voice action and run it on a worker thread.

        Returns immediately. ``False`` means nothing could be claimed (already
        taken, expired before approval or cancelled), so nothing will run.
        """
        action = self.claim_approved()
        if action is None:
            return False
        self.schedule(action, db=db, cfg=cfg)
        return True


# Global singleton instance
_GLOBAL_CONFIRMATION_STORE = ConfirmationStore()


def get_confirmation_store() -> ConfirmationStore:
    return _GLOBAL_CONFIRMATION_STORE


# ---------------------------------------------------------------------------
# Desktop dialog and result delivery registration
# ---------------------------------------------------------------------------

DialogPresenter = Callable[[ConfirmationRequest, Callable[[bool], Any]], Any]

_dialog_callback: Optional[DialogPresenter] = None
_dialog_callback_lock = threading.Lock()


def set_dialog_callback(callback: Optional[DialogPresenter]) -> None:
    """Register the desktop presenter ``callback(request, resolve) -> handle``.

    The presenter shows the dialog without blocking, calls ``resolve(True)`` or
    ``resolve(False)`` once the user answers, and returns a handle whose
    ``close()`` dismisses the dialog from any thread. Unregistering drops any
    dialog request still waiting.
    """
    global _dialog_callback
    with _dialog_callback_lock:
        _dialog_callback = callback
    if callback is None:
        _GLOBAL_CONFIRMATION_STORE.cancel_dialog_pending()


def get_dialog_callback() -> Optional[DialogPresenter]:
    """Get desktop confirmation dialog presenter."""
    with _dialog_callback_lock:
        return _dialog_callback


# Where a confirmed action was requested: spoken to the assistant, or typed in
# the text chat (which runs the reply engine quietly).
ORIGIN_VOICE = "voice"
ORIGIN_CHAT = "chat"

_result_handlers: Dict[str, Callable[[str, bool], None]] = {}
_result_handler_lock = threading.Lock()


def set_result_handler(handler: Optional[Callable[[str, bool], None]], origin: str = ORIGIN_VOICE) -> None:
    """Register where the outcome of a confirmed action from ``origin`` is reported.

    ``handler(reply, success)`` runs on the worker thread that executed the
    action, so it must hand off to any UI thread itself. ``None`` unregisters.
    """
    with _result_handler_lock:
        if handler is None:
            _result_handlers.pop(origin, None)
        else:
            _result_handlers[origin] = handler


def deliver_result(reply: str, success: bool, origin: str = ORIGIN_VOICE) -> None:
    """Report a confirmed action's outcome to its originating interface.

    Cancelled and expired requests never reach here. A chat origin without a
    registered handler is only logged: text never prints to the daemon console.
    """
    with _result_handler_lock:
        handler = _result_handlers.get(origin)
    if handler is None:
        debug_log(f"Confirmed action finished (no {origin} handler): success={success}", "safety")
        if origin == ORIGIN_VOICE:
            print(f"  {'✅' if success else '❌'} {reply}", flush=True)
        return
    try:
        handler(reply, success)
    except Exception as exc:
        debug_log(f"Delivering confirmed action result failed: {exc}", "safety")

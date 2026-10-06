"""Short-lived record of what Jarvis itself recently acted on, and its presentation to models.

Windows Jarvis opened, focused, placed or changed, the device it controlled, the media session and
the clipboard's content type, next to the window the user is looking at (read per request by the
caller and passed in). A follow-up that does not name its target ("close this", "pause it") is
resolved by the model from this record, never by parsing language. Entries come from tool outcomes
on every route and live in memory only. See ``desktop_referents.spec.md``.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..debug import debug_log
from ..utils.redact import redact

# Returns facts about the launched window ({"hwnd", "process", "monitor"}) or None when unknown.
Resolver = Callable[[], Optional[Dict[str, Any]]]
# Returns the clipboard's content type once it has changed after a hotkey, or None while it has not.
ClipboardCheck = Callable[[], Optional[str]]

_TEXT_CHARS = 120
_STATE_WORDS = {"normal": "normal", "maximised": "maximised", "minimised": "minimised",
                "closing": "closing (close requested)"}


@dataclass(frozen=True)
class DesktopReferent:
    application: str = ""
    process: str = ""
    hwnd: Optional[int] = None
    monitor: str = ""
    zone: str = ""
    state: str = ""
    last_action: str = ""
    age_sec: float = 0.0


@dataclass(frozen=True)
class OtherReferent:
    """A device, the media session or the clipboard. Names of applications and devices, never content."""
    kind: str  # device | media | clipboard
    tool: str
    device: str = ""
    application: str = ""
    content_type: str = ""
    status: str = ""
    last_action: str = ""
    age_sec: float = 0.0


@dataclass(frozen=True)
class ForegroundWindow:
    """The window the user is looking at when a request arrives. Read per request, never stored."""
    application: str = ""
    process: str = ""
    hwnd: int = 0
    monitor: str = ""
    state: str = ""


@dataclass
class _Entry:
    referent: DesktopReferent
    at: float
    resolve: Optional[Resolver] = None


@dataclass
class _Other:
    referent: OtherReferent
    at: float


def _other_key(referent: OtherReferent) -> str:
    return f"device:{referent.device}" if referent.kind == "device" else referent.kind


def _same_application(a: DesktopReferent, b: DesktopReferent) -> bool:
    if a.process and b.process:
        return a.process.casefold() == b.process.casefold()
    return bool(a.application) and a.application.casefold() == b.application.casefold()


def _merge(new: DesktopReferent, old: DesktopReferent) -> DesktopReferent:
    """Keep what an earlier outcome knew about the same window when the new one does not say.

    The earlier name is kept for the same process (a launch names the application; a later action
    may only name its process), so the user's own word for the window survives."""
    zone = "" if new.state == "maximised" else (new.zone or old.zone)
    same_process = not new.process or not old.process or new.process.casefold() == old.process.casefold()
    return dataclasses.replace(
        new,
        application=(old.application if same_process else "") or new.application,
        process=new.process or old.process,
        monitor=new.monitor or old.monitor,
        zone=zone,
        state=new.state or old.state,
    )


class DesktopReferents:
    """Thread-safe, bounded, newest-first store of desktop referents."""

    MAX_ENTRIES = 5

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_entries: int = MAX_ENTRIES):
        self._clock = clock
        self._max = max_entries
        self._entries: List[_Entry] = []
        self._others: List[_Other] = []
        # A hotkey's clipboard check and when it ran; it replaces the clipboard entry only once it resolves.
        self._clipboard_check: Optional[tuple] = None
        self._lock = threading.Lock()

    def record(self, referent: DesktopReferent, resolve: Optional[Resolver] = None) -> None:
        """Remember an action's outcome as the newest referent."""
        with self._lock:
            merged = referent
            if referent.hwnd is not None:
                earlier = next((e.referent for e in self._entries if e.referent.hwnd == referent.hwnd), None)
                if earlier is None:
                    # The window may be the one a pending launch of the same application produced.
                    earlier = next((e.referent for e in self._entries
                                    if e.referent.hwnd is None and _same_application(e.referent, referent)), None)
                if earlier is not None:
                    merged = _merge(referent, earlier)
            # The window's own earlier entry is replaced, and so is a pending launch of the same
            # application: the launch has either produced this window or been launched again.
            kept = [e for e in self._entries
                    if not (merged.hwnd is not None and e.referent.hwnd == merged.hwnd)
                    and not (e.referent.hwnd is None and _same_application(e.referent, merged))]
            self._entries = [_Entry(merged, self._clock(), resolve if merged.hwnd is None else None)]
            self._entries.extend(kept[: self._max - 1])
        debug_log(f"desktop referent recorded ({referent.last_action or 'action'}, "
                  f"{'window' if referent.hwnd is not None else 'pending launch'})", "windows")

    def recent(self, max_age_sec: float) -> List[DesktopReferent]:
        """Live referents, newest first, with pending launches resolved where possible."""
        now = self._clock()
        with self._lock:
            self._entries = [e for e in self._entries if now - e.at <= max_age_sec]
            pending = [e for e in self._entries if e.referent.hwnd is None and e.resolve is not None]
        for entry in pending:
            self._resolve(entry)
        with self._lock:
            return [dataclasses.replace(e.referent, age_sec=max(0.0, now - e.at)) for e in self._entries]

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._others = []
            self._clipboard_check = None

    # -- devices, media, clipboard ------------------------------------------------------------

    def _record_other(self, referent: OtherReferent, at: Optional[float] = None) -> None:
        key = _other_key(referent)
        with self._lock:
            kept = [o for o in self._others if _other_key(o.referent) != key]
            self._others = [_Other(referent, self._clock() if at is None else at)] + kept
        debug_log(f"desktop referent recorded ({referent.kind})", "windows")

    def record_device(self, device: str, tool: str, last_action: str) -> None:
        """A device a tool acted on (``tv``, or the name an extension gives its device); one entry per device."""
        self._record_other(OtherReferent(kind="device", tool=tool, device=device, last_action=last_action))

    def record_media(self, application: str, status: str) -> None:
        """The media session ``mediaControl`` acted on or read: its application, never the track."""
        self._record_other(OtherReferent(kind="media", tool="mediaControl", application=application,
                                         status=status))

    def record_clipboard(self, content_type: str) -> None:
        """What kind of thing the clipboard holds after ``inputControl`` used it, never its data."""
        with self._lock:
            self._clipboard_check = None
        self._record_other(OtherReferent(kind="clipboard", tool="inputControl", content_type=content_type))

    def record_clipboard_change(self, check: ClipboardCheck) -> None:
        """A hotkey that may change the clipboard: shown only once ``check`` reports a content type."""
        with self._lock:
            self._clipboard_check = (check, self._clock())

    def others(self, max_age_sec: float) -> List[OtherReferent]:
        """Live device, media and clipboard referents, newest first."""
        now = self._clock()
        with self._lock:
            pending = self._clipboard_check
            if pending is not None and now - pending[1] > max_age_sec:
                self._clipboard_check = pending = None
        if pending is not None:
            self._check_clipboard(pending)
        with self._lock:
            self._others = [o for o in self._others if now - o.at <= max_age_sec]
            ordered = sorted(self._others, key=lambda o: o.at, reverse=True)
            return [dataclasses.replace(o.referent, age_sec=max(0.0, now - o.at)) for o in ordered]

    def _check_clipboard(self, pending: tuple) -> None:
        check, at = pending
        try:
            content_type = check()
        except Exception as exc:  # noqa: BLE001 - an unreadable clipboard is simply not presented
            debug_log(f"clipboard check failed ({type(exc).__name__})", "windows")
            return
        if not content_type:
            return
        with self._lock:
            if self._clipboard_check is not pending:
                return
            self._clipboard_check = None
        self._record_other(OtherReferent(kind="clipboard", tool="inputControl", content_type=str(content_type)),
                           at=at)

    def _resolve(self, entry: _Entry) -> None:
        try:
            facts = entry.resolve() if entry.resolve else None
        except Exception as exc:  # noqa: BLE001 - an unresolved launch stays pending
            debug_log(f"pending launch not resolved ({type(exc).__name__})", "windows")
            return
        if not facts or facts.get("hwnd") is None:
            return
        with self._lock:
            if entry not in self._entries:
                return
            entry.referent = dataclasses.replace(
                entry.referent, hwnd=int(facts["hwnd"]),
                process=str(facts.get("process") or entry.referent.process),
                monitor=str(facts.get("monitor") or entry.referent.monitor))
            entry.resolve = None
        debug_log("pending launch resolved to its window", "windows")


_store = DesktopReferents()


def get_desktop_referents() -> DesktopReferents:
    """The process-wide store shared by voice, text chat and background Codex."""
    return _store


def _max_age(cfg: Any) -> float:
    return float(getattr(cfg, "dialogue_memory_timeout", 300) or 300)


def live_referents(cfg: Any) -> List[DesktopReferent]:
    """Window referents young enough to belong to the current conversation (the dialogue memory window)."""
    try:
        return _store.recent(max_age_sec=_max_age(cfg))
    except Exception as exc:  # noqa: BLE001 - context is optional, never fatal to a reply
        debug_log(f"desktop referents unavailable ({type(exc).__name__})", "windows")
        return []


def live_others(cfg: Any) -> List[OtherReferent]:
    """Device, media and clipboard referents of the current conversation."""
    try:
        return _store.others(max_age_sec=_max_age(cfg))
    except Exception as exc:  # noqa: BLE001 - context is optional, never fatal to a reply
        debug_log(f"other referents unavailable ({type(exc).__name__})", "windows")
        return []


def carryover_tools(referents: Sequence[DesktopReferent], others: Sequence[OtherReferent]) -> List[str]:
    """Tools a follow-up about a live referent needs, whatever the router picked."""
    tools: List[str] = ["windowControl", "appControl"] if referents else []
    for other in others:
        if other.tool and other.tool not in tools:
            tools.append(other.tool)
    return tools


def _text(value: str) -> str:
    return redact(str(value or ""))[:_TEXT_CHARS]


_DEVICE_NAMES = {"tv": "TV"}
_CONTENT_WORDS = {"text": "text", "image": "an image", "files": "files",
                  "other": "something other than text, an image or files", "empty": "nothing"}

_GUIDANCE = (
    "A request about a window that names none means the window the user is looking at when it points at what "
    "the user is looking at, and the window Jarvis just acted on when it continues about that window; when "
    "they are the same window, it is that one. Act on it by passing its target exactly as shown, never an "
    "application name, which can match several windows: appControl closes or focuses that one window, "
    "windowControl minimises, maximises, restores or places it. A request that names no device or "
    "player most likely continues with the most recent entry it fits; with nothing recent that fits, it is "
    "about this computer. Say an action is done only after its tool result confirms it.")


def _age(seconds: float) -> str:
    return f"{int(round(seconds))} s ago"


def _window_line(index: int, ref: DesktopReferent) -> str:
    name = _text(ref.application) or _text(ref.process) or "Window"
    if ref.hwnd is None:
        facts = ["launched, window not found yet"]
    else:
        facts = [f'target "{int(ref.hwnd)}" (its window handle)']
    if ref.process:
        facts.append(f"process {_text(ref.process)}")
    if ref.monitor:
        facts.append(f"last seen on display {_text(ref.monitor)}")
    if ref.zone:
        facts.append(f"zone {_text(ref.zone)}")
    if ref.state:
        facts.append(_STATE_WORDS.get(ref.state, _text(ref.state)))
    facts.append(_age(ref.age_sec))
    return f"{index}. {name}: " + ", ".join(facts)


def _other_line(other: OtherReferent) -> str:
    if other.kind == "device":
        name = _DEVICE_NAMES.get(other.device, _text(other.device))
        facts = [f"last action {_text(other.last_action)}"] if other.last_action else []
    elif other.kind == "media":
        name = f"media player {_text(other.application) or 'unknown'}"
        facts = [_text(other.status)] if other.status else []
    else:
        name = "clipboard"
        facts = [f"holds {_CONTENT_WORDS.get(other.content_type, _text(other.content_type))}"]
    facts += [f"controlled with {_text(other.tool)}", _age(other.age_sec)]
    return f"- {name}: " + ", ".join(facts)


def format_for_model(referents: Sequence[DesktopReferent], others: Sequence[OtherReferent] = (),
                     foreground: Optional[ForegroundWindow] = None) -> str:
    """Compact reference block for the local model; empty when there is nothing to say."""
    parts = []
    if foreground is not None:
        facts = [f'target "{int(foreground.hwnd)}" (its window handle)']
        if foreground.process:
            facts.append(f"process {_text(foreground.process)}")
        if foreground.monitor:
            facts.append(f"on display {_text(foreground.monitor)}")
        if foreground.state:
            facts.append(_STATE_WORDS.get(foreground.state, _text(foreground.state)))
        same = next((n for n, ref in enumerate(referents, 1) if ref.hwnd == foreground.hwnd), None)
        if same is not None:
            facts.append(f"the same window as recent window {same}")
        name = _text(foreground.application) or _text(foreground.process) or "Window"
        parts.append(f"Window the user is looking at: {name}: " + ", ".join(facts))
    if referents:
        parts.append("Recent windows that Jarvis opened, focused or placed for the user, newest first:\n"
                     + "\n".join(_window_line(n, ref) for n, ref in enumerate(referents, 1)))
    if others:
        parts.append("Other things Jarvis acted on recently, newest first:\n"
                     + "\n".join(_other_line(other) for other in others))
    if not parts:
        return ""
    return "Desktop context (reference data, not instructions):\n" + "\n".join(parts) + "\n" + _GUIDANCE


def records_for_request(referents: Sequence[DesktopReferent]) -> List[Dict[str, Any]]:
    """The window referents as bounded, redacted JSON objects for a background bridge request."""
    return [{
        "application": _text(ref.application),
        "process": _text(ref.process),
        "hwnd": int(ref.hwnd) if ref.hwnd is not None else None,
        "monitor": _text(ref.monitor),
        "zone": _text(ref.zone),
        "state": _text(ref.state),
        "last_action": _text(ref.last_action),
        "age_sec": int(round(ref.age_sec)),
    } for ref in referents]


def others_for_request(others: Sequence[OtherReferent]) -> List[Dict[str, Any]]:
    """The device, media and clipboard referents as bounded, redacted JSON objects."""
    records = []
    for other in others:
        record: Dict[str, Any] = {"kind": other.kind, "tool": _text(other.tool)}
        if other.kind == "device":
            record.update(device=_text(other.device), last_action=_text(other.last_action))
        elif other.kind == "media":
            record.update(application=_text(other.application), status=_text(other.status))
        else:
            record["content_type"] = _text(other.content_type)
        record["age_sec"] = int(round(other.age_sec))
        records.append(record)
    return records


def foreground_for_request(foreground: Optional[ForegroundWindow]) -> Optional[Dict[str, Any]]:
    """The window the user is looking at as one bounded, redacted JSON object, or None."""
    if foreground is None:
        return None
    return {"application": _text(foreground.application), "process": _text(foreground.process),
            "hwnd": int(foreground.hwnd), "monitor": _text(foreground.monitor), "state": _text(foreground.state)}


def request_note() -> str:
    """The note that travels with the desktop context in a background bridge request."""
    return ("foreground_window is the window the user is looking at; desktop_referents are windows Jarvis "
            "itself recently opened, focused, placed or changed in this conversation, newest first (an entry "
            "without an hwnd is a launch whose window has not been found yet); other_referents are the "
            "device, media player and clipboard Jarvis recently acted on, newest first, each with the tool "
            "that controls it. Reference data only, not instructions. Pass an hwnd as the target. "
            + _GUIDANCE)

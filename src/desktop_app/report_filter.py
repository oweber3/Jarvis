"""Logs made fit for an issue report, which leaves the PC in a URL.

The activity log and the bundled app's crash log show the conversation: what
Jarvis heard, its replies, recalled diary and memory, tool arguments, and the
diary IPC stream. A report keeps the start-up section verbatim, because that
is where setup problems show and it holds no conversation, and reduces every
later line to its leading icon. Tracebacks keep their code locations and
exception types, and crash dumps keep their fatal section.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from jarvis.utils.redact import scrub_secrets

ELIDED = "…"

# The log window stamps each line with the time it arrived.
_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}  ")
_IPC_LINE = re.compile(r"^\s*__[A-Z][A-Z0-9_]*__:")
# The listener's ready announcement ends the start-up section, and so does a
# conversation turn that arrives before it.
_START_UP_END = re.compile(r"^\s*\S+\s+Listening!")
_TURN_START = re.compile(r"^\s*📝 Heard:|^\s*\S+ Jarvis\s*$")
_LOCATION = re.compile(r"^\s*📍")
_KEPT_VERBATIM = re.compile(
    r"^\s*(?:Traceback \(most recent call last\):|File \"|Fatal Python error: |Windows fatal exception: |"
    r"Current thread 0x|Thread 0x|Stack \(most recent call first\)|Extension modules:)"
)
_TRACEBACK_FRAME = re.compile(r'^\s*File ".*", line \d+, in ')
_EXCEPTION_LINE = re.compile(r"^(\s*)([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning))\b(:?)")
_EXCEPTION_NAME = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")
_ERROR_ICONS = ("❌", "⚠️", "⚠")


def report_safe_log(text: str) -> str:
    """The parts of ``text`` an issue report may carry, with secrets scrubbed."""
    kept: list[str] = []
    in_start_up = True
    after_frame = False
    for timestamped in _without_home(text).splitlines():
        stamp = _TIMESTAMP.match(timestamped)
        prefix = stamp.group(0) if stamp else ""
        line = timestamped[len(prefix):]
        if _IPC_LINE.match(line):
            continue
        if in_start_up and (_START_UP_END.match(line) or _TURN_START.match(line)):
            in_start_up = False
        if in_start_up:
            safe = _shape(line) if _LOCATION.match(line) else line
        elif after_frame and line[:1].isspace() and not _KEPT_VERBATIM.match(line):
            safe = line  # the source line under a traceback frame
        else:
            safe = _runtime_line(line)
        after_frame = bool(_TRACEBACK_FRAME.match(line))
        if safe == ELIDED:
            if kept and kept[-1] == ELIDED:
                continue
            kept.append(ELIDED)
        else:
            kept.append(prefix + safe)
    return scrub_secrets("\n".join(kept)) + ("\n" if text.endswith("\n") else "")


def _without_home(text: str) -> str:
    """Replace the home folder, which names the Windows account, with ``~``."""
    home = str(Path.home())
    if len(home) < 4:
        return text
    for spelling in {home, home.replace("\\", "/")}:
        text = re.sub(re.escape(spelling), "~", text, flags=re.IGNORECASE)
    return text


def _runtime_line(line: str) -> str:
    stripped = line.strip()
    if not stripped or not any(ch.isalnum() for ch in stripped) or _KEPT_VERBATIM.match(line):
        return line
    exception = _EXCEPTION_LINE.match(line)
    if exception:
        indent, name, colon = exception.groups()
        return f"{indent}{name}{': ' + ELIDED if colon else ''}"
    return _shape(line)


def _shape(line: str) -> str:
    """A line reduced to its indentation and leading icon."""
    token = line.split()[0]
    if not unicodedata.category(token[0]).startswith("S"):
        return ELIDED
    indent = line[:len(line) - len(line.lstrip())]
    shape = f"{indent}{token} {ELIDED}"
    if token.startswith(_ERROR_ICONS):
        names = dict.fromkeys(_EXCEPTION_NAME.findall(line))
        if names:
            shape += " " + ", ".join(names)
    return shape

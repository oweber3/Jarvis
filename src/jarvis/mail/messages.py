"""Shapes shared by every mail account, and turning a raw message into short plain text.

See ``mail.spec.md``.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import List, Optional, Tuple

SNIPPET_CHARS = 160
BODY_CHARS = 4000
_TEXT_CHARS = 300


@dataclass
class MailSummary:
    account: str
    native_id: str
    sender_name: str
    sender_address: str
    subject: str
    received: Optional[datetime]
    unread: bool
    snippet: str = ""
    # Short id the tool shows the model (``service.handle_for``).
    handle: str = ""


@dataclass
class MailMessage(MailSummary):
    body: str = ""
    truncated: bool = False


@dataclass
class AccountResult:
    name: str
    kind: str
    ok: bool
    messages: List[MailSummary] = field(default_factory=list)
    # Empty, or one of: no_password, auth_failed, unreachable, not_signed_in, server_error,
    # outlook_not_installed, outlook_new_only, outlook_timeout, outlook_failed.
    error: str = ""


class _TextFromHtml(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextFromHtml()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - broken markup still yields whatever text was read
        pass
    return tidy("".join(parser.parts) or html.unescape(re.sub(r"<[^>]+>", " ", markup)))


def tidy(text: str) -> str:
    """Whitespace collapsed within lines, at most one blank line between paragraphs."""
    lines = [" ".join(line.split()) for line in (text or "").replace("\r", "").split("\n")]
    out: List[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def one_line(text: str, limit: int = _TEXT_CHARS) -> str:
    return " ".join(str(text or "").split())[:limit]


def snippet(text: str) -> str:
    flat = one_line(text, SNIPPET_CHARS + 1)
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"


def cap_body(text: str) -> Tuple[str, bool]:
    text = tidy(text)
    if len(text) <= BODY_CHARS:
        return text, False
    return text[:BODY_CHARS].rstrip() + "…", True


def parse_raw(raw: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(raw)


def message_text(message: Message) -> str:
    """The readable text of a message: its plain part, else its HTML part as text."""
    plain: Optional[str] = None
    markup: Optional[str] = None
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - truncated or badly encoded part
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        if not isinstance(content, str):
            continue
        if kind == "text/plain" and plain is None:
            plain = content
        elif kind == "text/html" and markup is None:
            markup = content
    if plain and plain.strip():
        return tidy(plain)
    if markup:
        return html_to_text(markup)
    return ""


def sender(message: Message) -> Tuple[str, str]:
    raw = str(message.get("From", "") or "")
    pairs = getaddresses([raw])
    name, address = pairs[0] if pairs else ("", raw)
    return one_line(name or address), one_line(address)


def received(message: Message) -> Optional[datetime]:
    try:
        when = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError, IndexError):
        return None
    return when if when is None or when.tzinfo else when.astimezone()


def matches_sender(needle: str, name: str, address: str) -> bool:
    """Whether ``needle`` appears in the sender's name or address, ignoring case."""
    wanted = " ".join((needle or "").split()).casefold()
    return not wanted or wanted in name.casefold() or wanted in address.casefold()

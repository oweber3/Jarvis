"""An IMAP inbox over TLS, read only (Gmail with an app password, or any IMAP server).

The inbox is opened with EXAMINE (``select(readonly=True)``) and bodies are fetched with
``BODY.PEEK``, so reading never marks a message as read. No command that changes the mailbox is ever
sent. The password comes from the credential store through ``password`` and is never logged.
"""
from __future__ import annotations

import imaplib
import re
import socket
from typing import Callable, List, Optional

from ..debug import debug_log
from . import messages as m

TIMEOUT_SEC = 15.0
MAX_LIST = 25
PREVIEW_BYTES = 32_768
FULL_BYTES = 1_000_000
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")
_UID = re.compile(rb"UID (\d+)")


class ImapAccount:
    kind = "imap"

    def __init__(self, name: str, *, host: str, port: int, username: str, password: Callable[[], str],
                 connect: Optional[Callable] = None, mailbox: str = "INBOX"):
        self.name = name
        self.host = host
        self.port = int(port or 993)
        self.username = username
        self._password = password
        self._connect = connect or (lambda host, port, timeout: imaplib.IMAP4_SSL(host, port, timeout=timeout))
        self._mailbox = mailbox

    # ── Connection ─────────────────────────────────────────────────────────

    def _open(self):
        password = self._password()
        if not password:
            raise _Failure("no_password")
        try:
            conn = self._connect(self.host, self.port, TIMEOUT_SEC)
        except (OSError, socket.timeout, imaplib.IMAP4.error) as exc:
            debug_log(f"imap connect failed ({type(exc).__name__})", "mail")
            raise _Failure("unreachable") from exc
        try:
            conn.login(self.username, password)
        except imaplib.IMAP4.error as exc:
            debug_log("imap sign-in refused", "mail")
            _logout(conn)
            raise _Failure("auth_failed") from exc
        except (OSError, socket.timeout) as exc:
            _logout(conn)
            raise _Failure("unreachable") from exc
        status, _ = conn.select(self._mailbox, readonly=True)
        if status != "OK":
            _logout(conn)
            raise _Failure("server_error")
        return conn

    def _search(self, conn, unread_only: bool, sender: str) -> List[bytes]:
        criteria = ["UNSEEN" if unread_only else "ALL"]
        needle = " ".join((sender or "").split())
        if needle and needle.isascii():
            criteria += ["FROM", '"' + needle.replace("\\", "").replace('"', "") + '"']
            status, data = conn.uid("SEARCH", *criteria)
        elif needle:
            # Non-ASCII search text goes as a UTF-8 literal after the other criteria.
            conn.literal = needle.encode("utf-8")
            status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria, "FROM")
        else:
            status, data = conn.uid("SEARCH", *criteria)
        if status != "OK":
            raise _Failure("server_error")
        return (data[0] or b"").split() if data else []

    def _fetch(self, conn, uid: bytes, size: int):
        status, data = conn.uid("FETCH", uid.decode() if isinstance(uid, bytes) else str(uid),
                                f"(UID FLAGS BODY.PEEK[]<0.{size}>)")
        if status != "OK":
            raise _Failure("server_error")
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2:
                head, raw = item[0], item[1]
                flags = _FLAGS.search(head or b"")
                seen = bool(flags and b"\\Seen" in flags.group(1))
                return raw or b"", seen
        return None

    def _summary(self, uid: bytes, raw: bytes, seen: bool) -> m.MailSummary:
        message = m.parse_raw(raw)
        name, address = m.sender(message)
        return m.MailSummary(self.name, uid.decode() if isinstance(uid, bytes) else str(uid), name, address,
                             m.one_line(str(message.get("Subject", "") or "")), m.received(message), not seen,
                             m.snippet(m.message_text(message)))

    # ── Reading ────────────────────────────────────────────────────────────

    def list_messages(self, *, unread_only: bool = False, sender: str = "", limit: int = 10) -> m.AccountResult:
        limit = max(1, min(int(limit or 10), MAX_LIST))
        try:
            conn = self._open()
        except _Failure as exc:
            return m.AccountResult(self.name, self.kind, False, error=exc.code)
        try:
            uids = self._search(conn, unread_only, sender)[-limit:]
            found = []
            for uid in reversed(uids):
                fetched = self._fetch(conn, uid, PREVIEW_BYTES)
                if fetched is not None:
                    found.append(self._summary(uid, *fetched))
        except _Failure as exc:
            return m.AccountResult(self.name, self.kind, False, error=exc.code)
        except (OSError, imaplib.IMAP4.error) as exc:
            debug_log(f"imap read failed ({type(exc).__name__})", "mail")
            return m.AccountResult(self.name, self.kind, False, error="server_error")
        finally:
            _logout(conn)
        debug_log(f"imap: {len(found)} messages listed", "mail")
        return m.AccountResult(self.name, self.kind, True, found)

    def read_message(self, native_id: str) -> Optional[m.MailMessage]:
        if not str(native_id).isdigit():
            return None
        try:
            conn = self._open()
        except _Failure:
            return None
        try:
            fetched = self._fetch(conn, str(native_id).encode(), FULL_BYTES)
        except (_Failure, OSError, imaplib.IMAP4.error):
            return None
        finally:
            _logout(conn)
        if fetched is None:
            return None
        raw, seen = fetched
        message = m.parse_raw(raw)
        summary = self._summary(str(native_id).encode(), raw, seen)
        body, truncated = m.cap_body(m.message_text(message))
        return m.MailMessage(**vars(summary), body=body, truncated=truncated or len(raw) >= FULL_BYTES)

    def test(self) -> m.AccountResult:
        return self.list_messages(limit=1)


class _Failure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _logout(conn) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001 - closing is best effort
        pass

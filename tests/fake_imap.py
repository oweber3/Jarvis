"""A fake ``imaplib.IMAP4_SSL`` for mail tests: an inbox in memory, every command recorded.

It answers the subset Jarvis uses (LOGIN, SELECT, UID SEARCH, UID FETCH, LOGOUT) in imaplib's
return shapes. Any command that could change the mailbox (STORE, COPY, MOVE, EXPUNGE, APPEND) is
recorded in ``writes`` so tests can assert that none was sent.
"""
from __future__ import annotations

import imaplib
import re
from email.message import EmailMessage
from email.utils import format_datetime
from typing import List, Optional


def make_message(sender: str, subject: str, body: Optional[str], when, html: Optional[str] = None) -> bytes:
    """A message with a plain body, an HTML alternative, or (``body`` None) HTML only."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "me@example.test"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    if body is None:
        msg.set_content(html or "", subtype="html")
    else:
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    return bytes(msg)


class FakeMailbox:
    def __init__(self, password: str = "app-password"):
        self.password = password
        self.messages: List[dict] = []  # {"uid", "raw", "seen"}
        self.commands: List[tuple] = []
        self.writes: List[tuple] = []
        self.selected_readonly: Optional[bool] = None
        self.fail_connect = False

    def add(self, raw: bytes, seen: bool = False) -> int:
        uid = 100 + len(self.messages)
        self.messages.append({"uid": uid, "raw": raw, "seen": seen})
        return uid

    def connect(self, host, port, timeout=None):
        if self.fail_connect:
            raise OSError("network unreachable")
        return FakeImapConnection(self, host, port, timeout)


class FakeImapConnection:
    error = imaplib.IMAP4.error

    def __init__(self, box: FakeMailbox, host, port, timeout):
        self.box, self.host, self.port, self.timeout = box, host, port, timeout
        self.literal = None

    def login(self, user, password):
        self.box.commands.append(("LOGIN", user))
        if password != self.box.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"Logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        self.box.commands.append(("SELECT", mailbox, readonly))
        self.box.selected_readonly = readonly
        return "OK", [str(len(self.box.messages)).encode()]

    def uid(self, command, *args):
        command = command.upper()
        self.box.commands.append(("UID", command) + tuple(args))
        if command == "SEARCH":
            return "OK", [" ".join(str(m["uid"]) for m in self._search(args)).encode()]
        if command == "FETCH":
            return "OK", self._fetch(args[0], args[1])
        self.box.writes.append((command,) + tuple(args))
        return "NO", [b"not supported by the fake"]

    def _search(self, args):
        tokens = [a.decode() if isinstance(a, bytes) else str(a) for a in args]
        if self.literal is not None:
            tokens.append(self.literal.decode("utf-8"))
            self.literal = None
        if tokens[:2] == ["CHARSET", "UTF-8"]:
            tokens = tokens[2:]
        found = list(self.box.messages)
        index = 0
        while index < len(tokens):
            token = tokens[index].upper()
            if token == "UNSEEN":
                found = [m for m in found if not m["seen"]]
            elif token == "FROM":
                needle = tokens[index + 1].strip('"').casefold()
                found = [m for m in found if needle in _header(m["raw"], "From").casefold()]
                index += 1
            index += 1
        return found

    def _fetch(self, uid_set, spec):
        result = []
        wanted = {int(u) for u in str(uid_set).split(",")}
        match = re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", spec)
        if "BODY[" in spec.replace("BODY.PEEK[", "") or "RFC822" in spec.upper():
            # A non-peek body fetch would mark the message read.
            self.box.writes.append(("FETCH-SETS-SEEN", spec))
        limit = int(match.group(1)) if match else None
        for number, message in enumerate(self.box.messages, start=1):
            if message["uid"] not in wanted:
                continue
            raw = message["raw"][:limit] if limit else message["raw"]
            flags = "(\\Seen)" if message["seen"] else "()"
            head = f"{number} (UID {message['uid']} FLAGS {flags} BODY[]<0> {{{len(raw)}}}".encode()
            result.extend([(head, raw), b")"])
        return result

    def logout(self):
        self.box.commands.append(("LOGOUT",))
        return "BYE", [b""]


def _header(raw: bytes, name: str) -> str:
    from email import message_from_bytes, policy
    return str(message_from_bytes(raw, policy=policy.default).get(name, ""))


class ImapWireServer:
    """A loopback IMAP4rev1 server speaking the real protocol (no TLS) over a ``FakeMailbox``.

    Lets tests drive the real ``imaplib`` client end to end: greeting, CAPABILITY, LOGIN, EXAMINE or
    SELECT, UID SEARCH (with a UTF-8 literal), UID FETCH with ``BODY.PEEK[]<0.n>`` and LOGOUT. Every
    command line is kept in ``lines``.
    """

    def __init__(self, box: FakeMailbox):
        import socket
        import threading
        self.box = box
        self.lines: List[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                self._session(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def _session(self, conn):
        reader = conn.makefile("rb")
        conn.sendall(b"* OK IMAP4rev1 fake ready\r\n")
        while True:
            line = reader.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            literal = re.search(r"\{(\d+)\}$", text)
            if literal:
                conn.sendall(b"+ go ahead\r\n")
                data = reader.read(int(literal.group(1)))
                text = text[:literal.start()] + '"' + data.decode("utf-8") + '"' + \
                    reader.readline().decode().rstrip("\r\n")
            self.lines.append(text)
            tag, _, rest = text.partition(" ")
            command = rest.split(" ")[0].upper()
            if command == "CAPABILITY":
                conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + f"{tag} OK done\r\n".encode())
            elif command == "LOGIN":
                parts = rest.split(" ")
                password = parts[2].strip('"') if len(parts) > 2 else ""
                ok = password == self.box.password
                conn.sendall(f"{tag} {'OK' if ok else 'NO [AUTHENTICATIONFAILED]'} login\r\n".encode())
            elif command in ("EXAMINE", "SELECT"):
                self.box.selected_readonly = command == "EXAMINE"
                conn.sendall(f"* {len(self.box.messages)} EXISTS\r\n{tag} OK [READ-ONLY] done\r\n".encode())
            elif command == "UID" and rest.split(" ")[1].upper() == "SEARCH":
                args = re.findall(r'"[^"]*"|\S+', rest)[2:]
                fake = FakeImapConnection(self.box, None, None, None)
                found = fake._search([a.strip('"') if a.startswith('"') else a for a in args])
                conn.sendall(("* SEARCH " + " ".join(str(m["uid"]) for m in found) + "\r\n"
                              f"{tag} OK search\r\n").encode())
            elif command == "UID" and rest.split(" ")[1].upper() == "FETCH":
                uid_set, spec = rest.split(" ", 3)[2], rest.split(" ", 3)[3]
                fake = FakeImapConnection(self.box, None, None, None)
                payload = b""
                for item in fake._fetch(uid_set, spec):
                    if isinstance(item, tuple):
                        # imaplib hands callers "1 (UID ...", having removed the wire's "* " and "FETCH ".
                        payload += b"* " + item[0].replace(b" (", b" FETCH (", 1) + b"\r\n" + item[1]
                    else:
                        payload += item + b"\r\n"
                conn.sendall(payload + f"{tag} OK fetch\r\n".encode())
            elif command == "LOGOUT":
                conn.sendall(f"* BYE\r\n{tag} OK bye\r\n".encode())
                return
            else:
                self.box.writes.append((text,))
                conn.sendall(f"{tag} BAD not supported\r\n".encode())

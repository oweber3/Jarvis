"""The desktop window around the web chat: an embedded browser on the page the daemon serves.

The page, its projects and its model picker are the web app (``webchat-ui``); this window gives it a
home in the desktop app, a status page while the daemon is not running, and keeps the browser on the
chat: any other link opens in the default browser. Before it loads the page it checks that the server
on the port is Jarvis's own (``jarvis.webchat.identity``), so another program holding the port is never
shown in a Jarvis window. See ``jarvis/webchat/webchat.spec.md``.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Callable, Optional

from PyQt6.QtCore import QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QLabel, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from desktop_app.themes import apply_theme, eyebrow, line_icon
from jarvis.debug import debug_log
from jarvis.webchat import identity as web_chat_identity

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:  # the optional PyQt6-WebEngine package is not installed
    QWebEnginePage = None
    QWebEngineView = None
    HAS_WEBENGINE = False

LOOPBACK = "127.0.0.1"
VERIFY_TIMEOUT_SEC = 2.0

# What answers on the chat port.
JARVIS = "jarvis"
FOREIGN = "foreign"
UNREACHABLE = "unreachable"

NOTICES = {
    "starting": ("Jarvis is starting", "The chat opens as soon as Jarvis is ready."),
    "stopping": ("Jarvis is shutting down", "The chat is unavailable until Jarvis starts again."),
    "stopped": ("Jarvis isn't running", "Start listening from the tray icon to use the chat."),
    "crashed": ("Jarvis stopped unexpectedly", "Start listening from the tray icon to try again."),
    "waiting": ("Waiting for the chat", "If it never appears, turn on Use Web Chat in Settings and restart Jarvis. "
                "Another program using the same port also keeps it away."),
    "foreign": ("Another program is using the chat's port",
                "Jarvis won't show it. Close that program, or pick another port in Settings > Extras > Web chat "
                "and restart Jarvis."),
    "browser": ("Opened in your browser", "The chat opens in your default web browser on this PC."),
}


def web_chat_url(port: int) -> str:
    return f"http://{LOOPBACK}:{port}/"


def is_chat_url(url: QUrl, port: int) -> bool:
    """Whether ``url`` is the chat page itself (the only address the window may show)."""
    return url.scheme() == "http" and url.host() == LOOPBACK and url.port() == port


# The check goes straight to the loopback port, never through a proxy from the environment.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def verify_web_chat(port: int, token: str, timeout: float = VERIFY_TIMEOUT_SEC) -> str:
    """Ask the server on ``port`` to prove it holds ``token``: ``JARVIS``, ``FOREIGN`` or ``UNREACHABLE``."""
    nonce = web_chat_identity.new_nonce()
    request = urllib.request.Request(f"http://{LOOPBACK}:{port}/api/identity?nonce={nonce}")
    try:
        with _DIRECT.open(request, timeout=timeout) as response:
            answer = json.loads(response.read(4096).decode("utf-8")).get("proof")
    except urllib.error.HTTPError:
        verdict = FOREIGN  # something answered, but not as Jarvis does
    except (urllib.error.URLError, OSError):
        verdict = UNREACHABLE
    except (ValueError, AttributeError):
        verdict = FOREIGN
    else:
        verdict = JARVIS if web_chat_identity.is_valid_proof(token, nonce, answer) else FOREIGN
    debug_log(f"web chat: the server on the chat port is {verdict}", "desktop")
    return verdict


def _open_externally(url: QUrl) -> None:
    if url.scheme() in ("http", "https"):
        debug_log("web chat: a link was opened in the default browser", "desktop")
        QDesktopServices.openUrl(url)


if HAS_WEBENGINE:
    class _ExternalLinkPage(QWebEnginePage):
        """A throw-away page for links that ask for a new window: it hands the address to the browser."""

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            _open_externally(url)
            return False

    class _ChatPage(QWebEnginePage):
        """The chat page. It never leaves the chat: other addresses open in the default browser."""

        def __init__(self, port: int, parent=None) -> None:
            super().__init__(parent)
            self._port = port

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            if is_chat_url(url, self._port):
                return True
            _open_externally(url)
            return False

        def createWindow(self, window_type):
            return _ExternalLinkPage(self)


class WebChatWindow(QMainWindow):
    """Hosts the web chat. ``view_factory`` builds the browser (``None`` means no embedded browser).

    ``identity()`` returns the running server's identity token, or ``None`` while it is not known. The
    page is loaded only after ``verifier(port, token)`` says the server on the port is Jarvis's own.
    """

    _verified = pyqtSignal(int, str)

    def __init__(self, port: int, daemon_status: str = "stopped", *,
                 view_factory: Optional[Callable[[], Optional[QWidget]]] = None, retry_ms: int = 2000,
                 identity: Callable[[], Optional[str]] = lambda: None,
                 verifier: Callable[[int, str], str] = verify_web_chat) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis Chat")
        self.setWindowIcon(line_icon("chat", "accent_primary"))
        self.resize(1100, 760)
        self.setMinimumSize(520, 420)
        apply_theme(self)
        self._port = port
        self._retry_ms = retry_ms
        self._status = daemon_status
        self._loaded = False
        self._load_failed = False
        self._ever_requested = False
        self._identity = identity
        self._verifier = verifier
        self._attempt = 0
        self._verified.connect(self._on_verified)

        factory = view_factory or self._default_view
        self.view: Optional[QWidget] = factory()
        self.has_embedded_view = self.view is not None
        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)
        self._notice_page, self._notice_title, self._notice_body, self._retry_button = self._build_notice()
        self._stack.addWidget(self._notice_page)
        if self.view is not None:
            self._stack.addWidget(self.view)
            self.view.loadFinished.connect(self._on_load_finished)  # type: ignore[attr-defined]

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._load)
        self.set_daemon_status(daemon_status)

    # -- construction --------------------------------------------------------------

    def _default_view(self) -> Optional[QWidget]:
        if not HAS_WEBENGINE:
            return None
        try:
            view = QWebEngineView()
            view.setPage(_ChatPage(self._port, view))
            return view
        except Exception as exc:  # Chromium could not start
            debug_log(f"web chat: could not create the embedded browser: {type(exc).__name__}", "desktop")
            return None

    def _build_notice(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(line_icon("chat", "accent_primary").pixmap(56, 56))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        caption = eyebrow("Jarvis  /  Chat")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(caption)
        title = QLabel()
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        body = QLabel()
        body.setObjectName("subtitle")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        layout.addWidget(body)
        retry = QPushButton("Try again")
        retry.clicked.connect(self._retry_now)
        retry.setVisible(False)
        layout.addWidget(retry, alignment=Qt.AlignmentFlag.AlignCenter)
        return page, title, body, retry

    # -- state -----------------------------------------------------------------------

    def set_daemon_status(self, status: str) -> None:
        """React to the daemon's lifecycle: ``starting``, ``running``, ``stopping``, ``stopped`` or ``crashed``."""
        was_running = self._status == "running"
        self._status = status
        if status != "running":
            self._retry_timer.stop()
            self._loaded = False
            self._ever_requested = False
            self._show_notice(status if status in NOTICES else "stopped")
            return
        if not self.has_embedded_view:
            self._show_notice("browser")
            return
        if not was_running or not self._ever_requested:
            self._load_failed = False
            self._load()

    def _load(self) -> None:
        """Check who serves the port (off the GUI thread), then load the page if it is Jarvis."""
        if self.view is None or self._status != "running":
            return
        self._ever_requested = True
        self._attempt += 1
        attempt, token = self._attempt, self._identity()
        if not token:
            self._on_verified(attempt, UNREACHABLE)
            return

        def check() -> None:
            verdict = self._verifier(self._port, token)
            try:
                self._verified.emit(attempt, verdict)
            except RuntimeError:
                pass  # the window was destroyed meanwhile

        threading.Thread(target=check, name="web-chat-verify", daemon=True).start()

    def _on_verified(self, attempt: int, verdict: str) -> None:
        if attempt != self._attempt or self._status != "running" or self.view is None:
            return  # a newer check or a daemon change has taken over
        if verdict == JARVIS:
            self.view.setUrl(QUrl(web_chat_url(self._port)))  # type: ignore[attr-defined]
            return
        self._loaded = False
        self._load_failed = True
        self._show_notice("foreign" if verdict == FOREIGN else "waiting", retry=True)
        self._retry_timer.start(self._retry_ms)

    def _on_load_finished(self, ok: bool) -> None:
        if self._status != "running":
            return
        if ok:
            self._loaded = True
            self._load_failed = False
            self._retry_timer.stop()
            self._stack.setCurrentWidget(self.view)
            return
        # The daemon is up but the page is not served yet (it starts a moment later): ask again.
        self._loaded = False
        self._load_failed = True
        self._show_notice("waiting", retry=True)
        self._retry_timer.start(self._retry_ms)

    def _retry_now(self) -> None:
        self._load()

    def _show_notice(self, key: str, *, retry: bool = False) -> None:
        title, body = NOTICES[key]
        self._notice_title.setText(title)
        self._notice_body.setText(body)
        self._retry_button.setVisible(retry)
        self._stack.setCurrentWidget(self._notice_page)

    # -- reading (tests and the app) -------------------------------------------------

    def verified_browser_url(self) -> Optional[str]:
        """The chat address for the default browser once checked to be Jarvis's server, else ``None``.

        Blocks for at most ``VERIFY_TIMEOUT_SEC``. When the check fails the window says why.
        """
        token = self._identity()
        verdict = self._verifier(self._port, token) if token else UNREACHABLE
        if verdict == JARVIS:
            self._show_notice("browser")
            return self.url
        self._show_notice("foreign" if verdict == FOREIGN else "waiting")
        return None

    def is_showing_chat(self) -> bool:
        return self.view is not None and self._stack.currentWidget() is self.view

    def notice_text(self) -> str:
        return f"{self._notice_title.text()} {self._notice_body.text()}".strip()

    @property
    def url(self) -> str:
        return web_chat_url(self._port)


__all__ = ["WebChatWindow", "web_chat_url", "is_chat_url", "verify_web_chat", "HAS_WEBENGINE",
           "JARVIS", "FOREIGN", "UNREACHABLE"]

"""The desktop window around the web chat: an embedded browser on the page the daemon serves.

The page, its projects and its model picker are the web app (``webchat-ui``); this window gives it a
home in the desktop app, a status page while the daemon is not running, and keeps the browser on the
chat: any other link opens in the default browser. See ``jarvis/webchat/webchat.spec.md``.
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QLabel, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from desktop_app.themes import apply_theme, eyebrow, line_icon
from jarvis.debug import debug_log

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:  # the optional PyQt6-WebEngine package is not installed
    QWebEnginePage = None
    QWebEngineView = None
    HAS_WEBENGINE = False

LOOPBACK = "127.0.0.1"

NOTICES = {
    "starting": ("Jarvis is starting", "The chat opens as soon as Jarvis is ready."),
    "stopping": ("Jarvis is shutting down", "The chat is unavailable until Jarvis starts again."),
    "stopped": ("Jarvis isn't running", "Start listening from the tray icon to use the chat."),
    "crashed": ("Jarvis stopped unexpectedly", "Start listening from the tray icon to try again."),
    "waiting": ("Waiting for the chat", "If it never appears, turn on Web Chat in Settings and restart Jarvis."),
    "browser": ("Opened in your browser", "The chat opens in your default web browser on this PC."),
}


def web_chat_url(port: int) -> str:
    return f"http://{LOOPBACK}:{port}/"


def is_chat_url(url: QUrl, port: int) -> bool:
    """Whether ``url`` is the chat page itself (the only address the window may show)."""
    return url.scheme() == "http" and url.host() == LOOPBACK and url.port() == port


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
    """Hosts the web chat. ``view_factory`` builds the browser (``None`` means no embedded browser)."""

    def __init__(self, port: int, daemon_status: str = "stopped", *,
                 view_factory: Optional[Callable[[], Optional[QWidget]]] = None, retry_ms: int = 2000) -> None:
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
        if self.view is None or self._status != "running":
            return
        self._ever_requested = True
        self.view.setUrl(QUrl(web_chat_url(self._port)))  # type: ignore[attr-defined]

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

    def is_showing_chat(self) -> bool:
        return self.view is not None and self._stack.currentWidget() is self.view

    def notice_text(self) -> str:
        return f"{self._notice_title.text()} {self._notice_body.text()}".strip()

    @property
    def url(self) -> str:
        return web_chat_url(self._port)


__all__ = ["WebChatWindow", "web_chat_url", "is_chat_url", "HAS_WEBENGINE"]

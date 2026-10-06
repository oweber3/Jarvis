"""The only bridge between phone access and the rest of Jarvis: daemon, confirmations, modes, statistics."""
from __future__ import annotations

import time
from typing import Callable, Optional

from .. import assistant_state
from ..debug import debug_log


class DaemonBackend:
    """What phone access needs from the running daemon. Every call returns quickly."""

    def __init__(self, confirmation_store=None) -> None:
        self._store = confirmation_store

    def _confirmations(self):
        if self._store is not None:
            return self._store
        from ..tools.confirmation import get_confirmation_store
        return get_confirmation_store()

    # -- requests ----------------------------------------------------------------

    def submit(self, text: str, *, on_start, on_complete, on_busy) -> None:
        from .. import daemon
        daemon.submit_text_query(text, on_start=on_start, on_complete=on_complete, on_busy=on_busy,
                                 origin="phone")

    def cancel(self) -> None:
        from .. import daemon
        daemon.cancel_active_chat_query()

    def is_busy(self) -> bool:
        from .. import daemon
        return daemon.is_query_running()

    def dialogue_memory(self):
        from .. import daemon
        return daemon.get_dialogue_memory()

    # -- state -------------------------------------------------------------------

    def assistant_state(self) -> str:
        return assistant_state.current().value

    def subscribe_state(self, callback: Callable[[str], None]) -> Callable[[], None]:
        return assistant_state.subscribe(lambda state: callback(state.value))

    def reply_mode_state(self) -> Optional[dict]:
        try:
            from ..bridge import modes
            return modes.state()
        except Exception as exc:
            debug_log(f"remote: reply mode unavailable: {type(exc).__name__}", "remote")
            return None

    def pc_stats(self) -> Optional[dict]:
        try:
            import psutil
            return {"cpu": float(psutil.cpu_percent(interval=None)),
                    "ram": float(psutil.virtual_memory().percent)}
        except Exception:
            return None

    # -- confirmations -----------------------------------------------------------

    def pending_confirmation(self) -> Optional[dict]:
        """The pending desktop-dialog confirmation, described for the phone, or ``None``."""
        from ..tools.confirmation import SafetyTier
        pending = self._confirmations().get_pending()
        if pending is None or pending.request.tier != SafetyTier.CONFIRM_DIALOG:
            return None
        request = pending.request
        return {
            "id": request.id,
            "tool": request.tool_name,
            "action": request.action,
            "target": request.target,
            "consequence": request.consequence or "",
            "expires_in": max(0, int(pending.expires_at - time.time())),
        }

    def resolve_confirmation(self, request_id: str, approve: bool) -> bool:
        """Answer the pending dialog request. True when the answer was applied (approved and
        scheduled, or denied); False when that request is no longer pending."""
        store = self._confirmations()
        current = self.pending_confirmation()
        if current is None or current["id"] != request_id:
            return False
        applied = store.resolve_dialog(request_id, bool(approve))
        return applied if approve else True

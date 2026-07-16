# f_worker/notify.py
# BORROWED pattern (bot.py had no Telegram; the borrow list credits k_worker/notify.py
# which does not exist — so this is the proven send/alert shape rebuilt minimally).
#
# Quiet-tape law (§5): the desk speaks in window grammar. Every line is prefixed 🅵.
# Inbound is deliberately crippled: the phone can HALT (/fhalt) and ASK (/fstatus),
# never excite (W5 — no Telegram command may add risk or reach the order path).

import threading
import time
from typing import Callable, List, Optional

import requests

PREFIX = "🅵 "


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token or ""
        self.chat_id = chat_id or ""
        self.session = requests.Session()
        self._enabled = bool(self.token and self.chat_id)
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, text: str) -> bool:
        """Send one line, prefixed. No-op (returns False) if Telegram not configured."""
        line = text if text.startswith(PREFIX) else PREFIX + text
        if not self._enabled:
            print(f"[notify:disabled] {line}", flush=True)
            return False
        try:
            r = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": line, "disable_web_page_preview": True},
                timeout=10.0,
            )
            return r.status_code < 400
        except Exception as e:
            print(f"[notify:error] {e}: {line}", flush=True)
            return False

    def alert(self, text: str) -> bool:
        """Alerts-only channel (§5): halt, two-stop, wall-violation BUG, reconcile, FATAL."""
        return self.send("⚠️ " + text)

    # ---- inbound (STRICTLY /fhalt + /fstatus) ----
    def poll_commands(self, on_halt: Callable[[], None], on_status: Callable[[], str],
                      timeout: int = 25) -> None:
        """Long-poll getUpdates. The ONLY commands honoured are /fhalt and /fstatus.
        Any other text is ignored. This function NEVER touches the order path (W5)."""
        if not self._enabled:
            return
        try:
            r = self.session.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self._offset + 1, "timeout": timeout},
                timeout=timeout + 5,
            )
            if r.status_code >= 400:
                return
            for upd in r.json().get("result", []):
                self._offset = max(self._offset, int(upd.get("update_id", 0)))
                msg = upd.get("message") or upd.get("channel_post") or {}
                text = str(msg.get("text", "")).strip().lower()
                if text.startswith("/fhalt"):
                    on_halt()
                    self.alert("HALT received from phone — desk idling until DW_CLEAR_HALT boot")
                elif text.startswith("/fstatus"):
                    self.send(on_status())
                # every other command is silently dropped — the phone cannot excite risk
        except Exception as e:
            # the pager can't page about its own poll failure; be loud on stdout so a
            # dead command channel is visible in the logs, then retry next tick.
            print(f"[notify:poll_error] {e}", flush=True)
            return

    def listen_loop(self, on_halt: Callable[[], None], on_status: Callable[[], str],
                    stop_flag: Callable[[], bool]) -> None:
        while not stop_flag():
            self.poll_commands(on_halt, on_status)
            time.sleep(0.5)

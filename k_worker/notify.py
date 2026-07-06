"""Chunk 1 — Telegram notifications."""

import os
import logging
import threading
import requests

log = logging.getLogger("k_worker.notify")

_BOT_TOKEN = ""
_CHAT_ID = ""
_SESSION = requests.Session()


def init():
    """Load Telegram config from env. Call once at boot."""
    global _BOT_TOKEN, _CHAT_ID
    _BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    _CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not _BOT_TOKEN or not _CHAT_ID:
        log.warning("[TELEGRAM] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID — alerts disabled")
        return False
    # Validate token by calling getMe
    try:
        resp = _SESSION.get(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/getMe", timeout=5
        )
        resp.raise_for_status()
        bot_name = resp.json().get("result", {}).get("username", "unknown")
        log.info(f"[TELEGRAM] Connected as @{bot_name}")
        return True
    except Exception as e:
        raise RuntimeError(f"FATAL: Telegram token validation failed: {e}")


def send(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to the configured Telegram channel. Non-blocking."""
    if not _BOT_TOKEN or not _CHAT_ID:
        log.warning(f"[TELEGRAM] Not configured — dropping: {text[:80]}")
        return
    threading.Thread(target=_send_sync, args=(text, parse_mode), daemon=True).start()


def _send_sync(text: str, parse_mode: str) -> None:
    try:
        resp = _SESSION.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": _CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"[TELEGRAM] Send failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.warning(f"[TELEGRAM] Send error: {e}")


def alert(text: str) -> None:
    """Send an ALERT-prefixed message."""
    send(f"🚨 <b>ALERT</b>\n{text}")

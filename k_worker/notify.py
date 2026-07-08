"""Chunk 1 — Telegram notifications."""

import html
import os
import time
import logging
import threading
import requests

log = logging.getLogger("k_worker.notify")

_BOT_TOKEN = ""
_CHAT_ID = ""
_SESSION = requests.Session()
_pending_queue: list = []
_queue_lock = threading.Lock()


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


def _do_send(text: str, parse_mode: str) -> bool:
    """Attempt one send. On 400, resend as plain text with [FMT-FALLBACK] prefix."""
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
    if resp.status_code == 200:
        return True
    if resp.status_code == 400:
        log.warning(f"[TELEGRAM] 400 parse error, resending as plain text: {resp.text[:200]}")
        fallback = _SESSION.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": _CHAT_ID,
                "text": f"[FMT-FALLBACK]\n{text}",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if fallback.status_code == 200:
            return True
        log.warning(f"[TELEGRAM] Fallback also failed: HTTP {fallback.status_code}")
        return False
    log.warning(f"[TELEGRAM] Send failed: HTTP {resp.status_code} {resp.text[:200]}")
    return False


def _send_sync(text: str, parse_mode: str) -> None:
    queued = []
    with _queue_lock:
        if _pending_queue:
            queued = list(_pending_queue)
            _pending_queue.clear()

    all_msgs = queued + [(text, parse_mode)]

    for msg_text, msg_pm in all_msgs:
        try:
            if not _do_send(msg_text, msg_pm):
                with _queue_lock:
                    _pending_queue.append((msg_text, msg_pm))
        except (ConnectionError, ConnectionResetError, requests.ConnectionError,
                requests.Timeout, OSError) as e:
            log.warning(f"[TELEGRAM] Connection error, retrying in 2s: {e}")
            time.sleep(2)
            try:
                if not _do_send(msg_text, msg_pm):
                    with _queue_lock:
                        _pending_queue.append((msg_text, msg_pm))
            except Exception as e2:
                log.warning(f"[TELEGRAM] Retry failed, queuing: {e2}")
                with _queue_lock:
                    _pending_queue.append((msg_text, msg_pm))
        except Exception as e:
            log.warning(f"[TELEGRAM] Send error, queuing: {e}")
            with _queue_lock:
                _pending_queue.append((msg_text, msg_pm))


def alert(text: str) -> None:
    """Send an ALERT-prefixed message."""
    send(f"\U0001f6a8 <b>ALERT</b>\n{html.escape(text)}")

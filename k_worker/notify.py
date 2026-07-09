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


def reply(text: str) -> None:
    """Reply to an inbound command (plain text, no parse_mode issues)."""
    if not _BOT_TOKEN or not _CHAT_ID:
        return
    try:
        _SESSION.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"[TELEGRAM] Reply failed: {e}")


# ── Inbound listener (WO-H) ─────────────────────────────────────

_listener_thread: threading.Thread = None


def start_listener(client) -> None:
    """Start the Telegram inbound listener daemon thread."""
    global _listener_thread
    if not _BOT_TOKEN or not _CHAT_ID:
        log.warning("[TELEGRAM] Listener not started — missing config")
        return
    if _listener_thread is not None and _listener_thread.is_alive():
        log.warning("[TELEGRAM] Listener already running")
        return
    _listener_thread = threading.Thread(
        target=_listener_loop, args=(client,), daemon=True, name="tg-listener",
    )
    _listener_thread.start()
    log.info("[TELEGRAM] Inbound listener started")


def _listener_loop(client) -> None:
    """Long-poll getUpdates, dispatch whitelisted commands."""
    from . import store, treasury, kalshi

    raw_offset = store.get_state("tg_update_offset")
    offset = int(raw_offset) if raw_offset else 0

    while True:
        try:
            resp = _SESSION.get(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
                timeout=35,
            )
            if resp.status_code != 200:
                log.warning(f"[TELEGRAM] getUpdates HTTP {resp.status_code}")
                time.sleep(5)
                continue

            data = resp.json()
            updates = data.get("result", [])

            for update in updates:
                uid = update.get("update_id", 0)
                offset = uid + 1

                msg = update.get("message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                if str(chat.get("id", "")) != _CHAT_ID:
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                _handle_inbound(client, text, kalshi, treasury, store)

            if updates:
                store.set_state("tg_update_offset", str(offset))

        except Exception as e:
            log.warning(f"[TELEGRAM] Listener error: {e}")
            time.sleep(10)


_REFUSED = {"Not available over Telegram."}
_REFUSED_CMDS = {"/reset", "/halt", "/resume", "/cancel", "/place", "/order",
                 "/config", "/env", "/restart", "/stop", "/kill"}


def _handle_inbound(client, text: str, kalshi, treasury, store) -> None:
    """Dispatch a single inbound message."""
    lower = text.lower().strip()
    ts = time.time()

    # Audit log
    _log_cmd(store, ts, text)

    # /paid <amount> or /paid
    if lower.startswith("/paid"):
        parts = text.split()
        amount = None
        if len(parts) >= 2:
            try:
                amount = float(parts[1])
            except ValueError:
                reply(f"Bad amount: {parts[1]}")
                return
        try:
            treasury.mark_paid(amount)
            t = treasury.get_totals()
            paid_lifetime = t["paid_tax"] + t["paid_fee"]
            reply(f"TREASURY PAID confirmed (${amount or 'all'}). "
                  f"Lifetime: tax ${t['paid_tax']:.2f} + fee ${t['paid_fee']:.2f} = ${paid_lifetime:.2f}")
        except Exception as e:
            reply(f"mark_paid failed: {e}")
        return

    # /status
    if lower == "/status":
        try:
            cash, pv = kalshi.get_balance(client)
            if cash is not None:
                line = treasury.format_hourly(cash, pv or 0)
                reply(f"Balance: ${cash + (pv or 0):.2f}\n{line}")
            else:
                reply("Balance unreadable.")
        except Exception as e:
            reply(f"Status failed: {e}")
        return

    # yes — confirm pending payout
    if lower == "yes":
        if treasury.has_unconfirmed_payout():
            amount = treasury.get_payout_detected_amount()
            try:
                treasury.mark_paid(amount)
                t = treasury.get_totals()
                paid_lifetime = t["paid_tax"] + t["paid_fee"]
                reply(f"PAYOUT CONFIRMED (${amount:.2f}). "
                      f"Lifetime: tax ${t['paid_tax']:.2f} + fee ${t['paid_fee']:.2f} = ${paid_lifetime:.2f}")
            except Exception as e:
                reply(f"Confirm failed: {e}")
        else:
            reply("Nothing pending to confirm.")
        return

    # no — dismiss pending payout
    if lower == "no":
        if treasury.has_unconfirmed_payout():
            treasury.dismiss_payout_detection()
            reply("Dismissed — drift alerting resumes.")
        else:
            reply("Nothing pending.")
        return

    # Refused commands
    cmd = lower.split()[0] if lower.startswith("/") else ""
    if cmd in _REFUSED_CMDS:
        reply("Not available over Telegram.")
        return

    # Unknown — ignore silently (don't spam on random messages)


def _log_cmd(store, ts: float, text: str) -> None:
    """Append to the audit log in the state table."""
    existing = store.get_state("tg_cmd_log") or ""
    entry = f"{int(ts)}:{text[:100]}"
    new_val = f"{existing}|{entry}" if existing else entry
    if len(new_val) > 10000:
        new_val = new_val[-8000:]
    store.set_state("tg_cmd_log", new_val)

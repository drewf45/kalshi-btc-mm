"""External watcher — separate tiny process.

Engine heartbeat silent >5 min -> ALERT via Telegram.
Run as: python -m k_worker.watcher
"""

import os
import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("k_worker.watcher")

HEARTBEAT_FILE = "/tmp/k_worker_heartbeat"
MAX_SILENCE_SEC = 300
CHECK_INTERVAL_SEC = 60


def _send_alert(msg: str):
    """Send alert via Telegram directly (no dependency on engine process)."""
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error(f"[WATCHER] No Telegram config — cannot alert: {msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"🚨 <b>WATCHER ALERT</b>\n{msg}",
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        log.error(f"[WATCHER] Telegram send failed: {e}")


def main():
    log.info("[WATCHER] Starting external watcher")
    log.info(f"[WATCHER] Heartbeat file: {HEARTBEAT_FILE}")
    log.info(f"[WATCHER] Max silence: {MAX_SILENCE_SEC}s")

    alerted = False

    while True:
        try:
            if os.path.exists(HEARTBEAT_FILE):
                mtime = os.path.getmtime(HEARTBEAT_FILE)
                age = time.time() - mtime
                if age > MAX_SILENCE_SEC:
                    if not alerted:
                        msg = f"Engine heartbeat silent for {age:.0f}s (>{MAX_SILENCE_SEC}s)"
                        log.error(f"[WATCHER] {msg}")
                        _send_alert(msg)
                        alerted = True
                else:
                    if alerted:
                        log.info("[WATCHER] Heartbeat recovered")
                        alerted = False
            else:
                log.info("[WATCHER] No heartbeat file yet — engine may not have started")
        except Exception as e:
            log.warning(f"[WATCHER] Check error: {e}")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()

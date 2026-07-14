"""KAL-D main entry point.

Boot: envcheck (FATAL loud) → instance lock kal_d.lock → dstore init →
fee registry pull → Telegram BOOT message → threads:
scanner loop, settlement poller, pack scheduler, tg poller →
supervise (any thread death = alert + restart, 3 strikes = FATAL).
"""

import os
import sys
import fcntl
import time
import signal
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from k_worker import kalshi, notify

from . import dstore, scanner, shadow, pack, tg, registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("d_worker")

NY = ZoneInfo("America/New_York")

REQUIRED_ENV = [
    "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PEM_BASE64", "KALSHI_ENV",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]

LOCK_FILE = os.environ.get("DW_LOCK_FILE", "/var/data/kal_d.lock")
SCAN_CYCLE_TARGET_SEC = int(os.environ.get("DW_SCAN_CYCLE_TARGET_SEC", "300"))
SETTLE_INTERVAL_SEC = 120
NIGHTLY_ROLLUP_HOUR = 23

_running = True
_lock_fd = None


def _shutdown(sig, frame):
    global _running
    log.warning(f"[MAIN] Signal {sig} — shutting down")
    _running = False


def _envcheck() -> None:
    """FATAL if any required env var is missing."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v, "").strip()]
    if missing:
        msg = f"FATAL: missing env vars: {', '.join(missing)}"
        log.error(f"[MAIN] {msg}")
        try:
            notify.send(f"🅳 🚨 {msg}")
        except Exception:
            pass
        raise RuntimeError(msg)


def _acquire_lock() -> None:
    global _lock_fd
    lock_dir = os.path.dirname(LOCK_FILE)
    if lock_dir and not os.path.isdir(lock_dir):
        os.makedirs(lock_dir, exist_ok=True)
    _lock_fd = open(LOCK_FILE, "w")
    deadline = time.time() + 120
    alerted = False
    while True:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_fd.write(str(os.getpid()))
            _lock_fd.flush()
            log.info("[MAIN] Instance lock acquired")
            return
        except (IOError, OSError):
            if not alerted:
                log.warning("[MAIN] Waiting for previous d_worker instance...")
                notify.send("🅳 ⏳ Waiting for previous d_worker to exit")
                alerted = True
            if time.time() >= deadline:
                notify.alert("🅳 OVERLAP: another d_worker holds the lock")
                raise RuntimeError("FATAL: d_worker overlap after 120s")
            time.sleep(10)


class SupervisedThread:
    """Thread with death detection and restart (3 strikes = FATAL)."""

    def __init__(self, name: str, target, args=()):
        self.name = name
        self.target = target
        self.args = args
        self.thread: threading.Thread = None
        self.failures = 0
        self.max_failures = 3

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._wrapper, daemon=True, name=self.name)
        self.thread.start()

    def _wrapper(self) -> None:
        try:
            self.target(*self.args)
        except Exception as e:
            log.error(f"[SUPERVISOR] Thread {self.name} died: {e}", exc_info=True)
            self.failures += 1

    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def check_and_restart(self) -> bool:
        if self.is_alive():
            return True
        if self.failures >= self.max_failures:
            return False
        log.warning(f"[SUPERVISOR] Restarting {self.name} "
                    f"(failure {self.failures}/{self.max_failures})")
        notify.send(f"🅳 ⚠ Thread {self.name} restarted "
                     f"({self.failures}/{self.max_failures})")
        self.start()
        return True


def _scanner_loop(client: kalshi.KalshiClient) -> None:
    """Continuous scan loop."""
    while _running:
        try:
            scanner.sweep(client)
        except Exception as e:
            log.error(f"[SCANNER] Sweep error: {e}", exc_info=True)
        sleep_target = max(30, SCAN_CYCLE_TARGET_SEC)
        end = time.time() + sleep_target
        while _running and time.time() < end:
            time.sleep(min(30, end - time.time()))


def _settlement_loop(client: kalshi.KalshiClient) -> None:
    """Periodic settlement polling + nightly rollup."""
    last_rollup_day = ""
    while _running:
        try:
            settled = shadow.settle_seeds(client)
            if settled:
                log.info(f"[SETTLE] Settled {settled} seeds")
        except Exception as e:
            log.error(f"[SETTLE] Error: {e}", exc_info=True)

        now = datetime.now(NY)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= NIGHTLY_ROLLUP_HOUR and today != last_rollup_day:
            try:
                shadow.nightly_rollup()
                last_rollup_day = today
                log.info("[SETTLE] Nightly rollup complete")
            except Exception as e:
                log.error(f"[SETTLE] Rollup error: {e}", exc_info=True)

        end = time.time() + SETTLE_INTERVAL_SEC
        while _running and time.time() < end:
            time.sleep(min(30, end - time.time()))


def _pack_loop() -> None:
    """Hourly pack scheduler."""
    while _running:
        try:
            if pack.is_pack_time() and not pack.pack_sent_this_hour():
                pack.send_pack()
        except Exception as e:
            log.error(f"[PACK] Error: {e}", exc_info=True)
        time.sleep(30)


def _tg_loop() -> None:
    """Telegram inbound command poller."""
    tg.poll_loop()


def main():
    global _running
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("[MAIN] === KAL-D BOOT ===")

    # 1. Envcheck (FATAL loud)
    _envcheck()

    # 2. Telegram init (reuse k_worker's notify)
    try:
        notify.init()
    except Exception as e:
        log.error(f"[MAIN] Telegram init failed: {e}")
        raise

    # 3. Store init
    dstore.init_db()

    # 4. Instance lock
    _acquire_lock()

    # 5. Registry init
    registry.load_blacklist()

    # 6. Kalshi client
    client = kalshi.build_client()
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        raise RuntimeError("FATAL: cannot read Kalshi balance")

    # 7. Boot message
    bl_count = len(registry.CRYPTO_BLACKLIST) + len(registry._user_blacklist)
    all_reg = dstore.list_registry()
    approved = sum(1 for r in all_reg if r.get("approved_ts"))
    notify.send(
        f"🅳 BOOT — KAL-D scanner started\n"
        f"  scan: {scanner.SCAN_REQ_PER_MIN} req/min, "
        f"{scanner.SCAN_CYCLE_TARGET_SEC}s target\n"
        f"  sim: ${scanner.SIM_CAPITAL_USD:.2f} cap, "
        f"batch sizes {scanner.SHADOW_BATCH_SIZES}\n"
        f"  blacklist: {bl_count} series | "
        f"registry: {approved} approved / {len(all_reg) - approved} drafted\n"
        f"  min net clip: {scanner.MIN_NET_CLIP_CENTS}¢/ct")

    # 8. Launch threads
    threads = [
        SupervisedThread("dw-scanner", _scanner_loop, (client,)),
        SupervisedThread("dw-settle", _settlement_loop, (client,)),
        SupervisedThread("dw-pack", _pack_loop),
        SupervisedThread("dw-tg", _tg_loop),
    ]
    tg.init()
    for t in threads:
        t.start()
    log.info("[MAIN] All threads launched")

    # 9. Supervisor loop
    while _running:
        time.sleep(30)
        for t in threads:
            if not t.check_and_restart():
                msg = f"FATAL: thread {t.name} exceeded {t.max_failures} failures"
                log.error(f"[MAIN] {msg}")
                notify.alert(f"🅳 {msg}")
                _running = False
                break

    log.warning("[MAIN] Shutting down...")
    notify.send("🅳 <b>KAL-D STOPPED</b>")
    log.warning("[MAIN] Done.")


if __name__ == "__main__":
    main()

"""k_worker main entry point.

Boot sequence:
1. envcheck — validate all config
2. notify — init Telegram
3. store — init SQLite
4. kalshi — build client, read balance
5. Main loop: discover market → engine cycle → repeat
6. Scoreboard on schedule (every 4 hours)
"""

import os
import sys
import time
import signal
import logging

from . import envcheck, notify, kalshi, store, gateway, discipline, engine, scoreboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("k_worker")

LOOP_SLEEP_SEC = 30
SCOREBOARD_INTERVAL_SEC = 4 * 3600
_running = True


def _shutdown(sig, frame):
    global _running
    log.warning(f"[MAIN] Received signal {sig} — shutting down")
    _running = False


def main():
    global _running
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 1. Environment check
    log.info("[MAIN] ═══ K-WORKER BOOT ═══")
    vals = envcheck.check_env()
    is_live = vals["_is_live"]
    mode = "LIVE" if is_live else "DEMO"
    log.warning(f"[MAIN] Mode: {mode}")

    # 2. Telegram
    try:
        notify.init()
    except Exception as e:
        log.error(f"[MAIN] Telegram init failed: {e}")
        if is_live:
            raise
        log.warning("[MAIN] Continuing without Telegram (demo mode)")

    # 3. Store
    store.init_db()

    # 4. Kalshi client
    client = kalshi.build_client()
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        raise RuntimeError("FATAL: Cannot read balance from Kalshi")

    total = cash + (pv or 0)
    log.warning(f"[MAIN] Balance: cash=${cash:.2f} positions=${pv or 0:.2f} total=${total:.2f}")

    notify.send(
        f"<b>K-WORKER STARTED</b>\n"
        f"Mode: {mode}\n"
        f"Balance: ${total:.2f} (cash=${cash:.2f})\n"
        f"Engine: KXBTC15M · flat 1ct · fav 95-99c · maker-only"
    )

    discipline.check_drawdown(cash)

    envcheck.check_clock_skew()

    last_scoreboard = 0
    last_ticker = None

    # 5. Main loop
    while _running:
        try:
            engine.heartbeat()

            if discipline.is_halted():
                log.warning(f"[MAIN] Engine halted: {discipline.halt_reason()}")
                time.sleep(LOOP_SLEEP_SEC)
                continue

            # Discover current market
            try:
                event_ticker, ticker, market_obj = kalshi.discover_market(client)
            except Exception as e:
                log.warning(f"[MAIN] Market discovery failed: {e}")
                time.sleep(LOOP_SLEEP_SEC)
                continue

            close_ts = kalshi.resolve_close_ts(market_obj, ticker)
            if close_ts is None:
                log.warning(f"[MAIN] No close_ts for {ticker}")
                time.sleep(LOOP_SLEEP_SEC)
                continue

            now = time.time()
            secs_to_expiry = close_ts - now

            # Skip if already expired
            if secs_to_expiry < 10:
                time.sleep(LOOP_SLEEP_SEC)
                continue

            # New market detected
            if ticker != last_ticker:
                log.info(f"[MAIN] Market: {ticker} closes in {secs_to_expiry:.0f}s")
                gateway.reset_for_new_market()
                last_ticker = ticker

            # Skip if already traded this ticker
            if gateway.is_traded(ticker):
                time.sleep(LOOP_SLEEP_SEC)
                continue

            # Check for existing positions
            try:
                pos = kalshi.position_for_market(client, ticker)
                if abs(pos) > 0:
                    gateway.mark_traded(ticker)
                    log.info(f"[MAIN] Already holding position on {ticker}")
                    time.sleep(LOOP_SLEEP_SEC)
                    continue
            except Exception:
                pass

            # Run engine cycle
            outcome = engine.run_market_cycle(client, ticker, close_ts, market_obj)
            if outcome:
                log.info(f"[MAIN] Cycle result for {ticker}: {outcome}")

            # Scoreboard
            if time.time() - last_scoreboard > SCOREBOARD_INTERVAL_SEC:
                try:
                    scoreboard.send_scoreboard()
                    last_scoreboard = time.time()
                except Exception as e:
                    log.warning(f"[MAIN] Scoreboard failed: {e}")

        except Exception as e:
            log.error(f"[MAIN] Loop error: {e}", exc_info=True)
            time.sleep(LOOP_SLEEP_SEC)
            continue

        time.sleep(LOOP_SLEEP_SEC)

    # Shutdown
    log.warning("[MAIN] Shutting down...")
    try:
        scoreboard.send_scoreboard()
    except Exception:
        pass
    notify.send("<b>K-WORKER STOPPED</b>")
    log.warning("[MAIN] Done.")


if __name__ == "__main__":
    main()

"""Chunk 6 — Engine thesis skeleton.

Final 180s window. Rest post-only limit at favorite's touch for 95-99c.
No repricing in 99c band. Max one reprice in 95-98c.
No chase, no scorer, no probability model — cost IS the signal.
Unfilled at T-10s -> cancel, log SKIP NO_FILL.
"""

import time
import logging
from typing import Optional, Tuple, Dict

from . import kalshi, store, gateway, discipline, notify

log = logging.getLogger("k_worker.engine")

ENTRY_WINDOW_SEC = 180
CANCEL_BEFORE_EXPIRY_SEC = 10
POLL_INTERVAL_SEC = 5
MAX_REPRICES_LOW_BAND = 1

HEARTBEAT_FILE = "/tmp/k_worker_heartbeat"
_heartbeat_ts: float = 0.0


def heartbeat():
    global _heartbeat_ts
    _heartbeat_ts = time.time()
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(_heartbeat_ts))
    except Exception:
        pass


def get_heartbeat_ts() -> float:
    return _heartbeat_ts


def run_market_cycle(client: kalshi.KalshiClient, ticker: str,
                     close_ts: int, market_obj: Dict) -> Optional[str]:
    """Run one full market cycle for a ticker. Returns outcome string or None.

    Phases:
    1. Wait until entry window (T-180s)
    2. Evaluate via gateway
    3. If allowed, submit and monitor fill
    4. Track settlement
    """
    heartbeat()

    now = time.time()
    secs_to_expiry = close_ts - now

    if discipline.is_halted():
        log.warning(f"[ENGINE] Halted — skipping {ticker} ({discipline.halt_reason()})")
        return "halted"

    # Phase 1: Wait for entry window
    entry_time = close_ts - ENTRY_WINDOW_SEC
    if now < entry_time:
        wait = entry_time - now
        if wait > 300:
            log.info(f"[ENGINE] {ticker} closes in {secs_to_expiry:.0f}s, entry window in {wait:.0f}s — too far out")
            return "too_early"
        log.info(f"[ENGINE] Waiting {wait:.0f}s for entry window on {ticker}")
        _wait_with_heartbeat(wait)

    heartbeat()
    now = time.time()
    secs_to_expiry = close_ts - now

    if secs_to_expiry < CANCEL_BEFORE_EXPIRY_SEC:
        log.info(f"[ENGINE] {ticker} too close to expiry ({secs_to_expiry:.0f}s)")
        return "expired"

    # Phase 2: Fetch book and evaluate
    book = kalshi.fetch_orderbook(client, ticker)
    cash, _ = kalshi.get_balance(client)
    if cash is None:
        log.warning(f"[ENGINE] Cannot read balance — skipping {ticker}")
        return "no_balance"

    discipline.check_drawdown(cash)
    if discipline.is_halted():
        return "halted"

    eval_result = gateway.evaluate(ticker, book, secs_to_expiry, cash)

    if not eval_result.allowed:
        # Log shadow row for out-of-band
        env = "live-observed"
        if eval_result.reject_code == "OUT_OF_BAND_COST" and eval_result.cost_cents is not None:
            if eval_result.cost_cents < 95:
                env = "live-observed"
        store.insert_row(store.SurfaceRow(
            market_ticker=ticker,
            decision_ts=time.time(),
            action="SKIP",
            seconds_to_expiry=secs_to_expiry,
            yes_quote_cents=eval_result.yes_quote_cents,
            side=eval_result.side,
            cost_per_contract_cents=eval_result.cost_cents,
            breakeven_pct=eval_result.breakeven_pct,
            skip_reason=eval_result.reject_code,
            env=env,
        ))
        log.info(f"[ENGINE] SKIP {ticker}: {eval_result.reject_code} — {eval_result.reject_reason}")
        return f"skip:{eval_result.reject_code}"

    # Phase 3: Submit via gateway
    order_id, row_id = gateway.submit(
        client, ticker, eval_result, close_ts, secs_to_expiry, book,
    )

    if order_id is None:
        log.info(f"[ENGINE] Submit failed for {ticker}")
        return "submit_failed"

    # Phase 4: Monitor fill
    outcome = _monitor_order(client, ticker, order_id, row_id, eval_result,
                             close_ts, book)
    heartbeat()
    return outcome


def _wait_with_heartbeat(seconds: float):
    """Wait while sending heartbeats."""
    end = time.time() + seconds
    while time.time() < end:
        heartbeat()
        remaining = end - time.time()
        time.sleep(min(30, max(0.1, remaining)))


def _monitor_order(client: kalshi.KalshiClient, ticker: str,
                   order_id: str, row_id: int,
                   eval_result: gateway.EvalResult,
                   close_ts: int, original_book: kalshi.Book) -> str:
    """Monitor an open order: wait for fill, handle repricing, cancel at T-10s."""
    reprices = 0
    cost_cents = eval_result.cost_cents
    is_99_band = cost_cents == 99

    while True:
        heartbeat()
        now = time.time()
        secs_left = close_ts - now

        if secs_left <= CANCEL_BEFORE_EXPIRY_SEC:
            # T-10s: cancel unfilled
            kalshi.cancel_all_for_market(client, ticker)
            store.update_settlement(row_id, "no_fill", 0.0, time.time())
            log.info(f"[ENGINE] NO_FILL {ticker} — cancelled at T-{CANCEL_BEFORE_EXPIRY_SEC}s")
            return "no_fill"

        # Check order status
        order = kalshi.get_order(client, order_id)
        if order is None:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        status = (order.get("status") or "").lower()

        if status in ("executed", "filled"):
            return _handle_fill(client, ticker, order, row_id, eval_result, close_ts)

        if status in ("canceled", "cancelled"):
            store.update_settlement(row_id, "cancelled_external", 0.0, time.time())
            log.warning(f"[ENGINE] Order {order_id} cancelled externally")
            return "cancelled_external"

        # Still resting — check if reprice needed (95-98c bands only)
        if not is_99_band and reprices < MAX_REPRICES_LOW_BAND:
            new_book = kalshi.fetch_orderbook(client, ticker)
            new_side, new_cost, new_yes_quote = gateway._favorite_side(new_book)
            if (new_side == eval_result.side and new_cost is not None
                    and new_cost != cost_cents
                    and gateway.COST_BAND_LO <= new_cost <= gateway.COST_BAND_HI):
                # Reprice: cancel old, place new
                kalshi.cancel_order(client, order_id)
                rest_price = new_book.yes_bid if eval_result.side == "yes" else new_book.no_bid
                if rest_price is not None and rest_price >= gateway.COST_BAND_LO:
                    try:
                        expiry_ts = close_ts - CANCEL_BEFORE_EXPIRY_SEC
                        new_oid = kalshi.place_order_maker(
                            client, ticker, eval_result.side,
                            rest_price, count=1, expiration_ts=expiry_ts,
                        )
                        order_id = new_oid
                        cost_cents = new_cost
                        reprices += 1
                        log.info(f"[ENGINE] REPRICE #{reprices} {ticker} → {cost_cents}c rest@{rest_price}c")
                    except Exception as e:
                        log.warning(f"[ENGINE] Reprice failed: {e}")
                        # Original order already cancelled
                        store.update_settlement(row_id, "reprice_failed", 0.0, time.time())
                        return "reprice_failed"

        time.sleep(POLL_INTERVAL_SEC)


def _handle_fill(client: kalshi.KalshiClient, ticker: str,
                 order: Dict, row_id: int,
                 eval_result: gateway.EvalResult,
                 close_ts: int) -> str:
    """Handle a filled order: record fill, wait for settlement."""
    fill_price = order.get("avg_price") or order.get("yes_price") or order.get("no_price")
    fee_cents = order.get("taker_fee", 0) + order.get("maker_fee", 0)
    if isinstance(fill_price, (int, float)):
        fill_price = int(fill_price)
    else:
        fill_price = eval_result.cost_cents

    slippage = fill_price - eval_result.cost_cents if eval_result.cost_cents else 0

    store.update_fill(row_id, fill_price, time.time(), slippage, fee_cents)
    log.info(f"[ENGINE] FILLED {ticker} {eval_result.side} @ {fill_price}c fee={fee_cents}c")

    notify.send(
        f"<b>FILL</b> {ticker}\n"
        f"{eval_result.side.upper()} @ {fill_price}c | fee={fee_cents}c"
    )

    # Post-fill drift check (10s after fill)
    time.sleep(min(10, max(0, close_ts - time.time() - 5)))
    heartbeat()
    try:
        drift_book = kalshi.fetch_orderbook(client, ticker)
        if eval_result.side == "yes" and drift_book.yes_bid is not None:
            drift = drift_book.yes_bid - fill_price
        elif eval_result.side == "no" and drift_book.no_bid is not None:
            drift = drift_book.no_bid - fill_price
        else:
            drift = 0
        store.update_drift(row_id, drift)
    except Exception:
        pass

    # Wait for settlement
    return _wait_for_settlement(client, ticker, row_id, eval_result, fill_price,
                                fee_cents, close_ts)


def _wait_for_settlement(client: kalshi.KalshiClient, ticker: str,
                         row_id: int, eval_result: gateway.EvalResult,
                         fill_price: int, fee_cents: int,
                         close_ts: int) -> str:
    """Wait for market settlement after fill."""
    # Wait until after close
    wait_until = close_ts + 30
    while time.time() < wait_until:
        heartbeat()
        time.sleep(min(15, max(1, wait_until - time.time())))

    # Poll for settlement (up to 5 minutes)
    deadline = time.time() + 300
    while time.time() < deadline:
        heartbeat()
        result = kalshi.get_settlement_result(client, ticker)
        if result is not None:
            won = (result == eval_result.side)
            if won:
                payout_cents = 100
                pnl = (payout_cents - fill_price - fee_cents) / 100.0
                resolution = "win"
                discipline.record_win()
            else:
                pnl = -(fill_price + fee_cents) / 100.0
                resolution = "loss"
                discipline.record_loss()

            store.update_settlement(row_id, resolution, pnl, time.time())
            emoji = "+" if won else "-"
            log.warning(
                f"[ENGINE] SETTLED {ticker} → {result.upper()} "
                f"{'WIN' if won else 'LOSS'} pnl=${pnl:+.2f}"
            )
            notify.send(
                f"<b>{'WIN' if won else 'LOSS'}</b> {ticker}\n"
                f"Result: {result.upper()} | PnL: ${pnl:+.2f}"
            )
            return resolution

        time.sleep(15)

    log.warning(f"[ENGINE] Settlement timeout for {ticker}")
    store.update_settlement(row_id, "timeout", 0.0, time.time())
    return "timeout"

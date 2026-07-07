"""Chunk 6 — Engine thesis skeleton.

Final 180s window. Rest post-only limit at favorite's touch for 95-99c.
No repricing in 99c band. Max one reprice in 95-98c.
No chase, no scorer, no probability model — cost IS the signal.
Unfilled at T-10s -> cancel, log SKIP NO_FILL.
"""

import os
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict

import requests as _requests

from . import kalshi, store, gateway, discipline, notify, treasury

log = logging.getLogger("k_worker.engine")

ENTRY_WINDOW_SEC = 180
CANCEL_BEFORE_EXPIRY_SEC = 10
POLL_INTERVAL_SEC = 5
MAX_REPRICES_LOW_BAND = 1
MAX_SUBMIT_ATTEMPTS = 2
TELEGRAM_PER_MARKET = os.environ.get("TELEGRAM_PER_MARKET", "1").strip() == "1"

_observe_mode = False
_submit_attempts: Dict[str, int] = {}
_skip_logged: set = set()


def set_observe_mode(enabled: bool) -> None:
    global _observe_mode
    _observe_mode = enabled

HEARTBEAT_FILE = "/tmp/k_worker_heartbeat"
HEARTBEAT_PING_URL = os.environ.get("HEARTBEAT_PING_URL", "").strip()
_PING_THROTTLE_SEC = 60
_heartbeat_ts: float = 0.0
_last_ping_ts: float = 0.0


def heartbeat():
    global _heartbeat_ts, _last_ping_ts
    _heartbeat_ts = time.time()
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(_heartbeat_ts))
    except Exception:
        pass
    if HEARTBEAT_PING_URL and (_heartbeat_ts - _last_ping_ts) >= _PING_THROTTLE_SEC:
        _last_ping_ts = _heartbeat_ts
        threading.Thread(target=_ping, daemon=True).start()


def _ping():
    try:
        _requests.post(HEARTBEAT_PING_URL, timeout=2)
    except Exception as e:
        log.warning(f"[HEARTBEAT] Ping failed: {e}")


def get_heartbeat_ts() -> float:
    return _heartbeat_ts


# ── T3: per-market Telegram ─────────────────────────────────────

def _time_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M")


def _bal_str(client: kalshi.KalshiClient) -> str:
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        return "Bal ?"
    return f"Bal ${(cash + (pv or 0)):.2f}"


def _bal_from(cash, pv=0) -> str:
    if cash is None:
        return "Bal ?"
    return f"Bal ${(cash + (pv or 0)):.2f}"


def _send_market_line(text: str) -> None:
    if TELEGRAM_PER_MARKET:
        notify.send(text)


# ── Main cycle ──────────────────────────────────────────────────

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
        _send_market_line(f"⏭ {_time_et()} SKIP_HALTED | Bal ?")
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
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        log.warning(f"[ENGINE] Cannot read balance — skipping {ticker}")
        _send_market_line(f"⏭ {_time_et()} SKIP_BALANCE (unreadable) | Bal ?")
        return "no_balance"

    if not _observe_mode:
        discipline.check_drawdown(cash)
        if discipline.is_halted():
            return "halted"

    eval_result = gateway.evaluate(ticker, book, secs_to_expiry, cash)

    spread = None
    if eval_result.side == "yes" and book.yes_bid is not None and book.yes_ask is not None:
        spread = book.yes_ask - book.yes_bid
    elif eval_result.side == "no" and book.no_bid is not None and book.no_ask is not None:
        spread = book.no_ask - book.no_bid

    if not eval_result.allowed:
        if ticker not in _skip_logged:
            store.insert_row(store.SurfaceRow(
                market_ticker=ticker,
                decision_ts=time.time(),
                action="SKIP",
                seconds_to_expiry=secs_to_expiry,
                yes_quote_cents=eval_result.yes_quote_cents,
                side=eval_result.side,
                cost_per_contract_cents=eval_result.cost_cents,
                breakeven_pct=eval_result.breakeven_pct,
                yes_ask_cents=book.yes_ask,
                no_ask_cents=book.no_ask,
                spread_cents=spread,
                skip_reason=eval_result.reject_code,
                why_tag=eval_result.why_tag,
                env="live-observed",
            ))
            _skip_logged.add(ticker)
        log.info(f"[ENGINE] SKIP {ticker}: {eval_result.reject_code} — {eval_result.reject_reason}")
        side_str = eval_result.side.upper() if eval_result.side else "?"
        cost_str = f"{eval_result.cost_cents}¢" if eval_result.cost_cents is not None else "?¢"
        tag = eval_result.why_tag or eval_result.reject_code
        bal = _bal_from(cash, pv)
        _send_market_line(f"⏭ {_time_et()} {tag} (fav {side_str} {cost_str} @T-{int(secs_to_expiry)}) | {bal}")
        return f"skip:{eval_result.reject_code}"

    # Observe mode: log what would be an ENTER but never submit
    if _observe_mode:
        if ticker not in _skip_logged:
            store.insert_row(store.SurfaceRow(
                market_ticker=ticker,
                decision_ts=time.time(),
                action="SKIP",
                seconds_to_expiry=secs_to_expiry,
                yes_quote_cents=eval_result.yes_quote_cents,
                side=eval_result.side,
                cost_per_contract_cents=eval_result.cost_cents,
                breakeven_pct=eval_result.breakeven_pct,
                yes_ask_cents=book.yes_ask,
                no_ask_cents=book.no_ask,
                spread_cents=spread,
                skip_reason="OBSERVE_MODE",
                why_tag=eval_result.why_tag,
                env="live-observed",
            ))
            _skip_logged.add(ticker)
        log.info(f"[ENGINE] OBSERVE {ticker}: would ENTER {eval_result.side} @ {eval_result.cost_cents}c")
        side_str = eval_result.side.upper() if eval_result.side else "?"
        bal = _bal_from(cash, pv)
        _send_market_line(
            f"\U0001f441 {_time_et()} OBSERVE {eval_result.why_tag} "
            f"(fav {side_str} {eval_result.cost_cents}¢ @T-{int(secs_to_expiry)}) | {bal}"
        )
        return "observe"

    # Phase 3: Submit via gateway (with retry discipline)
    attempt = _submit_attempts.get(ticker, 0) + 1
    _submit_attempts[ticker] = attempt

    if attempt > MAX_SUBMIT_ATTEMPTS:
        log.warning(f"[ENGINE] Max submit attempts ({MAX_SUBMIT_ATTEMPTS}) exhausted for {ticker}")
        _send_market_line(
            f"⏭ {_time_et()} SKIP_MAX_ATTEMPTS (fav {eval_result.side.upper()} "
            f"{eval_result.cost_cents}¢ @T-{int(secs_to_expiry)}) | {_bal_from(cash, pv)}"
        )
        return "max_attempts"

    order_id, row_id, skip_why = gateway.submit(
        client, ticker, eval_result, close_ts, secs_to_expiry, book,
    )

    if order_id is None:
        if skip_why == "SKIP_ORDER_AMBIGUOUS":
            time.sleep(2)
            pos = kalshi.position_for_market(client, ticker)
            if abs(pos) > 0:
                log.warning(f"[ENGINE] Ambiguous submit but exchange shows position — treating as filled")
                gateway.mark_traded(ticker)
                bal = _bal_str(client)
                _send_market_line(
                    f"⚠ {_time_et()} AMBIGUOUS_BUT_FILLED {eval_result.why_tag} | {bal}"
                )
                return "ambiguous_filled"
            if attempt < MAX_SUBMIT_ATTEMPTS:
                log.warning(f"[ENGINE] Ambiguous failure, no position — will retry next cycle")
                _submit_attempts[ticker] = attempt
                return "retry_pending"

        if skip_why == "SKIP_ORDER_REJECTED" and attempt < MAX_SUBMIT_ATTEMPTS:
            retry_tag = f"{eval_result.why_tag}_RETRY1"
            log.warning(f"[ENGINE] Definitive reject, attempt {attempt}/{MAX_SUBMIT_ATTEMPTS} — will retry")
            _send_market_line(
                f"⏭ {_time_et()} {retry_tag} (fav {eval_result.side.upper()} "
                f"{eval_result.cost_cents}¢ @T-{int(secs_to_expiry)}) | {_bal_from(cash, pv)}"
            )
            return "retry_pending"

        log.info(f"[ENGINE] Submit failed for {ticker}")
        tag = skip_why or "SKIP_SUBMIT_FAILED"
        side_str = eval_result.side.upper() if eval_result.side else "?"
        bal = _bal_str(client)
        _send_market_line(
            f"⏭ {_time_et()} {tag} (fav {side_str} {eval_result.cost_cents}¢ @T-{int(secs_to_expiry)}) | {bal}"
        )
        return "submit_failed"

    # Phase 4: Monitor fill -> settlement
    result = _monitor_order(client, ticker, order_id, row_id, eval_result,
                            close_ts, book)

    outcome = result["outcome"]
    bal = _bal_str(client)
    ts = _time_et()
    tag = eval_result.why_tag

    if outcome == "no_fill":
        _send_market_line(f"\U0001f7e1 {ts} NO_FILL {tag} rested {eval_result.cost_cents}¢, uncrossed | {bal}")
    elif outcome in ("win", "loss"):
        emoji = "✅" if outcome == "win" else "❌"
        label = "WIN" if outcome == "win" else "LOSS"
        fc = result.get("fill_cost", eval_result.cost_cents)
        fee = result.get("fee_cents", 0)
        pnl = result.get("pnl", 0.0)
        line = f"{emoji} {ts} FILL {tag} @{fc}¢ fee {fee}¢ → {label} ${pnl:+.2f} | {bal}"
        if result.get("treasury_line"):
            line += f"\n{result['treasury_line']}"
        _send_market_line(line)
    elif outcome == "timeout":
        fc = result.get("fill_cost", eval_result.cost_cents)
        fee = result.get("fee_cents", 0)
        _send_market_line(f"\U0001f7e1 {ts} FILL {tag} @{fc}¢ fee {fee}¢ → TIMEOUT | {bal}")
    elif outcome == "cancelled_external":
        _send_market_line(f"⏭ {ts} CANCELLED {tag} | {bal}")
    elif outcome == "reprice_failed":
        _send_market_line(f"⏭ {ts} REPRICE_FAIL {tag} | {bal}")

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
                   close_ts: int, original_book: kalshi.Book) -> Dict:
    """Monitor an open order via LIST + FILLS. Returns dict with outcome and fill details."""
    from decimal import Decimal
    reprices = 0
    cost_cents = eval_result.cost_cents
    is_99_band = cost_cents == 99

    while True:
        heartbeat()
        now = time.time()
        secs_left = close_ts - now

        if secs_left <= CANCEL_BEFORE_EXPIRY_SEC:
            kalshi.cancel_all_for_market(client, ticker)
            store.update_settlement(row_id, "no_fill", 0.0, time.time())
            log.info(f"[ENGINE] NO_FILL {ticker} — cancelled at T-{CANCEL_BEFORE_EXPIRY_SEC}s")
            return {"outcome": "no_fill"}

        status_result = kalshi.order_status(client, ticker, order_id)
        state = status_result["state"]

        if state == "filled":
            return _handle_fill(client, ticker, status_result["raw"], row_id,
                                eval_result, close_ts)

        if state == "gone":
            label = "no_fill" if secs_left <= 20 else "cancelled_external"
            store.update_settlement(row_id, label, 0.0, time.time())
            log.warning(f"[ENGINE] Order {order_id} {label} (T-{secs_left:.0f}s)")
            return {"outcome": label}

        # state == "resting" — check for partial fill edge case
        raw = status_result.get("raw") or {}
        fc_str = raw.get("fill_count", "0") or "0"
        if Decimal(fc_str) > 0:
            notify.alert(f"Partial fill at 1ct?! fill_count={fc_str} on resting order {raw}")

        # Reprice check (not in 99c band, max 1 reprice)
        if not is_99_band and reprices < MAX_REPRICES_LOW_BAND:
            new_book = kalshi.fetch_orderbook(client, ticker)
            new_side, new_cost, _ = gateway._favorite_side(new_book)
            if (new_side == eval_result.side and new_cost is not None
                    and new_cost != cost_cents
                    and gateway.COST_BAND_LO <= new_cost <= gateway.COST_BAND_HI):
                try:
                    new_oid, new_cost_out = gateway.reprice(
                        client, ticker, eval_result, order_id, new_book, close_ts,
                    )
                except Exception as e:
                    log.warning(f"[ENGINE] Reprice failed: {e}")
                    store.update_settlement(row_id, "reprice_failed", 0.0, time.time())
                    return {"outcome": "reprice_failed"}
                if new_oid is not None:
                    order_id = new_oid
                    cost_cents = new_cost_out
                    reprices += 1
                    log.info(f"[ENGINE] REPRICE #{reprices} {ticker} → {cost_cents}c")

        time.sleep(POLL_INTERVAL_SEC)


def _handle_fill(client: kalshi.KalshiClient, ticker: str,
                 fill_records: list, row_id: int,
                 eval_result: gateway.EvalResult,
                 close_ts: int) -> Dict:
    """Handle a filled order. fill_records: list of fill dicts from /portfolio/fills."""
    from decimal import Decimal

    fill_cost = None
    fee_cents = 0

    if fill_records:
        fr = fill_records[0]
        for key in ("yes_price", "price"):
            raw = fr.get(key)
            if raw is None:
                continue
            try:
                val = Decimal(str(raw))
                yes_cents = int(val * 100) if val < 1 else int(val)
                fill_cost = yes_cents if eval_result.side == "yes" else (100 - yes_cents)
                break
            except Exception:
                continue

        if fill_cost is None and fr.get("no_price") is not None:
            try:
                val = Decimal(str(fr["no_price"]))
                no_cents = int(val * 100) if val < 1 else int(val)
                fill_cost = no_cents if eval_result.side == "no" else (100 - no_cents)
            except Exception:
                pass

        for fee_key in ("fee", "taker_fee", "maker_fee"):
            raw = fr.get(fee_key)
            if raw is not None:
                try:
                    val = Decimal(str(raw))
                    fee_cents = int(val * 100) if val < 1 else int(val)
                except Exception:
                    pass
                break

    if fill_cost is None:
        fill_cost = eval_result.cost_cents
        notify.alert(f"Fill on {ticker} — price missing from fill records, using eval cost {fill_cost}c")

    if not (gateway.COST_BAND_LO <= fill_cost <= gateway.COST_BAND_HI):
        log.error(f"[ENGINE] PHRASING-LAW ASSERT: fill_cost={fill_cost}c outside 95-99 for {ticker} "
                  f"(fill keys={list(fill_records[0].keys()) if fill_records else []} "
                  f"side={eval_result.side}) — using eval cost, flagging row")
        notify.alert(f"Phrasing-law assert on fill {ticker}: {fill_cost}c — check fill records")
        fill_cost = eval_result.cost_cents

    slippage = fill_cost - eval_result.cost_cents if eval_result.cost_cents else 0

    store.update_fill(row_id, fill_cost, time.time(), slippage, fee_cents)
    log.info(f"[ENGINE] FILLED {ticker} {eval_result.side} @ {fill_cost}c fee={fee_cents}c")

    # Post-fill drift check (10s after fill)
    time.sleep(min(10, max(0, close_ts - time.time() - 5)))
    heartbeat()
    try:
        drift_book = kalshi.fetch_orderbook(client, ticker)
        if eval_result.side == "yes" and drift_book.yes_bid is not None:
            drift = drift_book.yes_bid - fill_cost
        elif eval_result.side == "no" and drift_book.no_bid is not None:
            drift = drift_book.no_bid - fill_cost
        else:
            drift = 0
        store.update_drift(row_id, drift)
    except Exception:
        pass

    # Wait for settlement
    settle = _wait_for_settlement(client, ticker, row_id, eval_result, fill_cost,
                                   fee_cents, close_ts)
    settle["fill_cost"] = fill_cost
    settle["fee_cents"] = fee_cents
    return settle


def _wait_for_settlement(client: kalshi.KalshiClient, ticker: str,
                         row_id: int, eval_result: gateway.EvalResult,
                         fill_cost: int, fee_cents: int,
                         close_ts: int) -> Dict:
    """Wait for market settlement after fill. Returns dict with outcome and pnl."""
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
                pnl = (payout_cents - fill_cost - fee_cents) / 100.0
                resolution = "win"
                discipline.record_win()
                split = treasury.waterfall(pnl)
            else:
                pnl = -(fill_cost + fee_cents) / 100.0
                resolution = "loss"
                discipline.record_loss()
                treasury.record_loss(pnl)
                split = None

            store.update_settlement(row_id, resolution, pnl, time.time())
            log.warning(
                f"[ENGINE] SETTLED {ticker} → {result.upper()} "
                f"{'WIN' if won else 'LOSS'} pnl=${pnl:+.2f}"
            )
            ret = {"outcome": resolution, "pnl": pnl}
            if split:
                ret["treasury_line"] = split["treasury_line"]
            return ret

        time.sleep(15)

    log.warning(f"[ENGINE] Settlement timeout for {ticker}")
    store.update_settlement(row_id, "timeout", 0.0, time.time())
    return {"outcome": "timeout", "pnl": 0.0}

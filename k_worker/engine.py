"""Chunk 6 — Engine thesis skeleton.

Final 180s window. Rest post-only limit at favorite's touch for 95-99c.
No repricing in 99c band. Max one reprice in 95-98c.
No chase, no scorer, no probability model — cost IS the signal.
Unfilled at T-10s -> cancel, log SKIP NO_FILL.
H8 probe lane: cost 80-94c, distance_pct >= 0.15%, secs <= 60.
"""

import os
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Optional, Tuple, Dict

import requests as _requests

from . import kalshi, store, gateway, discipline, notify, treasury, delta_table_loader
from .sessions import SESSION_WINDOWS, session_tag as _session_tag_fn

log = logging.getLogger("k_worker.engine")

ENTRY_WINDOW_SEC = 180
WATCH_WINDOW_SEC = 900
CANCEL_BEFORE_EXPIRY_SEC = 10
POLL_INTERVAL_SEC = 5
MAX_REPRICES_LOW_BAND = 1
MAX_SUBMIT_ATTEMPTS = 2
TELEGRAM_PER_MARKET = os.environ.get("TELEGRAM_PER_MARKET", "1").strip() == "1"

T_BANDS = [(900, 600), (600, 300), (300, 180), (180, 120), (120, 60), (60, 10)]

CONFIRM_LADDER = [
    {"lo_sec": 600, "hi_sec": 900, "floor_cents": 99, "confirms": 9},
    {"lo_sec": 300, "hi_sec": 600, "floor_cents": 98, "confirms": 6},
    {"lo_sec": 180, "hi_sec": 300, "floor_cents": 97, "confirms": 3},
]

_observe_mode = False
_submit_attempts: Dict[str, int] = {}
_skip_logged: set = set()  # cell keys: (ticker, cost_band, time_band_tuple)

_spot_history: list = []  # (timestamp, price) for vol_regime


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


# ── Context tags (Fix 5) ──────────────────────────────────────

def _current_session() -> str:
    return _session_tag_fn(time.time())


def _record_spot(price: float) -> None:
    now = time.time()
    _spot_history.append((now, price))
    cutoff = now - 1200
    _spot_history[:] = [(t, p) for t, p in _spot_history if t >= cutoff]


def _vol_regime() -> str:
    """|delta_spot| over prior 15m in bps -> LOW/MED/HIGH."""
    if len(_spot_history) < 2:
        return "UNKNOWN"
    now = time.time()
    target = now - 900
    oldest = min(_spot_history, key=lambda x: abs(x[0] - target))
    newest = _spot_history[-1]
    if oldest[1] == 0:
        return "UNKNOWN"
    delta_bps = abs(newest[1] - oldest[1]) / oldest[1] * 10000
    if delta_bps < 8:
        return "LOW"
    elif delta_bps <= 25:
        return "MED"
    return "HIGH"


# ── Cell-key throttle (Fix 2) ─────────────────────────────────

def _time_band(secs_to_expiry: float) -> Optional[Tuple[int, int]]:
    for hi, lo in T_BANDS:
        if lo <= secs_to_expiry < hi:
            return (hi, lo)
    return None


def _cell_key(ticker: str, cost_exact: Optional[float], secs_to_expiry: float) -> tuple:
    cost_band = int(cost_exact) if cost_exact is not None else None
    tb = _time_band(secs_to_expiry)
    return (ticker, cost_band, tb)


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
    """Run one full market cycle for a ticker."""
    heartbeat()

    now = time.time()
    secs_to_expiry = close_ts - now

    if discipline.is_halted():
        reason = discipline.halt_reason()
        store.insert_row(store.SurfaceRow(
            market_ticker=ticker, decision_ts=time.time(), action="SKIP",
            seconds_to_expiry=secs_to_expiry,
            skip_reason=f"HALTED:{reason}", why_tag=f"SKIP_HALTED_{reason}",
            env="live-observed",
        ))
        log.warning(f"[ENGINE] Halted — skipping {ticker} ({reason})")
        _send_market_line(f"⏭ {_time_et()} SKIP_HALTED | Bal ?")
        return "halted"

    # Phase 1: Watch-confirm ladder (T-900 to T-180) then final window (T-180)
    watch_start = close_ts - WATCH_WINDOW_SEC
    entry_time = close_ts - ENTRY_WINDOW_SEC
    if now < watch_start:
        wait = watch_start - now
        if wait > 300:
            log.info(f"[ENGINE] {ticker} closes in {secs_to_expiry:.0f}s, watch window in {wait:.0f}s — too far out")
            return "too_early"
        log.info(f"[ENGINE] Waiting {wait:.0f}s for watch window on {ticker}")
        _wait_with_heartbeat(wait)

    heartbeat()
    now = time.time()
    secs_to_expiry = close_ts - now

    if secs_to_expiry < CANCEL_BEFORE_EXPIRY_SEC:
        store.insert_row(store.SurfaceRow(
            market_ticker=ticker, decision_ts=time.time(), action="SKIP",
            seconds_to_expiry=secs_to_expiry,
            skip_reason="EXPIRED_BEFORE_EVAL", why_tag="MISSED_EXPIRED",
            env="live-observed",
        ))
        log.info(f"[ENGINE] {ticker} too close to expiry ({secs_to_expiry:.0f}s)")
        return "expired"

    # Watch-confirm ladder: try each tier before the final window
    ladder_result = None
    if secs_to_expiry > ENTRY_WINDOW_SEC:
        ladder_result = _run_watch_ladder(client, ticker, close_ts, market_obj)
        if ladder_result is not None:
            # Ladder passed — proceed directly to submit with this eval
            eval_result, book, cash, pv, spot, session_tag, vol_regime = ladder_result
            spread = _compute_spread(eval_result, book)
            boundary_lo = eval_result.boundary_lo
            boundary_hi = eval_result.boundary_hi
        else:
            # No tier passed — fall through to final window
            heartbeat()
            now = time.time()
            secs_to_expiry = close_ts - now
            if secs_to_expiry < CANCEL_BEFORE_EXPIRY_SEC:
                return "expired"

    if ladder_result is None:
        # Phase 2: Final window — existing single-evaluation behavior
        if now < entry_time:
            wait = entry_time - now
            if wait > 0:
                _wait_with_heartbeat(wait)
            heartbeat()
            now = time.time()
            secs_to_expiry = close_ts - now

        book = kalshi.fetch_orderbook(client, ticker)
        cash, pv = kalshi.get_balance(client)
        if cash is None:
            store.insert_row(store.SurfaceRow(
                market_ticker=ticker, decision_ts=time.time(), action="SKIP",
                seconds_to_expiry=secs_to_expiry,
                skip_reason="BALANCE_UNREADABLE", why_tag="SKIP_BALANCE_UNREADABLE",
                env="live-observed",
            ))
            log.warning(f"[ENGINE] Cannot read balance — skipping {ticker}")
            _send_market_line(f"⏭ {_time_et()} SKIP_BALANCE (unreadable) | Bal ?")
            return "no_balance"

        if not _observe_mode:
            # P2 (0708): pass RAW cash — check_drawdown applies
            # tradeable_balance internally; passing tradeable deducted
            # owed-to-Drew twice and tripped the rail early.
            discipline.check_drawdown(cash)
            if discipline.is_halted():
                return "halted"

        spot = kalshi.get_btc_spot()
        if spot is not None:
            _record_spot(spot)
        boundary_lo, boundary_hi = kalshi.extract_boundaries(market_obj)

        session_tag = _current_session()
        vol_regime = _vol_regime()

        eval_result = gateway.evaluate(ticker, book, secs_to_expiry, cash,
                                        spot=spot, boundary_lo=boundary_lo,
                                        boundary_hi=boundary_hi)

        spread = _compute_spread(eval_result, book)

    if not eval_result.allowed:
        ck = _cell_key(ticker, eval_result.cost_exact, secs_to_expiry)
        if ck not in _skip_logged:
            store.insert_row(store.SurfaceRow(
                market_ticker=ticker,
                decision_ts=time.time(),
                action="SKIP",
                seconds_to_expiry=secs_to_expiry,
                yes_quote_cents=eval_result.yes_quote_cents,
                side=eval_result.side,
                cost_per_contract_cents=eval_result.cost_exact or eval_result.cost_cents,
                breakeven_pct=eval_result.breakeven_pct,
                yes_ask_cents=book.yes_ask,
                no_ask_cents=book.no_ask,
                spread_cents=spread,
                skip_reason=eval_result.reject_code,
                why_tag=eval_result.why_tag,
                lane=eval_result.lane,
                spot_price=spot,
                boundary_lo=boundary_lo,
                boundary_hi=boundary_hi,
                distance=eval_result.distance,
                distance_pct=eval_result.distance_pct,
                session_tag=session_tag,
                vol_regime=vol_regime,
                env="live-observed",
            ))
            _skip_logged.add(ck)
        log.info(f"[ENGINE] SKIP {ticker}: {eval_result.reject_code} — {eval_result.reject_reason}")
        side_str = eval_result.side.upper() if eval_result.side else "?"
        cost_str = f"{eval_result.cost_exact or eval_result.cost_cents}¢" if eval_result.cost_exact or eval_result.cost_cents else "?¢"
        tag = eval_result.why_tag or eval_result.reject_code
        bal = _bal_from(cash, pv)
        prefix = "\U0001f9ea " if eval_result.lane == "H8" else "⏭ "
        _send_market_line(f"{prefix}{_time_et()} {tag} (fav {side_str} {cost_str} @T-{int(secs_to_expiry)}) | {bal}")
        return f"skip:{eval_result.reject_code}"

    # Observe mode: log what would be an ENTER
    if _observe_mode:
        ck = _cell_key(ticker, eval_result.cost_exact, secs_to_expiry)
        if ck not in _skip_logged:
            store.insert_row(store.SurfaceRow(
                market_ticker=ticker,
                decision_ts=time.time(),
                action="SKIP",
                seconds_to_expiry=secs_to_expiry,
                yes_quote_cents=eval_result.yes_quote_cents,
                side=eval_result.side,
                cost_per_contract_cents=eval_result.cost_exact or eval_result.cost_cents,
                breakeven_pct=eval_result.breakeven_pct,
                yes_ask_cents=book.yes_ask,
                no_ask_cents=book.no_ask,
                spread_cents=spread,
                skip_reason="OBSERVE_MODE",
                why_tag=eval_result.why_tag,
                lane=eval_result.lane,
                spot_price=spot,
                boundary_lo=boundary_lo,
                boundary_hi=boundary_hi,
                distance=eval_result.distance,
                distance_pct=eval_result.distance_pct,
                session_tag=session_tag,
                vol_regime=vol_regime,
                env="live-observed",
            ))
            _skip_logged.add(ck)
        log.info(f"[ENGINE] OBSERVE {ticker}: would ENTER {eval_result.side} @ {eval_result.cost_exact}c")
        side_str = eval_result.side.upper() if eval_result.side else "?"
        bal = _bal_from(cash, pv)
        prefix = "\U0001f9ea " if eval_result.lane == "H8" else "\U0001f441 "
        _send_market_line(
            f"{prefix}{_time_et()} OBSERVE {eval_result.why_tag} "
            f"(fav {side_str} {eval_result.cost_exact}¢ @T-{int(secs_to_expiry)}) | {bal}"
        )
        return "observe"

    # Phase 3: Submit via gateway
    attempt = _submit_attempts.get(ticker, 0) + 1
    _submit_attempts[ticker] = attempt

    if attempt > MAX_SUBMIT_ATTEMPTS:
        log.warning(f"[ENGINE] Max submit attempts ({MAX_SUBMIT_ATTEMPTS}) exhausted for {ticker}")
        _send_market_line(
            f"⏭ {_time_et()} SKIP_MAX_ATTEMPTS (fav {eval_result.side.upper()} "
            f"{eval_result.cost_exact}¢ @T-{int(secs_to_expiry)}) | {_bal_from(cash, pv)}"
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
                f"{eval_result.cost_exact}¢ @T-{int(secs_to_expiry)}) | {_bal_from(cash, pv)}"
            )
            return "retry_pending"

        log.info(f"[ENGINE] Submit failed for {ticker}")
        tag = skip_why or "SKIP_SUBMIT_FAILED"
        side_str = eval_result.side.upper() if eval_result.side else "?"
        bal = _bal_str(client)
        _send_market_line(
            f"⏭ {_time_et()} {tag} (fav {side_str} {eval_result.cost_exact}¢ @T-{int(secs_to_expiry)}) | {bal}"
        )
        return "submit_failed"

    # Update session/vol on the ENTER row
    store._conn.execute(
        "UPDATE surface SET session_tag=?, vol_regime=? WHERE id=?",
        (session_tag, vol_regime, row_id),
    )
    store._conn.commit()

    # P9 (0708): queue position at placement — instruments no-fill cause.
    try:
        qp = kalshi.get_order_queue_position(client, order_id)
        if qp is not None:
            store.update_queue_pos(row_id, "queue_pos_entry", qp)
            log.info(f"[ENGINE] Queue position at entry: {qp} ({ticker})")
    except Exception:
        pass

    # Phase 4: Monitor fill -> settlement
    order_placed_ts = time.time()
    result = _monitor_order(client, ticker, order_id, row_id, eval_result,
                            close_ts, book, order_placed_ts=order_placed_ts)

    outcome = result["outcome"]

    # P5 (0708): confirmed zero-fill releases lane-F hourly exposure.
    # blind_standdown deliberately NOT released (could be filled).
    if eval_result.lane == "F" and outcome in ("no_fill", "cancelled_external"):
        n_ct = max(1, getattr(eval_result, "contracts", 1))
        gateway.release_exposure((eval_result.cost_exact or eval_result.cost_cents) / 100.0 * n_ct)

    bal = _bal_str(client)
    ts = _time_et()
    tag = eval_result.why_tag
    prefix = "\U0001f9ea " if eval_result.lane == "H8" else ""

    if outcome == "no_fill":
        _send_market_line(f"{prefix}\U0001f7e1 {ts} NO_FILL {tag} rested {eval_result.cost_exact}¢, uncrossed | {bal}")
    elif outcome in ("win", "loss"):
        emoji = "✅" if outcome == "win" else "❌"
        label = "WIN" if outcome == "win" else "LOSS"
        fc = result.get("fill_cost", eval_result.cost_cents)
        fee = result.get("fee_cents", 0)
        pnl = result.get("pnl", 0.0)
        line = f"{prefix}{emoji} {ts} FILL {tag} @{fc}¢ fee {fee}¢ → {label} ${pnl:+.2f} | {bal}"
        if result.get("treasury_line"):
            line += f"\n{result['treasury_line']}"
        _send_market_line(line)
    elif outcome == "timeout":
        fc = result.get("fill_cost", eval_result.cost_cents)
        fee = result.get("fee_cents", 0)
        _send_market_line(f"{prefix}\U0001f7e1 {ts} FILL {tag} @{fc}¢ fee {fee}¢ → TIMEOUT | {bal}")
    elif outcome == "cancelled_external":
        _send_market_line(f"{prefix}⏭ {ts} CANCELLED {tag} | {bal}")
    elif outcome == "reprice_failed":
        _send_market_line(f"{prefix}⏭ {ts} REPRICE_FAIL {tag} | {bal}")
    elif outcome == "blind_standdown":
        _send_market_line(f"{prefix}⚠ {ts} BLIND_STANDDOWN {tag} | {bal}")

    heartbeat()
    return outcome


def _compute_spread(eval_result: gateway.EvalResult, book: kalshi.Book) -> Optional[int]:
    if eval_result.side == "yes" and book.yes_bid is not None and book.yes_ask is not None:
        return book.yes_ask - book.yes_bid
    elif eval_result.side == "no" and book.no_bid is not None and book.no_ask is not None:
        return book.no_ask - book.no_bid
    return None


def _run_watch_ladder(client: kalshi.KalshiClient, ticker: str,
                      close_ts: int, market_obj: Dict) -> Optional[tuple]:
    """Watch-confirm ladder (T-900 to T-180). Returns (eval_result, book, cash, pv,
    spot, session_tag, vol_regime) when a tier passes, or None if all tiers expire.
    R2: rejects if the counterparty side stays empty throughout a tier."""
    confirm_count = 0
    watch_side = None
    last_tier_idx = -1
    no_counterparty_ticks = 0

    while True:
        heartbeat()
        now = time.time()
        secs_left = close_ts - now

        if secs_left <= ENTRY_WINDOW_SEC:
            return None
        if secs_left < CANCEL_BEFORE_EXPIRY_SEC:
            return None

        # Determine current tier
        tier_idx = None
        tier = None
        for i, t in enumerate(CONFIRM_LADDER):
            if t["lo_sec"] <= secs_left < t["hi_sec"]:
                tier_idx = i
                tier = t
                break

        if tier is None:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # Reset on tier change
        if tier_idx != last_tier_idx:
            confirm_count = 0
            watch_side = None
            no_counterparty_ticks = 0
            last_tier_idx = tier_idx

        book = kalshi.fetch_orderbook(client, ticker)
        side, cost_d, yes_quote, fp_str = gateway._favorite_side(book)

        if side is None or cost_d is None:
            confirm_count = 0
            watch_side = None
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # R2: Counterparty liquidity check
        has_counterparty = True
        if side == "yes" and book.no_bid is None:
            has_counterparty = False
        elif side == "no" and book.yes_bid is None:
            has_counterparty = False
        if not has_counterparty:
            no_counterparty_ticks += 1
            confirm_count = 0
            time.sleep(POLL_INTERVAL_SEC)
            continue

        cost_int = int(cost_d)

        # Dropout: side flip resets counter
        if side != watch_side:
            confirm_count = 0
            watch_side = side

        # Dropout: below tier floor resets counter
        if cost_int < tier["floor_cents"]:
            confirm_count = 0
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # Must also be in the main band (95-99)
        if not (gateway.COST_BAND_LO_INT <= cost_int <= gateway.COST_BAND_HI_INT):
            confirm_count = 0
            time.sleep(POLL_INTERVAL_SEC)
            continue

        confirm_count += 1

        if confirm_count >= tier["confirms"]:
            # Tier passed — run full evaluate
            cash, pv = kalshi.get_balance(client)
            if cash is None:
                log.warning(f"[ENGINE] Watch ladder: balance unreadable at confirm")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            if not _observe_mode:
                # P2 (0708): raw cash — see run_market_cycle note.
                discipline.check_drawdown(cash)
                if discipline.is_halted():
                    return None

            spot = kalshi.get_btc_spot()
            if spot is not None:
                _record_spot(spot)
            boundary_lo, boundary_hi = kalshi.extract_boundaries(market_obj)

            eval_result = gateway.evaluate(ticker, book, secs_left, cash,
                                            spot=spot, boundary_lo=boundary_lo,
                                            boundary_hi=boundary_hi)

            if eval_result.allowed:
                # Top-rung guard (tier 0 = T-900-600 only) — P3 (0708):
                # upper-tail gate vs cost-implied breakeven; low-tail entries
                # trade and get TAGGED (ruling #3: tape decides adverse-selection).
                if tier_idx == 0 and delta_table_loader.is_loaded():
                    dist_usd = abs(eval_result.distance) if eval_result.distance is not None else 0
                    tv = delta_table_loader.f_top_rung_verdict(
                        dist_usd, secs_left,
                        cost_cents=eval_result.cost_exact or eval_result.cost_cents)
                    if tv["qualified"] is False:
                        log.info(f"[ENGINE] Top-rung guard: crossing risk over gate — "
                                 f"d=${dist_usd:.0f} wub={tv['wilson_ub']:.6f} "
                                 f"be={tv['breakeven_p']:.2%}")
                        store.insert_row(store.SurfaceRow(
                            market_ticker=ticker, decision_ts=time.time(),
                            action="SKIP", seconds_to_expiry=secs_left,
                            cost_per_contract_cents=cost_int,
                            skip_reason="TABLE_RISK_OVER_GATE",
                            why_tag=f"SKIP_TABLE_RISK_d{tv['distance_grid']}_wub{tv['wilson_ub']:.4f}",
                            env="live-observed",
                        ))
                        confirm_count = 0
                        time.sleep(POLL_INTERVAL_SEC)
                        continue
                    if tv.get("low_tail"):
                        eval_result.why_tag = f"{eval_result.why_tag}_LOWTAIL"

                # Enrich why_tag with confirmation count
                eval_result.why_tag = (f"{eval_result.why_tag}"
                                       f"_confirms_{confirm_count}/{tier['confirms']}")
                session_tag = _current_session()
                vol_regime = _vol_regime()
                log.warning(f"[ENGINE] Watch ladder tier {tier_idx} passed: "
                            f"{ticker} {side} {cost_int}c "
                            f"confirms {confirm_count}/{tier['confirms']}")
                return (eval_result, book, cash, pv, spot, session_tag, vol_regime)

        time.sleep(POLL_INTERVAL_SEC)


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
                   close_ts: int, original_book: kalshi.Book,
                   order_placed_ts: float = 0.0) -> Dict:
    """Monitor an open order — fills-first, blind-frozen, final-check."""
    reprices = 0
    cost_cents = eval_result.cost_cents
    cost_exact = eval_result.cost_exact or cost_cents
    is_99_band = cost_cents == 99
    blind_consecutive = 0
    gone_first_seen_ts: float = 0.0
    gone_consecutive: int = 0
    queue_t60_logged = False

    while True:
        heartbeat()
        now = time.time()
        secs_left = close_ts - now

        # T-10 cancel branch — final fills check before NO_FILL stamp
        if secs_left <= CANCEL_BEFORE_EXPIRY_SEC:
            cancel_result = kalshi.cancel_all_for_market(client, ticker)
            try:
                final_fills = kalshi.get_fills(client, ticker)
                mine = [f for f in final_fills
                        if str(f.get("order_id")) == str(order_id)]
                if mine:
                    log.warning(f"[ENGINE] Final fills check caught fill on {ticker}")
                    return _handle_fill(client, ticker, mine, row_id,
                                        eval_result, close_ts)
            except Exception as e:
                log.warning(f"[ENGINE] Final fills check failed: {e}")
            store.update_settlement(row_id, "no_fill", 0.0, time.time())
            log.info(f"[ENGINE] NO_FILL {ticker} — cancelled at T-{CANCEL_BEFORE_EXPIRY_SEC}s")
            return {"outcome": "no_fill"}

        # Poll order status — blind-frozen on failure
        try:
            status_result = kalshi.order_status(client, ticker, order_id)
            blind_consecutive = 0
        except kalshi.OrderStatusUnavailable as e:
            blind_consecutive += 1
            log.warning(f"[ENGINE] OrderStatusUnavailable #{blind_consecutive} on {ticker}: {e}")
            if blind_consecutive == 3 or (blind_consecutive > 3 and blind_consecutive % 10 == 0):
                notify.alert(f"BLIND: cannot verify order state on {ticker} "
                             f"(#{blind_consecutive}: {e})")
            if blind_consecutive >= 12:
                try:
                    kalshi.cancel_order(client, order_id)
                except Exception as ce:
                    notify.alert(f"BLIND_UNCANCELED: failed to cancel {order_id} on {ticker}: {ce}")
                store.update_why_tag(row_id, "BLIND_STANDDOWN", str(e)[:200])
                store.update_settlement(row_id, "blind_standdown", 0.0, time.time())
                return {"outcome": "blind_standdown"}
            time.sleep(POLL_INTERVAL_SEC)
            continue

        state = status_result["state"]

        if state == "filled":
            return _handle_fill(client, ticker, status_result["raw"], row_id,
                                eval_result, close_ts)

        if state == "gone":
            if secs_left <= 20:
                store.update_settlement(row_id, "no_fill", 0.0, time.time())
                log.warning(f"[ENGINE] Order {order_id} no_fill (T-{secs_left:.0f}s)")
                return {"outcome": "no_fill"}

            # Grace window: never emit "gone" within 10s of placement
            age = now - order_placed_ts if order_placed_ts else float("inf")
            if age < 10.0:
                log.info(f"[ENGINE] Grace window: order {order_id} age={age:.1f}s < 10s — resting_pending")
                gone_first_seen_ts = 0.0
                gone_consecutive = 0
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # Debounce: require TWO consecutive "gone" reads ≥5s apart
            if gone_consecutive == 0:
                gone_first_seen_ts = now
                gone_consecutive = 1
                log.info(f"[ENGINE] Gone debounce 1/2 on {order_id} — will recheck")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            else:
                gone_consecutive += 1
                elapsed = now - gone_first_seen_ts
                if elapsed < 5.0:
                    log.info(f"[ENGINE] Gone debounce {gone_consecutive}/2 on {order_id} — "
                             f"elapsed={elapsed:.1f}s < 5s, waiting")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

            # Confirmed gone after debounce — recheck fills before labeling
            try:
                recheck = kalshi.get_fills(client, ticker)
                recheck_mine = [f for f in recheck
                                if str(f.get("order_id")) == str(order_id)]
                if recheck_mine:
                    log.warning(f"[ENGINE] Cancel-race: fills found after gone on {ticker}")
                    return _handle_fill(client, ticker, recheck_mine, row_id,
                                        eval_result, close_ts)
            except Exception as e:
                log.warning(f"[ENGINE] Cancel-race fills recheck failed: {e}")
            store.update_settlement(row_id, "cancelled_external", 0.0, time.time())
            log.warning(f"[ENGINE] Order {order_id} cancelled_external (T-{secs_left:.0f}s, "
                         f"debounce={gone_consecutive} reads over {now - gone_first_seen_ts:.1f}s)")
            return {"outcome": "cancelled_external"}

        # state == "resting" — reset gone debounce, check partial fill
        gone_first_seen_ts = 0.0
        gone_consecutive = 0

        # P9 (0708): one queue-position sample inside the final minute.
        if not queue_t60_logged and secs_left <= 60:
            queue_t60_logged = True
            try:
                qp = kalshi.get_order_queue_position(client, order_id)
                if qp is not None:
                    store.update_queue_pos(row_id, "queue_pos_t60", qp)
            except Exception:
                pass
        raw = status_result.get("raw") or {}
        fc_str = raw.get("fill_count", "0") or "0"
        if Decimal(fc_str) > 0 and getattr(eval_result, "contracts", 1) == 1:
            notify.alert(f"Partial fill at 1ct?! fill_count={fc_str} on resting order {raw}")

        # Reprice gate: check fills before cancel+replace (prevents double-entry B5)
        if eval_result.lane == "F" and not is_99_band and reprices < MAX_REPRICES_LOW_BAND:
            try:
                pre_fills = kalshi.get_fills(client, ticker)
                pre_mine = [f for f in pre_fills
                            if str(f.get("order_id")) == str(order_id)]
                if pre_mine:
                    log.warning(f"[ENGINE] Reprice gate caught fill on {ticker} — skipping reprice")
                    return _handle_fill(client, ticker, pre_mine, row_id,
                                        eval_result, close_ts)
            except Exception as e:
                log.warning(f"[ENGINE] Reprice gate fills check failed: {e} — skipping reprice")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            new_book = kalshi.fetch_orderbook(client, ticker)
            new_side, new_cost_d, _, _ = gateway._favorite_side(new_book)
            if (new_side == eval_result.side and new_cost_d is not None
                    and int(new_cost_d) != cost_cents
                    and gateway.COST_BAND_LO <= new_cost_d <= gateway.COST_BAND_HI):
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
                    store.update_reprice(row_id, new_cost_out, new_oid, reprices)
                    log.info(f"[ENGINE] REPRICE #{reprices} {ticker} → {cost_cents}c oid={new_oid}")

        time.sleep(POLL_INTERVAL_SEC)


def _handle_fill(client: kalshi.KalshiClient, ticker: str,
                 fill_records: list, row_id: int,
                 eval_result: gateway.EvalResult,
                 close_ts: int) -> Dict:
    """Handle a filled order."""
    fill_cost = None
    fee_cents = 0

    if fill_records:
        fill_cost, fee_cents, _ = kalshi.parse_fills(fill_records, eval_result.side)

    if fill_cost is None:
        fill_cost = eval_result.cost_exact or eval_result.cost_cents
        notify.alert(f"Fill on {ticker} — price missing from fill records, using eval cost {fill_cost}c")

    # Widened fill assert: Decimal 95.00-99.00 for main, 80.00-94.00 for H8
    fill_d = Decimal(str(fill_cost))
    if eval_result.lane == "H8":
        valid = gateway.H8_COST_LO <= fill_d <= gateway.H8_COST_HI
    else:
        valid = gateway.COST_BAND_LO <= fill_d <= gateway.COST_BAND_HI
    if not valid:
        log.error(f"[ENGINE] PHRASING-LAW ASSERT: fill_cost={fill_cost}c outside band for {ticker} "
                  f"lane={eval_result.lane} side={eval_result.side}")
        notify.alert(f"Phrasing-law assert on fill {ticker}: {fill_cost}c lane={eval_result.lane}")
        fill_cost = eval_result.cost_exact or eval_result.cost_cents

    slippage = fill_cost - (eval_result.cost_exact or eval_result.cost_cents)

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

    settle = _wait_for_settlement(client, ticker, row_id, eval_result, fill_cost,
                                   fee_cents, close_ts)
    settle["fill_cost"] = fill_cost
    settle["fee_cents"] = fee_cents
    return settle


def _wait_for_settlement(client: kalshi.KalshiClient, ticker: str,
                         row_id: int, eval_result: gateway.EvalResult,
                         fill_cost: float, fee_cents: int,
                         close_ts: int) -> Dict:
    """Wait for market settlement after fill."""
    wait_until = close_ts + 30
    while time.time() < wait_until:
        heartbeat()
        time.sleep(min(15, max(1, wait_until - time.time())))

    deadline = time.time() + 300
    while time.time() < deadline:
        heartbeat()
        result = kalshi.get_settlement_result(client, ticker)
        if result is not None:
            won = (result == eval_result.side)
            n_ct = max(1, getattr(eval_result, "contracts", 1))
            if won:
                # P16 (0708): per-contract clip x contracts, fee is total.
                pnl = ((100 - fill_cost) * n_ct - fee_cents) / 100.0
                resolution = "win"
                discipline.record_win()
                split = treasury.waterfall(pnl)
            else:
                pnl = -(fill_cost * n_ct + fee_cents) / 100.0
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

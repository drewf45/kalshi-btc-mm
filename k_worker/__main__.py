"""k_worker main entry point.

Boot sequence:
1. envcheck — validate all config
2. notify — init Telegram
3. store — init SQLite
4. kalshi — build client, read balance
5. Main loop: discover market -> engine cycle -> repeat
6. Scoreboard on schedule (configurable via SCOREBOARD_EVERY_HOURS)
7. Settlement backfill every ~5 min
8. Hourly balance Telegram message
"""

import os
import time
import signal
import logging
from datetime import datetime
from typing import Optional

from . import envcheck, notify, kalshi, store, gateway, discipline, engine, scoreboard, treasury

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("k_worker")

LOOP_SLEEP_SEC = 30
SCOREBOARD_INTERVAL_SEC = int(float(os.environ.get("SCOREBOARD_EVERY_HOURS", "4")) * 3600)
BACKFILL_INTERVAL_SEC = 300
RECONCILE_INTERVAL_SEC = 300
CENSUS_INTERVAL_SEC = 3600
HOURLY_BALANCE_SEC = 3600
_running = True


def _shutdown(sig, frame):
    global _running
    log.warning(f"[MAIN] Received signal {sig} — shutting down")
    _running = False


STALE_TICKER_SEC = 86400  # 24 hours


def _backfill_settlements(client: kalshi.KalshiClient) -> int:
    """S1: Backfill settlement results for unresolved rows with closed markets.
    Fills get win/loss, SKIPs with a recorded side get obs_win/obs_loss.
    Computes winner_clip_cents and populates market_ledger per ticker.
    Returns count of rows updated."""
    updated = 0
    now = time.time()
    tickers = store.unresolved_tickers()
    for ticker in tickers:
        result = kalshi.get_settlement_result(client, ticker)
        if result is None:
            close_ts = store.approx_close_ts(ticker)
            if close_ts > 0 and (now - close_ts) > STALE_TICKER_SEC:
                log.warning(f"[BACKFILL] {ticker} beyond live tier — marking stale")
                for row_id, action, side, cost_cents, fee_cents in store.get_unresolved_rows(ticker):
                    store.update_settlement(row_id, "obs_stale", 0.0, now)
                    updated += 1
            continue
        rows = store.get_unresolved_rows(ticker)
        realized_cents = 0.0
        best_available_cents = 0.0
        regret_avoided = 0.0
        has_fill = False
        for row_id, action, side, cost_cents, fee_cents in rows:
            # Winner clip: if favorite == winner -> clip = 100-cost; else -> -cost
            clip = None
            if side is not None and cost_cents is not None:
                if result == side:
                    clip = 100.0 - float(cost_cents)
                else:
                    clip = -float(cost_cents)
                store.update_winner_clip(row_id, clip)
                if clip > 0 and clip > best_available_cents:
                    best_available_cents = clip

            if action == "ENTER":
                won = (result == side) if side else False
                if won:
                    pnl = (100 - (cost_cents or 0) - (fee_cents or 0)) / 100.0
                    res = "win"
                    discipline.record_win()
                    treasury.waterfall(pnl)
                    realized_cents = 100.0 - float(cost_cents or 0) - float(fee_cents or 0)
                else:
                    pnl = -((cost_cents or 0) + (fee_cents or 0)) / 100.0
                    res = "loss"
                    discipline.record_loss()
                    treasury.record_loss(pnl)
                    realized_cents = -(float(cost_cents or 0) + float(fee_cents or 0))
                has_fill = True
            else:
                if side is None:
                    continue
                won = (result == side)
                res = "obs_win" if won else "obs_loss"
                pnl = 0.0
                if clip is not None and clip < 0:
                    regret_avoided += abs(clip)
            store.update_settlement(row_id, res, pnl, time.time())
            updated += 1

        # Market ledger
        regret_missed = max(0, best_available_cents - max(0, realized_cents))
        if has_fill:
            if realized_cents > 0:
                outcome_class = "CAPTURED"
            else:
                outcome_class = "TOOK_LOSS"
        elif best_available_cents > 0:
            outcome_class = "MISSED_CLIP"
        elif regret_avoided > 0:
            outcome_class = "DODGED_LOSS"
        else:
            outcome_class = "UNOBSERVED"
        store.upsert_market_ledger(
            ticker, time.time(), result, realized_cents,
            best_available_cents, regret_missed, regret_avoided,
            outcome_class,
        )

    if updated:
        log.info(f"[BACKFILL] Updated {updated} rows across {len(tickers)} tickers")
    return updated


def _reconcile_fills(client: kalshi.KalshiClient) -> int:
    """Pull broker fills, diff against store, upgrade missed no_fill rows."""
    upgraded = 0
    try:
        broker_fills = kalshi.get_all_recent_fills(client, limit=200)
    except Exception as e:
        log.warning(f"[RECONCILE] Failed to fetch broker fills: {e}")
        return 0

    fill_by_oid = {}
    for f in broker_fills:
        oid = str(f.get("order_id", ""))
        if oid:
            fill_by_oid.setdefault(oid, []).append(f)

    nofill_rows = store.get_nofill_enter_rows(limit=200)
    for row_id, ticker, order_id, cost_cents, fee_cents, side in nofill_rows:
        if order_id and order_id in fill_by_oid:
            log.warning(f"[RECONCILE] Found broker fill for no_fill row {row_id} "
                        f"ticker={ticker} oid={order_id} — upgrading")
            store.clear_resolution(row_id)
            fills = fill_by_oid[order_id]
            fill_cost, fc, _ = kalshi.parse_fills(fills, side or "yes")
            if fill_cost is None:
                fill_cost = cost_cents or 0
            store.update_fill(row_id, fill_cost, time.time(), 0, fc)
            store.update_why_tag(row_id, "RECONCILED_FROM_BROKER")
            upgraded += 1
            notify.send(f"🔧 RECONCILE: upgraded row {row_id} ({ticker}) from no_fill → filled")

    if upgraded:
        log.warning(f"[RECONCILE] Upgraded {upgraded} rows from no_fill → filled")
    return upgraded


def _run_census(client: kalshi.KalshiClient) -> None:
    """Census: pull today's settled KXBTC15M from exchange, diff against surface."""
    try:
        settled = kalshi.get_todays_settled_tickers(client)
    except Exception as e:
        log.warning(f"[CENSUS] Exchange query failed: {e}")
        return
    if not settled:
        return
    covered = store.get_covered_tickers_today()
    missed = [t for t in settled if t not in covered]
    for ticker in missed:
        store.insert_row(store.SurfaceRow(
            market_ticker=ticker, decision_ts=time.time(), action="SKIP",
            skip_reason="UNSEEN_BY_ENGINE", why_tag="MISSED_UNSEEN",
            env="live-observed",
        ))
    total = len(settled)
    seen_live = len(covered)
    explained_missed = len(missed)
    store.set_state("census_total", str(total))
    store.set_state("census_covered", str(seen_live))
    store.set_state("census_missed", str(explained_missed))
    if missed:
        log.warning(f"[CENSUS] {seen_live} seen-live / {explained_missed} explained-missed / {total} total — missed: {missed[:5]}")
        notify.alert(f"Census: {explained_missed} missed windows — {seen_live}/{total} seen-live")
    else:
        log.info(f"[CENSUS] {seen_live}/{total} seen-live ✓")


def _send_hourly_balance(client: kalshi.KalshiClient) -> None:
    """T2: Send hourly balance status to Telegram."""
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        return
    total = cash + (pv or 0)
    daily = store.daily_stats("live-traded")
    broker_fills = store.count_fills_today("live-traded")
    treas = treasury.format_hourly()
    treasury.check_invariant(total)
    notify.send(
        f"Balance: ${total:.2f} | positions ${pv or 0:.2f} | "
        f"today: {broker_fills} fills, {daily['n']} settled, net ${daily['net_pnl']:.2f}\n"
        f"{treas}"
    )


BOOT_RECONCILE_TOL = 0.05


def _fill_timestamp(fill: dict) -> Optional[float]:
    """Extract Unix timestamp from a Kalshi fill record."""
    for k in ("created_time", "trade_time", "updated_time"):
        v = fill.get(k)
        if isinstance(v, str) and v:
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                pass
    for k in ("created_ts", "ts", "trade_ts"):
        v = fill.get(k)
        if v is not None:
            try:
                val = float(v)
                return val / 1000 if val > 10_000_000_000 else val
            except (ValueError, TypeError):
                pass
    return None


def _fill_side(fills: list) -> str:
    """Determine our side from fill records. bid->yes, ask->no."""
    for f in fills:
        s = str(f.get("side", "")).lower()
        if s in ("yes", "no"):
            return s
        if s == "bid":
            return "yes"
        if s == "ask":
            return "no"
    return "yes"


def rebuild_today(client: kalshi.KalshiClient) -> int:
    """Rebuild today's surface from broker fills on a fresh-store boot.
    Replays settled wins through accrual math (without waterfall — book already
    includes the P&L). Returns count of tickers rebuilt."""
    try:
        fills = kalshi.get_all_recent_fills(client, limit=200)
    except Exception as e:
        log.warning(f"[REBUILD] Failed to fetch broker fills: {e}")
        notify.alert(f"REBUILD: broker fills fetch failed: {e}")
        return 0

    today_start = store.et_midnight_ts()
    today_fills = [f for f in fills if (_fill_timestamp(f) or 0) >= today_start]

    if not today_fills:
        log.info("[REBUILD] No broker fills today — nothing to rebuild")
        return 0

    by_ticker: dict = {}
    for f in today_fills:
        tk = f.get("ticker") or f.get("market_ticker", "")
        if tk:
            by_ticker.setdefault(tk, []).append(f)

    rebuilt = 0
    total_tax = 0.0
    total_fee = 0.0

    for ticker, fill_records in by_ticker.items():
        if not ticker.startswith("KXBTC15M"):
            continue
        if store.has_row_for_ticker(ticker):
            continue

        side = _fill_side(fill_records)
        cost_cents, fee_cents, count = kalshi.parse_fills(fill_records, side)

        fill_ts = _fill_timestamp(fill_records[0]) or time.time()

        row_id = store.insert_row(store.SurfaceRow(
            market_ticker=ticker,
            decision_ts=fill_ts,
            action="ENTER",
            side=side,
            cost_per_contract_cents=int(cost_cents) if cost_cents is not None else None,
            fill_cost_cents=int(cost_cents) if cost_cents is not None else None,
            fill_ts=fill_ts,
            fee_cents=fee_cents,
            contracts=count,
            why_tag="REBUILT_FROM_BROKER",
            env="live-traded",
        ))

        result = kalshi.get_settlement_result(client, ticker)
        if result is not None and side and cost_cents is not None:
            won = (result == side)
            if won:
                pnl = (100 - cost_cents - fee_cents) / 100.0
                tax = pnl * treasury.TAX_RATE
                fee_amt = pnl * treasury.OPERATOR_FEE
                total_tax += tax
                total_fee += fee_amt
                store.update_settlement(row_id, "win", pnl, time.time())
                discipline.record_win()
            else:
                pnl = -(cost_cents + fee_cents) / 100.0
                store.update_settlement(row_id, "loss", pnl, time.time())
                discipline.record_loss()

        rebuilt += 1

    if total_tax > 0 or total_fee > 0:
        treasury.rebuild_accruals(total_tax, total_fee)

    log.warning(f"[REBUILD] {rebuilt} tickers rebuilt from broker fills "
                f"(accrued: tax=${total_tax:.3f} fee=${total_fee:.3f})")
    notify.send(f"🔧 REBUILD: {rebuilt} tickers rebuilt from broker fills "
                f"(tax ${total_tax:.3f}, fee ${total_fee:.3f})")
    return rebuilt


def boot_reconcile(client: kalshi.KalshiClient) -> None:
    """Drew's law 2026-07-07: never trust stored treasury across a deploy
    boundary. Pull live broker truth and reconcile BEFORE the first cycle."""
    live_bal, _ = kalshi.get_balance(client)
    if live_bal is None:
        raise RuntimeError("FATAL: boot reconcile cannot read balance — refusing to trade on unknown money")

    t = treasury.snapshot()
    expected = t.book + t.accrued_tax + t.accrued_fee
    delta = round(live_bal - expected, 2)
    fresh = treasury.is_genesis() and store.count_rows_total() == 0

    if fresh:
        treasury.set_book(live_bal)
        store.record_epoch(t.book, live_bal, delta, "fresh_store_init")
        notify.send(f"🔧 BOOT RECONCILE: fresh store — book initialized from live balance ${live_bal:.2f} (EPOCH)")
        rebuild_today(client)
    elif abs(delta) <= BOOT_RECONCILE_TOL:
        notify.send(f"🔧 BOOT RECONCILE OK: bal ${live_bal:.2f} ≈ expected ${expected:.2f} (Δ ${delta:+.2f})")
    else:
        new_book = round(live_bal - t.accrued_tax - t.accrued_fee, 2)
        treasury.set_book(new_book)
        store.record_epoch(t.book, new_book, delta, "boot_rebaseline")
        notify.send(
            f"🔧 BOOT RECONCILE: book ${t.book:.2f} → ${new_book:.2f} "
            f"(Δ ${delta:+.2f}) — re-baselined to live balance; accruals preserved (EPOCH)"
        )

    try:
        for p in kalshi.get_positions(client):
            tk = p.get("ticker") or p.get("market_ticker")
            if tk and not store.has_row_for_ticker(tk):
                store.insert_orphan_row(tk, p)
                notify.alert(f"ORPHAN POSITION at boot: {tk} — row created; sweep will settle it")
    except Exception as e:
        notify.alert(f"Boot position scan failed: {e} — positions unverified this boot")


def main():
    global _running
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 1. Environment check
    log.info("[MAIN] === K-WORKER BOOT ===")
    vals = envcheck.check_env()
    is_live = vals["_is_live"]
    worker_mode = vals["_mode"]
    env_label = "LIVE" if is_live else "DEMO"
    mode_label = worker_mode.upper()
    log.warning(f"[MAIN] Env: {env_label} | Mode: {mode_label}")

    if worker_mode == "observe":
        engine.set_observe_mode(True)

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
    store.dedup_historical_skips()

    # 4. Load persisted gateway state + treasury
    gateway._load_persisted_state()
    treasury.init_book()

    # 5. Kalshi client
    client = kalshi.build_client()
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        raise RuntimeError("FATAL: Cannot read balance from Kalshi")

    total = cash + (pv or 0)
    log.warning(f"[MAIN] Balance: cash=${cash:.2f} positions=${pv or 0:.2f} total=${total:.2f}")
    log.warning(f"[MAIN] Engine: KXBTC15M | flat 1ct | fav 95-99c | maker-only")

    if worker_mode == "trade":
        discipline.check_drawdown(cash)

    envcheck.check_clock_skew()

    # 5b. Route probes — FATAL if no working route
    fills_route = kalshi.probe_fills_route(client)
    orders_route = kalshi.probe_orders_route(client)
    notify.send(f"Route probes: fills {fills_route} ✓ | orders {orders_route} ✓")

    # 5c. Boot reconcile — Drew's law: never trust stored treasury across deploys
    boot_reconcile(client)

    if engine.HEARTBEAT_PING_URL:
        log.info(f"[MAIN] Dead-man ping configured: {engine.HEARTBEAT_PING_URL[:40]}...")
    else:
        log.info("[MAIN] No HEARTBEAT_PING_URL — dead-man ping disabled")

    last_scoreboard = 0
    last_backfill = 0
    last_reconcile = 0
    last_census = 0
    last_hourly = 0
    last_ticker = None

    # 6. Main loop
    while _running:
        try:
            engine.heartbeat()
            now = time.time()

            if worker_mode == "trade" and discipline.is_halted():
                log.warning(f"[MAIN] Engine halted: {discipline.halt_reason()}")
                time.sleep(LOOP_SLEEP_SEC)
                continue

            # Settlement backfill (S1) — every ~5 min
            if now - last_backfill > BACKFILL_INTERVAL_SEC:
                try:
                    _backfill_settlements(client)
                except Exception as e:
                    log.warning(f"[MAIN] Backfill error: {e}")
                last_backfill = now

            # Broker-fills reconciler (WO-C) — every ~5 min
            if now - last_reconcile > RECONCILE_INTERVAL_SEC:
                try:
                    _reconcile_fills(client)
                except Exception as e:
                    log.warning(f"[MAIN] Reconcile error: {e}")
                last_reconcile = now

            # Census (Part 4.2) — every hour
            if now - last_census > CENSUS_INTERVAL_SEC:
                try:
                    _run_census(client)
                except Exception as e:
                    log.warning(f"[MAIN] Census error: {e}")
                last_census = now

            # Hourly balance Telegram (T2)
            if now - last_hourly > HOURLY_BALANCE_SEC:
                try:
                    _send_hourly_balance(client)
                except Exception as e:
                    log.warning(f"[MAIN] Hourly balance error: {e}")
                last_hourly = now

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

            secs_to_expiry = close_ts - now

            if secs_to_expiry < 10:
                time.sleep(LOOP_SLEEP_SEC)
                continue

            if ticker != last_ticker:
                log.info(f"[MAIN] Market: {ticker} closes in {secs_to_expiry:.0f}s")
                gateway.reset_for_new_market()
                last_ticker = ticker

            if worker_mode == "trade" and gateway.is_traded(ticker):
                time.sleep(LOOP_SLEEP_SEC)
                continue

            if worker_mode == "trade":
                try:
                    pos = kalshi.position_for_market(client, ticker)
                    if abs(pos) > 0:
                        gateway.mark_traded(ticker)
                        log.info(f"[MAIN] Already holding position on {ticker}")
                        time.sleep(LOOP_SLEEP_SEC)
                        continue
                except Exception as e:
                    log.warning(f"[MAIN] Position check failed for {ticker}: {e}")
                    store.insert_row(store.SurfaceRow(
                        market_ticker=ticker, decision_ts=time.time(), action="SKIP",
                        skip_reason=f"POSCHECK_FAILED:{e}", why_tag="SKIP_POSCHECK_FAILED",
                        env="live-observed",
                    ))
                    notify.alert(f"Position check failed for {ticker}: {e} — skipping cycle")
                    time.sleep(LOOP_SLEEP_SEC)
                    continue

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

    log.warning("[MAIN] Shutting down...")
    try:
        scoreboard.send_scoreboard()
    except Exception:
        pass
    notify.send("<b>K-WORKER STOPPED</b>")
    log.warning("[MAIN] Done.")


if __name__ == "__main__":
    main()

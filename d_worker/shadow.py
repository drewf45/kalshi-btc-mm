"""Shadow seed lifecycle: book snapshot, dual fill model, settle.

Telegram messages: SEED placed (🌱) and SETTLED (✅/🚨) only.
Invariant break sets halt_promotion; cleared via DW_CLEAR_HALT=1 at next boot.

Settlement nets subtract seed-time order fees (from sizes_json) so the
values written to net_clip_taker/maker_cents and consumed by the GATE MARCH
rollup are true net, never gross.
"""

import json
import math
import time
import logging
from typing import Optional, List

from k_worker import kalshi, notify

from . import dstore, feemath, classify

log = logging.getLogger("d_worker.shadow")

BATCH_SIZES = [1, 5, 10]


def _wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound (copied from scoreboard pattern, per zero-touch)."""
    if n == 0:
        return 0.0
    p_hat = wins / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n) / denom
    return max(0.0, centre - spread)


def record_seed(verdict: classify.VerdictRow, book, market: dict) -> Optional[int]:
    """Record a shadow seed from a SEED verdict. Returns seed_id or None."""
    ticker = verdict.market_ticker
    side = verdict.side
    close_ts = verdict.close_ts

    if dstore.is_already_seeded(ticker):
        return None

    book_json = "{}"
    taker_price = None
    maker_price = None
    if book is not None:
        book_json = json.dumps({
            "yes_bid": book.yes_bid, "yes_ask": book.yes_ask,
            "no_bid": book.no_bid, "no_ask": book.no_ask,
            "yes_bid_qty": book.yes_bid_qty, "no_bid_qty": book.no_bid_qty,
        })
        if side == "yes":
            taker_price = book.yes_ask
            maker_price = (book.yes_bid + 1) if book.yes_bid is not None else None
        elif side == "no":
            taker_price = book.no_ask
            maker_price = (book.no_bid + 1) if book.no_bid is not None else None

    fee_taker_mult = feemath.taker_mult(verdict.fee_taker or 1.0)
    fee_maker_mult = feemath.maker_mult(verdict.fee_maker or 1.0)

    sizes_data = {}
    taker_viable = False
    import os
    batch_str = os.environ.get("DW_SHADOW_BATCH_SIZES", "1,5,10")
    batch_sizes = [int(x) for x in batch_str.split(",")]
    min_net = float(os.environ.get("DW_MIN_NET_CLIP_CENTS", "1.0"))

    for sz in batch_sizes:
        entry = {"size": sz}
        if taker_price is not None and taker_price < 100:
            fee_t = feemath.order_fee_cents(fee_taker_mult, sz, taker_price)
            net_t = (100 - taker_price) * sz - fee_t
            entry["taker_fee"] = fee_t
            entry["taker_net_clip"] = net_t
            if net_t / sz >= min_net:
                taker_viable = True
        if maker_price is not None and maker_price < 100:
            fee_m = feemath.order_fee_cents(fee_maker_mult, sz, maker_price)
            net_m = (100 - maker_price) * sz - fee_m
            entry["maker_fee"] = fee_m
            entry["maker_net_clip"] = net_m
        sizes_data[str(sz)] = entry

    verdict_id = dstore.insert_verdict(
        0, ticker, verdict.series_ticker, close_ts,
        verdict.dclass, "SEED", side,
        verdict.evidence, verdict.fee_maker, verdict.fee_taker)

    seed_id = dstore.insert_seed(
        verdict_id, ticker, verdict.dclass, side,
        book_json, taker_price or 0, taker_viable,
        maker_price or 0, json.dumps(sizes_data), close_ts)

    ev = verdict.evidence or {}
    obs_val = ev.get("obs_value", "?")
    obs_src = ev.get("obs_source", "?")
    stale = ev.get("staleness_sec", 0)
    strike = ev.get("strike", "?")

    price_info = ""
    if taker_price and batch_sizes:
        best_sz = batch_sizes[-1]
        entry = sizes_data.get(str(best_sz), {})
        net_per_ct = entry.get("taker_net_clip", 0) / best_sz if best_sz else 0
        price_info = f"taker {taker_price}¢→net {net_per_ct:.1f}¢/ct@{best_sz}"

    notify.send(
        f"🅳 🌱 SHADOW SEED — {ticker} {(side or '').upper()} | "
        f"{price_info} | {verdict.dclass} obs {obs_val}≥{strike} "
        f"({obs_src}, fresh {stale:.0f}s)")

    log.warning(f"[SHADOW] Seed {seed_id}: {ticker} {side} "
                f"taker={taker_price} maker={maker_price}")
    return seed_id


def _settle_net(correct: bool, price: int, fee: int) -> float:
    """Compute true net clip (after fees) for one side at settlement."""
    if price <= 0:
        return 0.0
    if correct:
        return float((100 - price) - fee)
    else:
        return float(-price)


def _extract_1lot_fees(sizes_json_str: str) -> tuple:
    """Extract 1-lot taker and maker fees from seed-time sizes_json.
    Returns (taker_fee, maker_fee)."""
    try:
        sizes = json.loads(sizes_json_str) if sizes_json_str else {}
    except (json.JSONDecodeError, TypeError):
        sizes = {}
    lot1 = sizes.get("1", {})
    return (lot1.get("taker_fee", 0), lot1.get("maker_fee", 0))


def settle_seeds(client: kalshi.KalshiClient,
                 governor=None) -> int:
    """Poll and settle unsettled seeds. Returns count settled.
    If governor is provided, settlement API calls consume tokens."""
    seeds = dstore.get_unsettled_seeds()
    settled = 0
    now = time.time()
    for seed in seeds:
        if seed["close_ts"] and now < seed["close_ts"] + 60:
            continue
        ticker = seed["market_ticker"]
        if governor:
            governor.consume(1)
        try:
            result = kalshi.get_settlement_result(client, ticker)
        except Exception as e:
            log.warning(f"[SHADOW] Settlement check failed for {ticker}: {e}")
            continue
        if result is None:
            continue
        side = seed["side"]
        correct = (result == side)
        taker_price = seed["taker_price_cents"] or 0
        maker_price = seed["maker_price_cents"] or 0

        taker_fee, maker_fee = _extract_1lot_fees(seed.get("sizes_json", ""))
        net_taker = _settle_net(correct, taker_price, taker_fee)
        net_maker = _settle_net(correct, maker_price, maker_fee)

        lockup_days = 0.0
        if seed["ts"] and seed["close_ts"]:
            lockup_days = (taker_price * (now - seed["ts"]) / 86400.0) / 100.0

        maker_filled = _check_maker_fill(seed)

        dstore.settle_seed(seed["id"], result, correct, maker_filled,
                           net_taker, net_maker, lockup_days)
        settled += 1

        status = "✓" if correct else "✗ WRONG"
        maker_label = "UNINSTRUMENTED" if maker_filled is None else (
            "filled est" if maker_filled else "not filled")
        notify.send(
            f"🅳 {'✅' if correct else '🚨'} SETTLED — {ticker} | "
            f"classifier {status} | net taker {net_taker:+.1f}¢, "
            f"maker {net_maker:+.1f}¢ ({maker_label}) | "
            f"lockup {lockup_days:.1f}¢-days")

        if not correct:
            dstore.set_state("halt_promotion", "1")
            notify.alert(
                f"🅳 🚨 INVARIANT BREAK: {ticker} classified {side} "
                f"but settled {result}. halt_promotion set. "
                f"Action: review evidence, set DW_CLEAR_HALT=1 and restart to resume.")
            log.error(f"[SHADOW] INVARIANT BREAK: {ticker} {side} → {result}")

    return settled


def _check_maker_fill(seed: dict) -> Optional[bool]:
    """Estimate if the maker touch traded through. Returns None (uninstrumented)."""
    return None


def nightly_rollup() -> None:
    """Roll up daily class stats with Wilson LB."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    all_settled = [s for s in _all_settled_seeds() if s.get("settled_ts")]

    by_class: dict = {}
    for s in all_settled:
        dc = s.get("dclass", "NONE")
        by_class.setdefault(dc, []).append(s)

    for dclass, items in by_class.items():
        n = len(items)
        correct = sum(1 for s in items if s.get("classifier_correct"))
        maker_fills = sum(1 for s in items if s.get("maker_filled_est"))
        taker_viable = sum(1 for s in items if s.get("taker_viable"))
        w_taker = _wilson_lb(correct, n)
        w_maker = _wilson_lb(correct, n)
        dstore.upsert_daily_stats(today, dclass, n, correct,
                                   w_taker, w_maker, maker_fills, taker_viable)


def _all_settled_seeds() -> List[dict]:
    with dstore._lock:
        rows = dstore._conn.execute(
            "SELECT * FROM shadow_seeds WHERE settled_ts IS NOT NULL"
        ).fetchall()
    cols = ["id", "verdict_id", "ts", "market_ticker", "dclass", "side",
            "book_json", "taker_price_cents", "taker_viable", "maker_price_cents",
            "sizes_json", "close_ts", "settled_ts", "result",
            "classifier_correct", "maker_filled_est",
            "net_clip_taker_cents", "net_clip_maker_cents", "lockup_capital_days"]
    return [dict(zip(cols, r)) for r in rows]

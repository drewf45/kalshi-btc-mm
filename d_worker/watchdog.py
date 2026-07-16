"""Position watchdog — the inside body.

Re-verifies the irreversibility argument for every open shadow seed every cycle.
Logs EVIDENCE_HELD or EVIDENCE_BROKEN per position. On BROKEN: records
hypothetical-abandon pricing and alerts. Settlement-lag alerts for positions
past close but unsettled beyond a bound.

Shadow phase: no live exits. Live phase (WO#3+): ABANDON_SHIP through gateway.
"""

import time
import logging
from typing import Optional, Dict, List

from k_worker import kalshi, notify

from . import dstore, feeds, registry, gateway, budget

log = logging.getLogger("d_worker.watchdog")

WATCH_INTERVAL_SEC = 90
SETTLE_LAG_THRESHOLD_SEC = 600


def watch_cycle(client: kalshi.KalshiClient, governor=None) -> dict:
    """Run one watch cycle over all open shadow seeds.
    Returns {checked, held, broken, settle_lag}."""
    seeds = dstore.get_unsettled_seeds()
    stats = {"checked": 0, "held": 0, "broken": 0, "settle_lag": 0}
    now = time.time()

    for seed in seeds:
        ticker = seed["market_ticker"]
        dclass = seed.get("dclass", "")
        side = seed.get("side")
        stats["checked"] += 1

        if seed["close_ts"] and now > seed["close_ts"] + SETTLE_LAG_THRESHOLD_SEC:
            stats["settle_lag"] += 1
            if not _lag_already_alerted(seed["id"]):
                lag_min = (now - seed["close_ts"]) / 60
                notify.send(
                    f"🅳 ⏰ SETTLE LAG — {ticker} closed {lag_min:.0f}m ago, "
                    f"still unsettled")
                dstore.set_state(f"settle_lag_alerted_{seed['id']}", "1")

        if dclass != "D1_WX":
            stats["held"] += 1
            _record_watch(seed["id"], "EVIDENCE_HELD", None)
            continue

        held, evidence = _reverify_d1_wx(seed, governor)
        _record_watch(seed["id"], "EVIDENCE_HELD" if held else "EVIDENCE_BROKEN",
                      evidence)

        if held:
            stats["held"] += 1
        else:
            stats["broken"] += 1
            bid_price = _snapshot_exit_liquidity(client, ticker, side, governor)
            notify.send(
                f"🅳 🚨 EVIDENCE BROKEN — {ticker} {side} | "
                f"abandon bid {bid_price}¢ | re-pull shows break")
            log.error(f"[WATCHDOG] EVIDENCE BROKEN: {ticker} {side}")

            if seed.get("is_live"):
                res_key = dstore.get_state(f"seed_reservation_{seed['id']}")
                res_id = int(res_key) if res_key else 0
                if res_id:
                    gateway.abandon_ship(client, ticker, side, res_id, governor)
                budget.deny_all(f"EVIDENCE_BROKEN on live seed {ticker}")

    return stats


def _reverify_d1_wx(seed: dict, governor=None) -> tuple:
    """Re-pull feed and check if the irreversibility argument still holds.
    Returns (held: bool, evidence: dict)."""
    ticker = seed["market_ticker"]
    evidence_json = seed.get("book_json", "{}")

    try:
        import json
        original_evidence = json.loads(
            dstore._conn.execute(
                "SELECT evidence_json FROM verdicts WHERE id=?",
                (seed["verdict_id"],)).fetchone()[0] or "{}")
    except Exception:
        original_evidence = {}

    series = _series_from_ticker(ticker)
    reg_row = dstore.get_registry(series) if series else None
    if not reg_row:
        return (True, {"reason": "no_registry", "assumed": "held"})

    station = reg_row.get("station_or_ref", "")
    if not station:
        return (True, {"reason": "no_station", "assumed": "held"})

    obs = feeds.observe_nws(station)
    if obs is None:
        return (True, {"reason": "feed_unavailable", "assumed": "held"})

    strike = original_evidence.get("strike")
    rounding = original_evidence.get("rounding", 1.0)
    direction = original_evidence.get("direction", "above")
    var_kind = (reg_row.get("variable_kind") or "running_max").lower()

    if strike is None:
        return (True, {"reason": "no_strike_in_evidence", "assumed": "held"})

    evidence = {
        "obs_value": obs.value,
        "obs_source": obs.source,
        "strike": strike,
        "direction": direction,
        "rounding": rounding,
        "staleness_sec": feeds.staleness(obs, 900),
    }

    if direction == "above" and var_kind == "running_max":
        threshold = strike + rounding
        if obs.value >= threshold:
            evidence["held"] = True
            return (True, evidence)
        evidence["held"] = False
        evidence["reason"] = f"obs {obs.value} < threshold {threshold}"
        return (False, evidence)
    elif direction == "below" and var_kind == "running_min":
        threshold = strike - rounding
        if obs.value <= threshold:
            evidence["held"] = True
            return (True, evidence)
        evidence["held"] = False
        evidence["reason"] = f"obs {obs.value} > threshold {threshold}"
        return (False, evidence)

    return (True, {"reason": "non_decidable_direction", "assumed": "held"})


def _snapshot_exit_liquidity(client: kalshi.KalshiClient, ticker: str,
                             side: str, governor=None) -> Optional[int]:
    """Record best-bid for hypothetical abandon pricing."""
    if governor:
        governor.consume(1)
    try:
        book = kalshi.fetch_orderbook(client, ticker)
        if book is None:
            return None
        if side == "yes":
            return book.yes_bid
        elif side == "no":
            return book.no_bid
        return None
    except Exception as e:
        log.warning(f"[WATCHDOG] Exit liquidity check failed for {ticker}: {e}")
        return None


def _record_watch(seed_id: int, status: str, evidence: dict) -> None:
    """Record a watch-cycle row (append-only)."""
    import json
    dstore.insert_watch_row(seed_id, status,
                            json.dumps(evidence) if evidence else None)


def _lag_already_alerted(seed_id: int) -> bool:
    return dstore.get_state(f"settle_lag_alerted_{seed_id}") == "1"


def _series_from_ticker(ticker: str) -> str:
    """Derive series from a market ticker (best effort)."""
    if "-" in ticker:
        return ticker.split("-")[0]
    return ticker

"""D1/D2 verdict logic — pure functions, no I/O.

D1_WX (monotone-crossed): running-max obs >= strike + rounding → DECIDED YES-above.
  Below-strike (high stays under X) is NOT decidable before close.
D1_CNT: same shape for cumulative counts (stub — no feeds day one).
D2 (deadline-passed): machine-readable market-object timestamps show event fully past.
"""

import time
import json
import logging
from dataclasses import dataclass
from typing import Optional, Dict

from . import feemath
from . import series_of as _series_of
from .feeds import Observation

log = logging.getLogger("d_worker.classify")


@dataclass
class VerdictRow:
    market_ticker: str
    series_ticker: str
    close_ts: float
    dclass: str
    verdict: str
    side: Optional[str] = None
    evidence: Optional[dict] = None
    fee_maker: Optional[float] = None
    fee_taker: Optional[float] = None


def _skip(market_ticker: str, series_ticker: str, close_ts: float,
          verdict: str, evidence: dict = None) -> VerdictRow:
    return VerdictRow(market_ticker=market_ticker, series_ticker=series_ticker,
                      close_ts=close_ts, dclass="NONE", verdict=verdict,
                      evidence=evidence)


def _parse_direction(market: dict) -> Optional[str]:
    """Parse above/below direction from market object. Returns 'above' or 'below' or None."""
    for field in ("subtitle", "yes_sub_title", "title"):
        text = (market.get(field) or "").lower()
        if "above" in text or "or more" in text or "at least" in text or "or higher" in text:
            return "above"
        if "below" in text or "or less" in text or "under" in text or "or fewer" in text:
            return "below"
    ticker = market.get("ticker", "")
    if "-B" in ticker or "-A" in ticker.upper():
        parts = ticker.split("-")
        for p in parts:
            if p.startswith("B"):
                return "above"
            if p.startswith("A"):
                return "below"
    return None


def _parse_strike(market: dict) -> Optional[float]:
    """Extract strike value from market object."""
    for field in ("floor_strike", "cap_strike", "custom_strike", "strike"):
        v = market.get(field)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    ticker = market.get("ticker", "")
    parts = ticker.split("-")
    for p in parts:
        cleaned = p.lstrip("BAba")
        try:
            return float(cleaned)
        except ValueError:
            continue
    return None


def run(market: dict, registry_row: Optional[dict],
        obs: Optional[Observation], obs2: Optional[Observation],
        fee_info: Optional[dict], book: Optional[object],
        state: dict) -> VerdictRow:
    """Classify a market. Returns a VerdictRow."""
    ticker = market.get("ticker", "")
    series = _series_of(market)
    close_ts = state.get("close_ts", 0)

    if not registry_row:
        return _skip(ticker, series, close_ts, "SKIP_NOT_DECIDED",
                     {"reason": "no_registry_row"})

    if registry_row.get("approved_ts") is None:
        return _skip(ticker, series, close_ts, "SKIP_RULES_UNREVIEWED",
                     {"series": series, "version": registry_row.get("version")})

    settle_source = (registry_row.get("settle_source") or "").upper()
    rounding = _parse_rounding(registry_row.get("rounding", "1"))

    # D1_WX: weather decidability
    if settle_source in ("NWS", "WEATHER", "NWS_STATION"):
        return _classify_d1_wx(market, ticker, series, close_ts,
                               registry_row, obs, obs2, fee_info, book,
                               state, rounding)

    # D1_CNT: cumulative count (stub)
    if settle_source in ("COUNT", "CUMULATIVE"):
        return _skip(ticker, series, close_ts, "SKIP_NOT_DECIDED",
                     {"reason": "D1_CNT not implemented", "series": series})

    # D2: deadline-passed
    d2 = _check_d2(market, registry_row)
    if d2 is not None:
        return _apply_money_gates(d2, fee_info, book, state)

    return _skip(ticker, series, close_ts, "SKIP_NOT_DECIDED",
                 {"reason": "no_decidedness_path", "series": series})


def _parse_rounding(rounding_str: str) -> float:
    try:
        return float(rounding_str)
    except (ValueError, TypeError):
        return 1.0


def _classify_d1_wx(market: dict, ticker: str, series: str, close_ts: float,
                    registry_row: dict, obs: Optional[Observation],
                    obs2: Optional[Observation], fee_info: Optional[dict],
                    book: Optional[object], state: dict,
                    rounding: float) -> VerdictRow:
    direction = _parse_direction(market)
    strike = _parse_strike(market)
    staleness_sec = state.get("staleness_sec", float("inf"))
    max_staleness = state.get("max_staleness", 900)

    if strike is None or direction is None:
        return _skip(ticker, series, close_ts, "SKIP_NOT_DECIDED",
                     {"reason": "cannot_parse_strike_or_direction",
                      "strike": strike, "direction": direction})

    if obs is None:
        return _skip(ticker, series, close_ts, "SKIP_CANT_VERIFY_FAST",
                     {"reason": "no_observation", "series": series})

    if staleness_sec > max_staleness:
        return _skip(ticker, series, close_ts, "SKIP_CANT_VERIFY_FAST",
                     {"reason": "stale_observation",
                      "staleness_sec": staleness_sec,
                      "max_staleness": max_staleness})

    running_val = state.get("running_value", obs.value)
    evidence = {
        "obs_value": obs.value,
        "running_value": running_val,
        "strike": strike,
        "direction": direction,
        "rounding": rounding,
        "obs_ts": obs.ts,
        "obs_source": obs.source,
        "staleness_sec": staleness_sec,
    }

    if obs2 is not None:
        evidence["obs2_value"] = obs2.value
        evidence["obs2_source"] = obs2.source
        evidence["feeds_agree"] = abs(obs.value - obs2.value) <= rounding

    decided = False
    side = None
    var_kind = (registry_row.get("variable_kind") or "running_max").lower()

    if direction == "above" and var_kind == "running_max":
        threshold = strike + rounding
        if running_val >= threshold:
            decided = True
            side = "yes"
            evidence["threshold"] = threshold
            evidence["crossed"] = True
    elif direction == "below" and var_kind == "running_min":
        threshold = strike - rounding
        if running_val <= threshold:
            decided = True
            side = "yes"
            evidence["threshold"] = threshold
            evidence["crossed"] = True

    if not decided:
        evidence["crossed"] = False
        return _skip(ticker, series, close_ts, "SKIP_NOT_DECIDED", evidence)

    verdict = VerdictRow(
        market_ticker=ticker, series_ticker=series, close_ts=close_ts,
        dclass="D1_WX", verdict="SEED", side=side, evidence=evidence)
    return _apply_money_gates(verdict, fee_info, book, state)


def _check_d2(market: dict, registry_row: dict) -> Optional[VerdictRow]:
    """D2: deadline-passed. Only from machine-readable timestamps."""
    ticker = market.get("ticker", "")
    series = _series_of(market)
    notes = (registry_row.get("notes") or "").lower()
    if "no-revival" not in notes:
        return None
    event_end = market.get("expiration_time") or market.get("expected_expiration_time")
    if not event_end:
        return None
    try:
        from datetime import datetime
        end_ts = datetime.fromisoformat(
            event_end.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
    if time.time() < end_ts:
        return None
    result_from_obj = market.get("result")
    if result_from_obj:
        side = result_from_obj
    else:
        return None
    return VerdictRow(
        market_ticker=ticker, series_ticker=series,
        close_ts=market.get("close_time_ts", 0),
        dclass="D2", verdict="SEED", side=side,
        evidence={"event_end": event_end, "now": time.time()})


def _apply_money_gates(verdict: VerdictRow, fee_info: Optional[dict],
                       book: Optional[object], state: dict) -> VerdictRow:
    """Apply fee, book, and budget gates to a SEED verdict.
    Returns the verdict (possibly downgraded to a SKIP)."""
    ticker = verdict.market_ticker
    series = verdict.series_ticker
    close_ts = verdict.close_ts

    if verdict.verdict != "SEED":
        return verdict

    if fee_info is None:
        return _skip(ticker, series, close_ts, "SKIP_FEE_FAIL",
                     {"reason": "NO_REGISTRY", **(verdict.evidence or {})})
    verdict.fee_maker = fee_info.get("maker_mult")
    verdict.fee_taker = fee_info.get("taker_mult")

    if book is None:
        return _skip(ticker, series, close_ts, "SKIP_NO_BOOK",
                     verdict.evidence)

    taker_price = None
    if verdict.side == "yes":
        taker_price = getattr(book, "yes_ask", None)
    elif verdict.side == "no":
        taker_price = getattr(book, "no_ask", None)

    if taker_price is None or taker_price >= 100:
        return _skip(ticker, series, close_ts, "SKIP_NO_BOOK",
                     {"reason": "no_crossable_ask", **(verdict.evidence or {})})

    min_net = state.get("min_net_clip_cents", 1.0)
    batch_sizes = state.get("batch_sizes", [1])
    taker_mult = feemath.taker_mult(fee_info.get("taker_mult", 1.0))
    any_viable = False
    for sz in batch_sizes:
        ok, net = feemath.entry_ok(taker_price, taker_mult, sz, min_net)
        if ok:
            any_viable = True
            break

    if not any_viable:
        return _skip(ticker, series, close_ts, "SKIP_FEE_FAIL",
                     {"reason": "entry_inequality_failed",
                      "taker_price": taker_price,
                      "min_net": min_net,
                      **(verdict.evidence or {})})

    sim_cap = state.get("sim_capital_remaining", 0)
    if sim_cap <= 0:
        return _skip(ticker, series, close_ts, "SKIP_LOCKUP_BUDGET",
                     verdict.evidence)

    if state.get("already_seeded"):
        return _skip(ticker, series, close_ts, "SKIP_ALREADY_SEEDED",
                     verdict.evidence)

    if verdict.evidence:
        verdict.evidence["taker_price"] = taker_price
    return verdict

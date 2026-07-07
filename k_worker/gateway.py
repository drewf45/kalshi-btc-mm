"""Chunk 4 — Gateway walls. evaluate → submit is the ONLY path to the exchange.

Walls (Tier 0, non-negotiable):
- KXBTC15M tickers only
- Favorite side by cost: 95.00-99.00¢ cost band only (Decimal)
- 80.00-94.00¢ H8 probe lane (bounded, separate budget)
- Below 80¢ REJECT (shadow only)
- Flat 1 contract
- One entry per ticker ever, verified against exchange position state
- Never both sides of one ticker
- Maker (post_only) only — no taker path exists
- Live balance re-read inside submit
- Per-hour exposure cap $3.00 (main lane)
- H8 probe budget $2.00/day
"""

import json
import time
import logging
from decimal import Decimal
from typing import Optional, Tuple
from dataclasses import dataclass, field

from . import kalshi, store

log = logging.getLogger("k_worker.gateway")

COST_BAND_LO = Decimal("95.00")
COST_BAND_HI = Decimal("99.00")
COST_BAND_LO_INT = 95
COST_BAND_HI_INT = 99
H8_COST_LO = Decimal("80.00")
H8_COST_HI = Decimal("94.00")
H8_PROBE_BUDGET_PER_DAY = 2.00
H8_MIN_DISTANCE_PCT = 0.0015
H8_MAX_SECS = 60
# 4 windows/hour × max 99¢ cost = $3.96; cap must not structurally ban the 4th window
HOURLY_EXPOSURE_CAP_USD = 4.00
MIN_BALANCE_USD = 5.00

_traded_tickers: set = set()
_hourly_exposure: list = []  # list of (timestamp, dollars_at_risk)


def _load_persisted_state() -> None:
    """Load traded tickers and hourly exposure from store at boot."""
    raw = store.get_state("traded_tickers")
    if raw:
        try:
            _traded_tickers.update(json.loads(raw))
        except Exception:
            pass
    raw = store.get_state("hourly_exposure")
    if raw:
        try:
            _hourly_exposure.extend(json.loads(raw))
        except Exception:
            pass


def _save_traded_tickers() -> None:
    store.set_state("traded_tickers", json.dumps(list(_traded_tickers)))


def _save_hourly_exposure() -> None:
    cutoff = time.time() - 3600
    _hourly_exposure[:] = [(ts, d) for ts, d in _hourly_exposure if ts >= cutoff]
    store.set_state("hourly_exposure", json.dumps(_hourly_exposure))


@dataclass
class EvalResult:
    allowed: bool
    side: Optional[str] = None
    cost_cents: Optional[int] = None
    cost_exact: Optional[float] = None
    rest_fp: Optional[str] = None
    yes_quote_cents: Optional[int] = None
    breakeven_pct: Optional[float] = None
    why_tag: Optional[str] = None
    reject_code: Optional[str] = None
    reject_reason: Optional[str] = None
    lane: str = "main"
    spot_price: Optional[float] = None
    boundary_lo: Optional[float] = None
    boundary_hi: Optional[float] = None
    distance: Optional[float] = None
    distance_pct: Optional[float] = None


def _favorite_side(book: kalshi.Book) -> Tuple[Optional[str], Optional[Decimal], Optional[int], Optional[str]]:
    """Determine favorite side. Returns (side, cost_decimal_cents, yes_quote_cents, fp_str).
    cost_decimal_cents is the exact Decimal cost in cents (e.g. Decimal('99.40'))."""
    prices = []
    if book.yes_bid is not None:
        if book.yes_bid_fp is not None:
            cost_d = Decimal(book.yes_bid_fp) * 100
        else:
            cost_d = Decimal(book.yes_bid)
        prices.append(("yes", cost_d, book.yes_bid, book.yes_bid_fp))
    if book.no_bid is not None:
        if book.no_bid_fp is not None:
            cost_d = Decimal(book.no_bid_fp) * 100
        else:
            cost_d = Decimal(book.no_bid)
        prices.append(("no", cost_d, 100 - book.no_bid, book.no_bid_fp))
    if not prices:
        return None, None, None, None
    best = max(prices, key=lambda x: x[1])
    return best[0], best[1], best[2], best[3]


def _hourly_exposure_usd() -> float:
    """Sum of dollars at risk in the last hour."""
    cutoff = time.time() - 3600
    return sum(d for ts, d in _hourly_exposure if ts >= cutoff)


def evaluate(ticker: str, book: kalshi.Book, secs_to_expiry: float,
             cash_usd: float, spot: Optional[float] = None,
             boundary_lo: Optional[float] = None,
             boundary_hi: Optional[float] = None) -> EvalResult:
    """Evaluate whether this market qualifies for entry.
    Returns EvalResult with allowed=True if all walls pass."""

    # Wall 1: KXBTC15M only
    if not ticker.startswith("KXBTC15M"):
        return EvalResult(
            allowed=False, reject_code="WRONG_FAMILY",
            reject_reason=f"Not KXBTC15M: {ticker}",
        )

    # Determine favorite side
    side, cost_d, yes_quote, fp_str = _favorite_side(book)
    if side is None or cost_d is None:
        return EvalResult(
            allowed=False, reject_code="NO_BOOK",
            reject_reason="No orderbook data",
            why_tag="SKIP_NO_BOOK",
        )

    cost_int = int(cost_d)
    cost_float = float(cost_d)
    breakeven = cost_float / 100.0

    # Compute distance if spot available
    dist, dist_pct = None, None
    if spot is not None and boundary_lo is not None and boundary_hi is not None:
        if side == "yes":
            dist = spot - boundary_hi
        else:
            dist = boundary_lo - spot
        dist_pct = abs(dist) / spot if spot > 0 else 0.0

    base = EvalResult(
        allowed=False, side=side, cost_cents=cost_int,
        cost_exact=cost_float, rest_fp=fp_str,
        yes_quote_cents=yes_quote, breakeven_pct=breakeven,
        spot_price=spot, boundary_lo=boundary_lo, boundary_hi=boundary_hi,
        distance=dist, distance_pct=dist_pct,
    )

    # Check H8 probe lane first (cost 80-94)
    if H8_COST_LO <= cost_d <= H8_COST_HI:
        return _evaluate_h8_probe(base, cost_d, secs_to_expiry, cash_usd,
                                  ticker, spot, dist_pct)

    # Wall 2: Main lane cost band 95.00-99.00¢
    if cost_d < COST_BAND_LO:
        base.reject_code = "OUT_OF_BAND_COST"
        base.reject_reason = f"cost={cost_float}¢ < {COST_BAND_LO}¢ (shadow only)"
        base.why_tag = f"SKIP_OOB_{cost_float}c_T-{int(secs_to_expiry)}"
        return base
    if cost_d > COST_BAND_HI:
        base.reject_code = "OUT_OF_BAND_COST"
        base.reject_reason = f"cost={cost_float}¢ > {COST_BAND_HI}¢"
        base.why_tag = f"SKIP_OOB_{cost_float}c_T-{int(secs_to_expiry)}"
        return base

    # Wall 3: One entry per ticker
    if ticker in _traded_tickers:
        base.reject_code = "SECOND_ENTRY"
        base.reject_reason = f"Already traded {ticker}"
        base.why_tag = "SKIP_SECOND_ENTRY"
        return base

    # Wall 4: Insufficient balance
    cost_usd = cost_float / 100.0
    if cash_usd < cost_usd:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"cash=${cash_usd:.2f} < cost=${cost_usd:.2f}"
        base.why_tag = "SKIP_BALANCE"
        return base

    if cash_usd < MIN_BALANCE_USD:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"cash=${cash_usd:.2f} < floor=${MIN_BALANCE_USD:.2f}"
        base.why_tag = "SKIP_BALANCE"
        return base

    # Wall 5: Hourly exposure cap
    current_exposure = _hourly_exposure_usd()
    if current_exposure + cost_usd > HOURLY_EXPOSURE_CAP_USD:
        base.reject_code = "HOURLY_CAP"
        base.reject_reason = (
            f"hourly exposure=${current_exposure:.2f} + ${cost_usd:.2f} "
            f"> cap=${HOURLY_EXPOSURE_CAP_USD:.2f}"
        )
        base.why_tag = "SKIP_HOURLY_CAP"
        return base

    why_tag = f"FAV_{cost_float}c_T-{int(secs_to_expiry)}"
    base.allowed = True
    base.why_tag = why_tag
    base.reject_code = None
    base.reject_reason = None
    return base


def _evaluate_h8_probe(base: EvalResult, cost_d: Decimal, secs_to_expiry: float,
                        cash_usd: float, ticker: str,
                        spot: Optional[float], dist_pct: Optional[float]) -> EvalResult:
    """Evaluate H8 probe lane qualification."""
    base.lane = "h8_probe"
    cost_float = float(cost_d)

    # All four conditions required
    if spot is None:
        base.reject_code = "H8_NO_SPOT"
        base.reject_reason = "Spot price unavailable"
        base.why_tag = "SKIP_H8_UNQUALIFIED"
        return base

    if dist_pct is None or dist_pct < H8_MIN_DISTANCE_PCT:
        base.reject_code = "H8_DISTANCE"
        base.reject_reason = f"distance_pct={dist_pct or 0:.4%} < {H8_MIN_DISTANCE_PCT:.2%}"
        base.why_tag = "SKIP_H8_UNQUALIFIED"
        return base

    if secs_to_expiry > H8_MAX_SECS:
        base.reject_code = "H8_TIME"
        base.reject_reason = f"secs_to_expiry={secs_to_expiry:.0f} > {H8_MAX_SECS}"
        base.why_tag = "SKIP_H8_UNQUALIFIED"
        return base

    # Probe kill: 3 losses before any win
    lifetime = store.query_h8_probe_lifetime()
    if lifetime["losses"] >= 3 and lifetime["wins"] == 0:
        base.reject_code = "H8_PROBE_KILLED"
        base.reject_reason = f"Probe killed: {lifetime['losses']} losses, 0 wins"
        base.why_tag = "SKIP_H8_KILLED"
        return base

    # Budget wall
    daily = store.query_h8_probe_daily()
    cost_usd = cost_float / 100.0
    if daily["at_risk"] + cost_usd > H8_PROBE_BUDGET_PER_DAY:
        base.reject_code = "H8_BUDGET"
        base.reject_reason = f"probe budget ${daily['at_risk']:.2f} + ${cost_usd:.2f} > ${H8_PROBE_BUDGET_PER_DAY:.2f}"
        base.why_tag = "SKIP_H8_BUDGET"
        return base

    if ticker in _traded_tickers:
        base.reject_code = "SECOND_ENTRY"
        base.reject_reason = f"Already traded {ticker}"
        base.why_tag = "SKIP_SECOND_ENTRY"
        return base

    if cash_usd < cost_usd or cash_usd < MIN_BALANCE_USD:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"cash=${cash_usd:.2f}"
        base.why_tag = "SKIP_BALANCE"
        return base

    base.allowed = True
    dp = dist_pct * 100 if dist_pct else 0
    base.why_tag = f"H8PROBE_{cost_float}c_D{dp:.2f}_T-{int(secs_to_expiry)}"
    base.reject_code = None
    base.reject_reason = None
    return base


def submit(client: kalshi.KalshiClient, ticker: str,
           eval_result: EvalResult, close_ts: int,
           secs_to_expiry: float, book: kalshi.Book) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Submit an order through the gateway. Returns (order_id, row_id, skip_why).
    This is the ONLY path to the exchange."""
    if not eval_result.allowed:
        raise ValueError("Cannot submit a rejected evaluation")

    # Re-read balance inside submit
    cash, _ = kalshi.get_balance(client)
    cost_usd = (eval_result.cost_exact or eval_result.cost_cents) / 100.0
    if cash is None or cash < cost_usd:
        log.warning(f"[GATEWAY] Balance re-check failed: cash=${cash}")
        row_id = store.insert_row(store.SurfaceRow(
            market_ticker=ticker,
            decision_ts=time.time(),
            action="SKIP",
            seconds_to_expiry=secs_to_expiry,
            yes_quote_cents=eval_result.yes_quote_cents,
            side=eval_result.side,
            cost_per_contract_cents=eval_result.cost_exact or eval_result.cost_cents,
            breakeven_pct=eval_result.breakeven_pct,
            skip_reason="INSUFFICIENT_BALANCE_RECHECK",
            why_tag="SKIP_BALANCE",
            lane=eval_result.lane,
            env="live-observed",
        ))
        return None, row_id, "SKIP_BALANCE"

    # Verify no existing position on exchange
    pos = kalshi.position_for_market(client, ticker)
    if abs(pos) > 0:
        _traded_tickers.add(ticker)
        _save_traded_tickers()
        log.warning(f"[GATEWAY] Exchange position check: already hold {pos}ct on {ticker}")
        row_id = store.insert_row(store.SurfaceRow(
            market_ticker=ticker,
            decision_ts=time.time(),
            action="SKIP",
            seconds_to_expiry=secs_to_expiry,
            yes_quote_cents=eval_result.yes_quote_cents,
            side=eval_result.side,
            cost_per_contract_cents=eval_result.cost_exact or eval_result.cost_cents,
            breakeven_pct=eval_result.breakeven_pct,
            skip_reason="BOTH_SIDES_OR_EXISTING",
            why_tag="SKIP_SECOND_ENTRY",
            lane=eval_result.lane,
            env="live-observed",
        ))
        return None, row_id, "SKIP_SECOND_ENTRY"

    # Rest price: exact fp from book (true touch)
    if eval_result.side == "yes":
        rest_price_int = book.yes_bid
        rest_fp = book.yes_bid_fp
    else:
        rest_price_int = book.no_bid
        rest_fp = book.no_bid_fp

    if rest_price_int is None:
        log.warning(f"[GATEWAY] Rest price gone")
        row_id = store.insert_row(store.SurfaceRow(
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
            skip_reason="REST_PRICE_GONE",
            why_tag="SKIP_REST_PRICE_GONE",
            lane=eval_result.lane,
            env="live-observed",
        ))
        return None, row_id, "SKIP_REST_PRICE_GONE"

    # Verify rest price is within the lane's band
    if rest_fp is not None:
        rest_cost_d = Decimal(rest_fp) * 100
    else:
        rest_cost_d = Decimal(rest_price_int)
    if eval_result.lane == "main":
        if not (COST_BAND_LO <= rest_cost_d <= COST_BAND_HI):
            log.warning(f"[GATEWAY] Rest price {rest_cost_d}¢ outside main band")
            row_id = store.insert_row(store.SurfaceRow(
                market_ticker=ticker, decision_ts=time.time(), action="SKIP",
                seconds_to_expiry=secs_to_expiry, side=eval_result.side,
                cost_per_contract_cents=float(rest_cost_d),
                skip_reason="REST_PRICE_OOB", why_tag="SKIP_REST_OOB",
                lane=eval_result.lane, env="live-observed",
            ))
            return None, row_id, "SKIP_REST_PRICE_OOB"
    elif eval_result.lane == "h8_probe":
        if not (H8_COST_LO <= rest_cost_d <= H8_COST_HI):
            log.warning(f"[GATEWAY] Rest price {rest_cost_d}¢ outside H8 band")
            row_id = store.insert_row(store.SurfaceRow(
                market_ticker=ticker, decision_ts=time.time(), action="SKIP",
                seconds_to_expiry=secs_to_expiry, side=eval_result.side,
                cost_per_contract_cents=float(rest_cost_d),
                skip_reason="REST_PRICE_OOB", why_tag="SKIP_H8_REST_OOB",
                lane=eval_result.lane, env="live-observed",
            ))
            return None, row_id, "SKIP_REST_PRICE_OOB"

    expiry_ts = close_ts - 10

    spread = None
    if eval_result.side == "yes" and book.yes_bid is not None and book.yes_ask is not None:
        spread = book.yes_ask - book.yes_bid
    elif eval_result.side == "no" and book.no_bid is not None and book.no_ask is not None:
        spread = book.no_ask - book.no_bid

    depth = book.yes_bid_qty if eval_result.side == "yes" else book.no_bid_qty
    row_id = store.insert_row(store.SurfaceRow(
        market_ticker=ticker,
        decision_ts=time.time(),
        action="ENTER",
        seconds_to_expiry=secs_to_expiry,
        yes_quote_cents=eval_result.yes_quote_cents,
        side=eval_result.side,
        cost_per_contract_cents=eval_result.cost_exact or eval_result.cost_cents,
        breakeven_pct=eval_result.breakeven_pct,
        book_depth_at_touch=depth,
        yes_ask_cents=book.yes_ask,
        no_ask_cents=book.no_ask,
        spread_cents=spread,
        why_tag=eval_result.why_tag,
        order_type="maker",
        contracts=1,
        dollars_at_risk=cost_usd,
        lane=eval_result.lane,
        spot_price=eval_result.spot_price,
        boundary_lo=eval_result.boundary_lo,
        boundary_hi=eval_result.boundary_hi,
        distance=eval_result.distance,
        distance_pct=eval_result.distance_pct,
        env="live-traded",
    ))

    try:
        order_id, order_resp = kalshi.place_order_maker(
            client, ticker, eval_result.side,
            rest_price_int, count=1, expiration_ts=expiry_ts,
            v2_price_str=rest_fp,
        )
        store.update_order_id(row_id, order_id)
        _traded_tickers.add(ticker)
        _save_traded_tickers()
        if eval_result.lane == "main":
            _hourly_exposure.append((time.time(), cost_usd))
            _save_hourly_exposure()
        log.warning(
            f"[GATEWAY] ORDER PLACED {ticker} {eval_result.side.upper()} "
            f"cost={eval_result.cost_exact}¢ rest@{rest_fp or rest_price_int} "
            f"why={eval_result.why_tag} oid={order_id}"
        )
        return order_id, row_id, None
    except RuntimeError as e:
        err = str(e)
        if "HTTP 4" in err and "HTTP 429" not in err:
            log.error(f"[GATEWAY] Order REJECTED (definitive): {e}")
            store.update_settlement(row_id, "order_rejected", 0.0, time.time())
            return None, row_id, "SKIP_ORDER_REJECTED"
        log.error(f"[GATEWAY] Order AMBIGUOUS failure: {e}")
        store.update_settlement(row_id, "order_ambiguous", 0.0, time.time())
        return None, row_id, "SKIP_ORDER_AMBIGUOUS"
    except Exception as e:
        log.error(f"[GATEWAY] Order AMBIGUOUS failure: {e}")
        store.update_settlement(row_id, "order_ambiguous", 0.0, time.time())
        return None, row_id, "SKIP_ORDER_AMBIGUOUS"


def reprice(client: kalshi.KalshiClient, ticker: str,
            eval_result: EvalResult, old_order_id: str,
            new_book: kalshi.Book, close_ts: int) -> Tuple[Optional[str], Optional[int]]:
    """The ONLY legal reprice path. Re-checks balance and band; accounts exposure delta."""
    new_side, new_cost_d, new_yq, new_fp = _favorite_side(new_book)
    if new_side != eval_result.side or new_cost_d is None:
        return None, None
    if not (COST_BAND_LO <= new_cost_d <= COST_BAND_HI):
        return None, None
    cash, _ = kalshi.get_balance(client)
    new_cost_float = float(new_cost_d)
    if cash is None or cash < new_cost_float / 100.0:
        return None, None
    kalshi.cancel_order(client, old_order_id)
    if eval_result.side == "yes":
        rest_price = new_book.yes_bid
        rest_fp = new_book.yes_bid_fp
    else:
        rest_price = new_book.no_bid
        rest_fp = new_book.no_bid_fp
    if rest_price is None:
        return None, None
    # Verify rest price in band
    if rest_fp is not None:
        rest_cost_d = Decimal(rest_fp) * 100
    else:
        rest_cost_d = Decimal(rest_price)
    if not (COST_BAND_LO <= rest_cost_d <= COST_BAND_HI):
        return None, None
    oid, _ = kalshi.place_order_maker(client, ticker, eval_result.side, rest_price,
                                       count=1, expiration_ts=close_ts - 10,
                                       v2_price_str=rest_fp)
    log.warning(f"[GATEWAY] REPRICE {ticker} → {new_cost_float}c rest@{rest_fp or rest_price} oid={oid}")
    return oid, int(new_cost_d)


def mark_traded(ticker: str) -> None:
    """Mark a ticker as traded (used when detecting existing positions)."""
    _traded_tickers.add(ticker)
    _save_traded_tickers()


def is_traded(ticker: str) -> bool:
    return ticker in _traded_tickers


def reset_for_new_market() -> None:
    """Called on market roll — prune old hourly exposure entries."""
    _save_hourly_exposure()

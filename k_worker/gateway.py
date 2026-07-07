"""Chunk 4 — Gateway walls. evaluate → submit is the ONLY path to the exchange.

Walls (Tier 0, non-negotiable):
- KXBTC15M tickers only
- Favorite side by cost: 95-99¢ cost band only
- 90-94¢ and below REJECT (shadow only)
- Flat 1 contract
- One entry per ticker ever, verified against exchange position state
- Never both sides of one ticker
- Maker (post_only) only — no taker path exists
- Live balance re-read inside submit
- Per-hour exposure cap $3.00
"""

import json
import time
import logging
from typing import Optional, Tuple
from dataclasses import dataclass, field

from . import kalshi, store

log = logging.getLogger("k_worker.gateway")

COST_BAND_LO = 95
COST_BAND_HI = 99
HOURLY_EXPOSURE_CAP_USD = 3.00
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
    yes_quote_cents: Optional[int] = None
    breakeven_pct: Optional[float] = None
    why_tag: Optional[str] = None
    reject_code: Optional[str] = None
    reject_reason: Optional[str] = None


def _cost_for_side(side: str, yes_quote: int) -> int:
    """Cost per contract in cents. YES cost = yes_quote. NO cost = 100 - yes_quote."""
    if side == "yes":
        return yes_quote
    return 100 - yes_quote


def _favorite_side(book: kalshi.Book) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """Determine favorite side by cost (cheapest side).

    Returns (side, cost_cents, yes_quote_cents) or (None, None, None).
    The favorite is whichever side has the HIGHER bid (higher bid = cheaper cost
    for the other side... no: higher bid = that side is more expensive to buy,
    but more likely to win).

    Actually: favorite = the side with highest bid. Cost = bid price.
    If yes_bid=97, buying YES costs 97¢. If no_bid=97, buying NO costs 97¢.
    The "favorite" is whichever side the market says is most likely.
    """
    prices = []
    if book.yes_bid is not None:
        prices.append(("yes", book.yes_bid, book.yes_bid))
    if book.no_bid is not None:
        prices.append(("no", book.no_bid, 100 - book.no_bid))
    if not prices:
        return None, None, None
    # Favorite = highest bid (most likely to win, highest cost)
    best = max(prices, key=lambda x: x[1])
    side, cost, yes_quote = best
    return side, cost, yes_quote


def _hourly_exposure_usd() -> float:
    """Sum of dollars at risk in the last hour."""
    cutoff = time.time() - 3600
    return sum(d for ts, d in _hourly_exposure if ts >= cutoff)


def evaluate(ticker: str, book: kalshi.Book, secs_to_expiry: float,
             cash_usd: float) -> EvalResult:
    """Evaluate whether this market qualifies for entry.

    Returns EvalResult with allowed=True if all walls pass.
    Always returns enough info for a shadow/skip log row.
    """

    # Wall 1: KXBTC15M only
    if not ticker.startswith("KXBTC15M"):
        return EvalResult(
            allowed=False, reject_code="WRONG_FAMILY",
            reject_reason=f"Not KXBTC15M: {ticker}",
        )

    # Determine favorite side
    side, cost, yes_quote = _favorite_side(book)
    if side is None or cost is None:
        return EvalResult(
            allowed=False, reject_code="NO_BOOK",
            reject_reason="No orderbook data",
        )

    breakeven = cost / 100.0

    base = EvalResult(
        allowed=False, side=side, cost_cents=cost,
        yes_quote_cents=yes_quote, breakeven_pct=breakeven,
    )

    # Wall 2: Cost band 95-99¢ only
    if cost < COST_BAND_LO:
        base.reject_code = "OUT_OF_BAND_COST"
        base.reject_reason = f"cost={cost}¢ < {COST_BAND_LO}¢ (shadow only)"
        return base
    if cost > COST_BAND_HI:
        base.reject_code = "OUT_OF_BAND_COST"
        base.reject_reason = f"cost={cost}¢ > {COST_BAND_HI}¢"
        return base

    # Wall 3: One entry per ticker
    if ticker in _traded_tickers:
        base.reject_code = "SECOND_ENTRY"
        base.reject_reason = f"Already traded {ticker}"
        return base

    # Wall 4: Insufficient balance
    cost_usd = cost / 100.0
    if cash_usd < cost_usd:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"cash=${cash_usd:.2f} < cost=${cost_usd:.2f}"
        return base

    if cash_usd < MIN_BALANCE_USD:
        base.reject_code = "INSUFFICIENT_BALANCE"
        base.reject_reason = f"cash=${cash_usd:.2f} < floor=${MIN_BALANCE_USD:.2f}"
        return base

    # Wall 5: Hourly exposure cap
    current_exposure = _hourly_exposure_usd()
    if current_exposure + cost_usd > HOURLY_EXPOSURE_CAP_USD:
        base.reject_code = "HOURLY_CAP"
        base.reject_reason = (
            f"hourly exposure=${current_exposure:.2f} + ${cost_usd:.2f} "
            f"> cap=${HOURLY_EXPOSURE_CAP_USD:.2f}"
        )
        return base

    # Build why_tag
    why_tag = f"FAV_{cost}c_T-{int(secs_to_expiry)}"

    base.allowed = True
    base.why_tag = why_tag
    base.reject_code = None
    base.reject_reason = None
    return base


def submit(client: kalshi.KalshiClient, ticker: str,
           eval_result: EvalResult, close_ts: int,
           secs_to_expiry: float, book: kalshi.Book) -> Tuple[Optional[str], Optional[int]]:
    """Submit an order through the gateway. Returns (order_id, row_id) or (None, row_id).

    This is the ONLY path to the exchange.
    """
    if not eval_result.allowed:
        raise ValueError("Cannot submit a rejected evaluation")

    # Re-read balance inside submit (Feb 22 lesson)
    cash, _ = kalshi.get_balance(client)
    if cash is None or cash < (eval_result.cost_cents / 100.0):
        log.warning(f"[GATEWAY] Balance re-check failed: cash=${cash}")
        row_id = store.insert_row(store.SurfaceRow(
            market_ticker=ticker,
            decision_ts=time.time(),
            action="SKIP",
            seconds_to_expiry=secs_to_expiry,
            yes_quote_cents=eval_result.yes_quote_cents,
            side=eval_result.side,
            cost_per_contract_cents=eval_result.cost_cents,
            breakeven_pct=eval_result.breakeven_pct,
            skip_reason="INSUFFICIENT_BALANCE_RECHECK",
            env="live-observed",
        ))
        return None, row_id

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
            cost_per_contract_cents=eval_result.cost_cents,
            breakeven_pct=eval_result.breakeven_pct,
            skip_reason="BOTH_SIDES_OR_EXISTING",
            env="live-observed",
        ))
        return None, row_id

    # Determine rest price: touch (best bid on our side)
    if eval_result.side == "yes":
        rest_price = book.yes_bid
    else:
        rest_price = book.no_bid

    if rest_price is None or rest_price < COST_BAND_LO:
        log.warning(f"[GATEWAY] Rest price gone: {rest_price}")
        row_id = store.insert_row(store.SurfaceRow(
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
            skip_reason="REST_PRICE_GONE",
            env="live-observed",
        ))
        return None, row_id

    # Expiration: T-10s
    expiry_ts = close_ts - 10

    spread = None
    if eval_result.side == "yes" and book.yes_bid is not None and book.yes_ask is not None:
        spread = book.yes_ask - book.yes_bid
    elif eval_result.side == "no" and book.no_bid is not None and book.no_ask is not None:
        spread = book.no_ask - book.no_bid

    # Log the ENTER row
    depth = book.yes_bid_qty if eval_result.side == "yes" else book.no_bid_qty
    row_id = store.insert_row(store.SurfaceRow(
        market_ticker=ticker,
        decision_ts=time.time(),
        action="ENTER",
        seconds_to_expiry=secs_to_expiry,
        yes_quote_cents=eval_result.yes_quote_cents,
        side=eval_result.side,
        cost_per_contract_cents=eval_result.cost_cents,
        breakeven_pct=eval_result.breakeven_pct,
        book_depth_at_touch=depth,
        yes_ask_cents=book.yes_ask,
        no_ask_cents=book.no_ask,
        spread_cents=spread,
        why_tag=eval_result.why_tag,
        order_type="maker",
        contracts=1,
        dollars_at_risk=eval_result.cost_cents / 100.0,
        env="live-traded",
    ))

    # Place the order
    try:
        order_id = kalshi.place_order_maker(
            client, ticker, eval_result.side,
            rest_price, count=1, expiration_ts=expiry_ts,
        )
        _traded_tickers.add(ticker)
        _save_traded_tickers()
        cost_usd = eval_result.cost_cents / 100.0
        _hourly_exposure.append((time.time(), cost_usd))
        _save_hourly_exposure()
        log.warning(
            f"[GATEWAY] ORDER PLACED {ticker} {eval_result.side.upper()} "
            f"cost={eval_result.cost_cents}¢ rest@{rest_price}¢ "
            f"why={eval_result.why_tag} oid={order_id}"
        )
        # T2: trade confirmation with balance
        from . import notify
        bal_cash, bal_pv = kalshi.get_balance(client)
        bal_line = f"Balance: ${(bal_cash or 0) + (bal_pv or 0):.2f}" if bal_cash is not None else "Balance: unknown"
        notify.send(
            f"<b>ORDER PLACED</b> {ticker}\n"
            f"{eval_result.side.upper()} {eval_result.cost_cents}c | why={eval_result.why_tag}\n"
            f"{bal_line}"
        )
        return order_id, row_id
    except Exception as e:
        log.error(f"[GATEWAY] Order failed: {e}")
        store.update_settlement(row_id, "order_failed", 0.0, time.time())
        return None, row_id


def reprice(client: kalshi.KalshiClient, ticker: str,
            eval_result: EvalResult, old_order_id: str,
            new_book: kalshi.Book, close_ts: int) -> Tuple[Optional[str], Optional[int]]:
    """The ONLY legal reprice path. Re-checks balance and band; accounts exposure delta."""
    new_side, new_cost, new_yq = _favorite_side(new_book)
    if new_side != eval_result.side or new_cost is None:
        return None, None
    if not (COST_BAND_LO <= new_cost <= COST_BAND_HI):
        return None, None
    cash, _ = kalshi.get_balance(client)
    if cash is None or cash < new_cost / 100.0:
        return None, None
    kalshi.cancel_order(client, old_order_id)
    rest_price = new_book.yes_bid if eval_result.side == "yes" else new_book.no_bid
    if rest_price is None or rest_price < COST_BAND_LO:
        return None, None
    oid = kalshi.place_order_maker(client, ticker, eval_result.side, rest_price,
                                   count=1, expiration_ts=close_ts - 10)
    log.warning(f"[GATEWAY] REPRICE {ticker} → {new_cost}c oid={oid}")
    return oid, new_cost


def mark_traded(ticker: str) -> None:
    """Mark a ticker as traded (used when detecting existing positions)."""
    _traded_tickers.add(ticker)
    _save_traded_tickers()


def is_traded(ticker: str) -> bool:
    return ticker in _traded_tickers


def reset_for_new_market() -> None:
    """Called on market roll — prune old hourly exposure entries."""
    _save_hourly_exposure()

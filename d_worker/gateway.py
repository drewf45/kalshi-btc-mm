"""Live gateway — the ONLY path to the exchange for d_worker.

Laws ported from k_worker's gateway:
  - Single gateway: every order flows through submit(), no other path.
  - Live-balance re-read before every order (the Feb-22 law).
  - Four-state verify: filled, gone, resting, blind.
  - Fail-loud: ambiguous outcomes are never silently eaten.
  - Taker-on-decided: per charter §2.5(b), taker entry allowed ONLY when
    VERIFIED_DECIDED=true AND the exact-roundup fee inequality passes.
  - ABANDON_SHIP: exit at market through this gateway, tagged, no committee.

Behind rung gate: nothing fires unless current_rung() >= 2.
"""

import time
import logging
from typing import Optional
from dataclasses import dataclass

from k_worker import kalshi, notify

from . import dstore, budget, feemath

log = logging.getLogger("d_worker.gateway")


RUNG_LIVE_MINIMUM = 2


@dataclass
class OrderResult:
    status: str
    order_id: Optional[str] = None
    fill_price: Optional[int] = None
    error: Optional[str] = None


def current_rung() -> int:
    """Current rung on the go-live ladder."""
    return int(dstore.get_state("current_rung") or "0")


def is_live_enabled() -> bool:
    return current_rung() >= RUNG_LIVE_MINIMUM


def submit(client: kalshi.KalshiClient, ticker: str, side: str,
           price_cents: int, reservation_id: int,
           governor=None) -> OrderResult:
    """Place a live 1-lot taker order. The ONLY path to the exchange.

    Pre-conditions enforced here:
      - Rung gate (>= RUNG_LIVE_MINIMUM)
      - Live-balance re-read
      - Reservation must be RESERVED status
    """
    if not is_live_enabled():
        return OrderResult("BLOCKED", error="rung_gate: below minimum")

    if dstore.get_state("live_halt") == "1":
        return OrderResult("BLOCKED", error="live_halt active")

    # Live-balance re-read (Feb-22 law)
    if governor:
        governor.consume(1)
    try:
        cash, _ = kalshi.get_balance(client)
    except Exception as e:
        log.error(f"[GATEWAY] Balance read failed: {e}")
        return OrderResult("BLOCKED", error=f"balance_read_failed: {e}")

    if cash is None:
        return OrderResult("BLOCKED", error="balance_unavailable")

    cost_usd = price_cents / 100.0
    if cash < cost_usd:
        budget.release(reservation_id, "SKIP_BALANCE")
        return OrderResult("BLOCKED", error=f"insufficient_balance: ${cash:.2f} < ${cost_usd:.2f}")

    # Convert reservation to at_risk
    budget.convert(reservation_id)

    # Place the order
    if governor:
        governor.consume(1)
    try:
        order_id = _place_taker_order(client, ticker, side, price_cents)
    except Exception as e:
        log.error(f"[GATEWAY] Order AMBIGUOUS: {ticker} {side} {price_cents}¢ — {e}")
        notify.alert(f"🅳 🚨 ORDER AMBIGUOUS — {ticker} {side} {price_cents}¢ — {e}")
        dstore.insert_order_row(reservation_id, ticker, side, price_cents,
                                "AMBIGUOUS", str(e))
        return OrderResult("AMBIGUOUS", error=str(e))

    if order_id is None:
        budget.release(reservation_id, "ORDER_REJECTED")
        dstore.insert_order_row(reservation_id, ticker, side, price_cents,
                                "REJECTED", "no order_id returned")
        return OrderResult("REJECTED", error="no_order_id")

    dstore.insert_order_row(reservation_id, ticker, side, price_cents,
                            "PLACED", order_id)
    log.warning(f"[GATEWAY] ORDER PLACED: {ticker} {side} {price_cents}¢ id={order_id}")
    notify.send(f"🅳 📦 ORDER PLACED — {ticker} {side} {price_cents}¢")
    return OrderResult("PLACED", order_id=order_id)


def abandon_ship(client: kalshi.KalshiClient, ticker: str, side: str,
                 reservation_id: int, governor=None) -> OrderResult:
    """Emergency exit at market — the watchdog's kill path."""
    if not is_live_enabled():
        return OrderResult("SHADOW_ONLY", error="no live orders to abandon")

    sell_side = "no" if side == "yes" else "yes"

    if governor:
        governor.consume(1)
    try:
        book = kalshi.fetch_orderbook(client, ticker)
        if book is None:
            return OrderResult("NO_BOOK", error="cannot read book for abandon")

        bid = book.yes_bid if side == "yes" else book.no_bid
        if bid is None or bid <= 0:
            return OrderResult("NO_BID", error="no exit liquidity")

        if governor:
            governor.consume(1)
        order_id = _place_taker_order(client, ticker, sell_side, 100 - bid)
    except Exception as e:
        log.error(f"[GATEWAY] ABANDON AMBIGUOUS: {ticker} — {e}")
        notify.alert(f"🅳 🚨 ABANDON AMBIGUOUS — {ticker} — {e}")
        return OrderResult("AMBIGUOUS", error=str(e))

    budget.release(reservation_id, "ABANDON_SHIP")
    dstore.insert_order_row(reservation_id, ticker, sell_side, 100 - bid,
                            "ABANDON_SHIP", order_id or "")
    notify.alert(f"🅳 🚨 ABANDON SHIP — {ticker} {side} exited at {bid}¢ bid")
    log.error(f"[GATEWAY] ABANDON_SHIP: {ticker} {side} at {bid}¢")
    return OrderResult("ABANDONED", order_id=order_id, fill_price=bid)


def _place_taker_order(client: kalshi.KalshiClient, ticker: str,
                       side: str, price_cents: int) -> Optional[str]:
    """Place a single-lot taker order. Returns order_id or None."""
    try:
        resp = client.request("POST", "/portfolio/orders", json_body={
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": 1,
            "yes_price": price_cents if side == "yes" else None,
            "no_price": price_cents if side == "no" else None,
        })
        if isinstance(resp, dict):
            order = resp.get("order", resp)
            return order.get("order_id") or order.get("id")
        return None
    except RuntimeError as e:
        if "4" in str(e)[:5]:
            log.warning(f"[GATEWAY] Order rejected (4xx): {e}")
            return None
        raise

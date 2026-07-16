# f_worker/lib/order_v2.py
# WO-F4: the PROVEN V2 order grammar, ported from k_worker/kalshi.py:668-714
# (place_order_maker) — the body that placed hundreds of live fills. f_worker's old
# body (POST /portfolio/orders, side yes/no, yes_price/no_price int cents) is a pre-V2
# fossil the live exchange would reject. Crypto-free so every intent→wire mapping is
# unit-tested against the proven engine's exact output.
#
# V2 quotes the YES leg ONLY, as bid/ask, price = fixed-point DOLLAR string, count =
# STRING. The proven engine was BUY-ONLY (maker); flipdesk also SELLS (flips) and
# CROSSES (salvage/flat), which are the same YES-bid/ask logic with the sell side and
# post_only=False. That taker path is the one audited unknown — same body, prove it in
# demo/probe before trusting it (WO-F4 §3).
#
#   intent               v2_side   yes-terms price
#   buy  YES @p          bid       p          (proven: side yes -> bid p$)
#   buy  NO  @p          ask       100 - p    (proven: side no  -> ask (100-p)$)
#   sell YES @q          ask       q          (flip a held YES leg)
#   sell NO  @q          bid       100 - q    (flip a held NO leg)

from decimal import Decimal
from typing import Any, Dict, Optional

EVENTS_ORDERS_PATH = "/portfolio/events/orders"     # place ALWAYS posts here (proven)


def yes_bidask(action: str, side: str, price_cents: int):
    """Collapse (buy/sell, yes/no, cents) to (v2_side, yes_terms_price_cents)."""
    a, s = action.lower(), side.lower()
    if a == "buy" and s == "yes":
        return "bid", int(price_cents)
    if a == "buy" and s == "no":
        return "ask", 100 - int(price_cents)
    if a == "sell" and s == "yes":
        return "ask", int(price_cents)
    if a == "sell" and s == "no":
        return "bid", 100 - int(price_cents)
    raise ValueError(f"cannot translate action={action} side={side}")


def _yes_price_str(side: str, price_cents: int, v2_price_str: Optional[str]) -> str:
    """YES-terms price as a dollar string. With v2_price_str (the exact orderbook_fp
    string for OUR side) we rest at the true subpenny touch, converting a NO string to
    YES terms via 1 - x exactly (proven uses Decimal, preserving precision)."""
    if v2_price_str is not None:
        if side.lower() == "yes":
            return v2_price_str
        return str(Decimal("1") - Decimal(v2_price_str))
    # cent-derived: match the proven engine's .2f formatting exactly
    if side.lower() == "yes":
        return f"{int(price_cents) / 100:.2f}"
    return f"{(100 - int(price_cents)) / 100:.2f}"


def build_v2_order(ticker: str, action: str, side: str, price_cents: int, count: int,
                   post_only: bool, client_order_id: str,
                   expiration_ts: Optional[int] = None,
                   v2_price_str: Optional[str] = None,
                   self_trade: str = "taker_at_cross") -> Dict[str, Any]:
    """The V2 events-order body (exact field set from place_order_maker). post_only=True
    is maker (entries/flips); False is taker (salvage/flat). expiration_ts (W4b) is set
    only when in the future — the caller guards against a past expiry."""
    v2_side, _ = yes_bidask(action, side, price_cents)
    body: Dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": v2_side,                                   # bid | ask, YES contract
        "count": str(max(1, int(count))),                  # STRING
        "price": _yes_price_str(side, price_cents, v2_price_str),
        "time_in_force": "good_till_canceled",
        "post_only": bool(post_only),
        "self_trade_prevention_type": self_trade,
    }
    if expiration_ts is not None:
        body["expiration_time"] = int(expiration_ts)       # W4b: dies with the window
    return body

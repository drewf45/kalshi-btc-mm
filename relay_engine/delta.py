"""Delta table — GATED ON B1 (C.3).

The canon is explicit: borrow the live `delta_table.py` + candle fetcher WHOLE
from the k_worker tree (B1). No re-derivation from memory. Until B1 arrives this
module is a loud stub that defines the ONLY interface the engine may use:
gate-input only — `p_survive` and `p_cross`. No scheduler role, ever.

Settlement truth anchors to `floor_strike` / `expiration_value` on the market
record, never generic spot — that law is enforced here at the interface.
"""

from typing import Optional

from .errors import GatedOnMissingInput


def p_survive(market_record: dict, seconds_remaining: float) -> float:
    """Probability the market's floor_strike survives to expiration. GATED ON B1."""
    _require_settlement_anchor(market_record)
    raise GatedOnMissingInput(
        "delta_table is gated on B1 (live k_worker zip): borrow delta_table.py WHOLE, "
        "do not re-derive")


def p_cross(market_record: dict, seconds_remaining: float) -> float:
    """Probability of a strike cross before expiration. GATED ON B1."""
    _require_settlement_anchor(market_record)
    raise GatedOnMissingInput(
        "delta_table is gated on B1 (live k_worker zip): borrow delta_table.py WHOLE, "
        "do not re-derive")


def _require_settlement_anchor(market_record: dict) -> None:
    """Settlement truth = floor_strike / expiration_value on the market record.
    Generic spot is not an acceptable anchor and never will be."""
    if not isinstance(market_record, dict) or (
            "floor_strike" not in market_record and "expiration_value" not in market_record):
        raise ValueError(
            "market record lacks floor_strike/expiration_value — refusing to anchor "
            "settlement truth to generic spot")


def settlement_anchor(market_record: dict) -> Optional[float]:
    _require_settlement_anchor(market_record)
    v = market_record.get("floor_strike", market_record.get("expiration_value"))
    return float(v) if v is not None else None

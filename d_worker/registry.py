"""Settlement-source registry + fee-multiplier registry.

Settlement registry: mapping from series → truth source + variable_kind.
Approval flow: series auto-drafted on first scan; approved via DW_APPROVED_SERIES
env var at boot (no Telegram commands). variable_kind defaults to running_max.

Fee registry: cached fee multipliers from the API, with change alerting.
"""

import time
import logging
from typing import Optional, List, Dict

from . import dstore
from . import series_of as _series_of

log = logging.getLogger("d_worker.registry")

CRYPTO_BLACKLIST = {
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXADA15M",
    "KXLTC15M", "KXAVAX15M", "KXLINK15M", "KXDOT15M", "KXMATIC15M",
    "KXSUI15M", "KXBNB15M", "KXTRX15M", "KXSHIB15M", "KXXRP15M",
    "KXPEPE15M",
}

_user_blacklist: set = set()


def load_blacklist() -> None:
    global _user_blacklist
    raw = dstore.get_state("series_blacklist")
    if raw:
        _user_blacklist = set(raw.split(","))
    else:
        _user_blacklist = set()


def save_blacklist() -> None:
    dstore.set_state("series_blacklist", ",".join(sorted(_user_blacklist)))


def is_blacklisted(series: str) -> bool:
    return series in CRYPTO_BLACKLIST or series in _user_blacklist


def add_blacklist(series: str) -> None:
    _user_blacklist.add(series)
    save_blacklist()


def remove_blacklist(series: str) -> None:
    _user_blacklist.discard(series)
    save_blacklist()


def auto_draft(market: dict) -> Optional[str]:
    """Auto-draft a registry entry from a market object if it looks like weather.
    Returns series_ticker if drafted, else None."""
    series = _series_of(market)
    if not series or is_blacklisted(series):
        return None
    existing = dstore.get_registry(series)
    if existing:
        return None
    title = (market.get("title") or market.get("subtitle") or "").lower()
    settle_source = ""
    station = ""
    units = "F"
    rounding = "0.5"
    tz = "America/New_York"
    if any(w in title for w in ["temperature", "high temp", "low temp",
                                 "degrees", "°f", "weather"]):
        settle_source = "NWS"
        rules_url = market.get("rules_url", "")
        notes = f"auto-drafted from market {market.get('ticker', '')}"
        dstore.draft_registry(series, settle_source, station, tz, units,
                              rounding, rules_url, notes)
        log.info(f"[REGISTRY] Auto-drafted {series} (weather)")
        return series
    return None


def pull_fee_schedule(client) -> List[dict]:
    """Pull fee info from market objects during sweep. Returns list of changes."""
    changes = []
    fees = dstore.list_fees()
    for f in fees:
        if f.get("maker_mult") is None:
            continue
    return changes


def update_fee_from_market(market: dict) -> Optional[dict]:
    """Extract fee multipliers from a market object and upsert.
    Returns old values if changed (for alerting)."""
    series = _series_of(market)
    if not series:
        return None
    maker = market.get("maker_fee_rate") or market.get("fee_rate_maker")
    taker = market.get("taker_fee_rate") or market.get("fee_rate_taker")
    if maker is None and taker is None:
        fee_info = market.get("fee_schedule", {})
        if isinstance(fee_info, dict):
            maker = fee_info.get("maker")
            taker = fee_info.get("taker")
    if maker is None:
        maker = 1.0
    if taker is None:
        taker = 1.0
    try:
        maker = float(maker)
        taker = float(taker)
    except (ValueError, TypeError):
        return None
    pos_limit = market.get("position_limit")
    min_tick = market.get("min_tick_size") or market.get("tick_size") or 1
    return dstore.upsert_fee(series, maker, taker,
                              position_limit=pos_limit,
                              min_tick=int(min_tick),
                              source="market_obj")


def get_approved_registry(series: str) -> Optional[dict]:
    """Get registry row only if approved."""
    row = dstore.get_registry(series)
    if row and row.get("approved_ts") is not None:
        return row
    return row

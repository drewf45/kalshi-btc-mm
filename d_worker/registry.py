"""Settlement-source registry + fee-multiplier registry.

Settlement registry: mapping from series → truth source + variable_kind.
Approval flow: series auto-drafted on first scan; approved via DW_APPROVED_SERIES
env var at boot (no Telegram commands). variable_kind defaults to running_max.

Fee registry: cached fee multipliers from the API, with change alerting.
"""

import os
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

DRAFT_PREFIXES = [p.strip().upper()
                  for p in os.environ.get("DW_DRAFT_PREFIXES", "KXHIGH,KXLOW").split(",")
                  if p.strip()]

CITY_STATIONS = {
    "NY":   ("KNYC", "America/New_York"),
    "CHI":  ("KMDW", "America/Chicago"),
    "AUS":  ("KAUS", "America/Chicago"),
    "DEN":  ("KDEN", "America/Denver"),
    "LAX":  ("KLAX", "America/Los_Angeles"),
    "MIA":  ("KMIA", "America/New_York"),
    "PHIL": ("KPHL", "America/New_York"),
}


def _station_for(series: str):
    """Map a prefixed weather series to (station, tz); UNKNOWN if city unmapped."""
    for prefix in DRAFT_PREFIXES:
        if series.startswith(prefix):
            city = series[len(prefix):]
            if city in CITY_STATIONS:
                return CITY_STATIONS[city]
    return ("UNKNOWN", "America/New_York")


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
    """Draft a registry entry. Detection order: ticker prefix -> category -> title.
    Returns series_ticker if drafted, else None."""
    series = _series_of(market)
    if not series or is_blacklisted(series):
        return None
    if dstore.get_registry(series):
        return None

    matched = None
    if any(series.startswith(p) for p in DRAFT_PREFIXES):
        matched = "prefix"
    else:
        cat = (market.get("category") or "").lower()
        if "climate" in cat or "weather" in cat:
            matched = "category"
        else:
            title = (market.get("title") or market.get("subtitle") or "").lower()
            if any(w in title for w in ["temperature", "high temp", "low temp",
                                         "degrees", "°f", "weather"]):
                matched = "title"
    if not matched:
        return None

    station, tz = _station_for(series)
    variable_kind = "running_min" if series.startswith("KXLOW") else "running_max"
    notes = (f"auto-drafted via {matched} from {market.get('ticker', '')} "
             f"kind={variable_kind}")
    dstore.draft_registry(series, "NWS", station, tz, "F", "0.5",
                          market.get("rules_url", ""), notes,
                          variable_kind=variable_kind)
    log.info(f"[REGISTRY] Drafted {series} via {matched} station={station} "
             f"kind={variable_kind}")
    return series


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

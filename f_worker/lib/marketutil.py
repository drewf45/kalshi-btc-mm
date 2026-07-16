# f_worker/lib/marketutil.py
# Crypto-free market helpers borrowed from bot.py. Split out of kalshi.py on purpose:
# the testable core (fgateway walls, manager, pricebrain, tests) needs orderbook
# parsing, payload building and close-ts math WITHOUT dragging in the cryptography
# stack that KalshiClient requires. Nothing here imports cryptography.

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


# ---- close_ts resolution (borrowed verbatim from bot.py) ----
def _parse_iso_to_epoch_s(s: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except Exception:
        return None


def infer_close_ts_from_ticker(ticker: str, interval_minutes: int = 15) -> Optional[int]:
    """Close-ts from a KXBTC15M ticker's date block. Kalshi's format is YYMONDD then
    HHMM, and the HHMM names the window's CLOSE time in UTC (verified against the live
    fill tape: KXBTC15M-26JUL161430-30 closed 2026-07-16T14:30:00Z). The old code read
    the block as DDMONYY and NY-start+interval — dating every inferred market a decade
    in the past, which the clock filter then discarded as a corpse. No tz shift, no
    interval add. interval_minutes is kept for signature/back-compat and unused."""
    if not ticker:
        return None
    try:
        parts = str(ticker).split("-")
        if len(parts) < 2:
            return None
        dt_chunk = parts[1]
        year = 2000 + int(dt_chunk[0:2])   # YY
        mon = MONTHS[dt_chunk[2:5].upper()]  # MON
        day = int(dt_chunk[5:7])           # DD
        hh = int(dt_chunk[7:9])            # HH
        mm = int(dt_chunk[9:11])           # MM (close time, UTC)
        return int(datetime(year, mon, day, hh, mm, tzinfo=UTC).timestamp())
    except Exception:
        return None


def resolve_close_ts(market_obj: Dict[str, Any], ticker: str) -> Optional[int]:
    if not isinstance(market_obj, dict):
        market_obj = {}
    for k in ("close_ts", "closeTs", "close_time_ts", "closeTimeTs", "close_timestamp",
              "closeTimestamp", "close_time", "closeTime", "expiration_ts", "expirationTs"):
        v = market_obj.get(k)
        if isinstance(v, (int, float)):
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv
    for k in ("close_time", "closeTime", "close_datetime", "closeDateTime",
              "expiration_time", "expirationTime"):
        v = market_obj.get(k)
        if isinstance(v, str):
            ts = _parse_iso_to_epoch_s(v)
            if ts is not None:
                return ts
    return infer_close_ts_from_ticker(ticker, interval_minutes=15)


def pick_active_market(markets: List[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    now = time.time()
    now_ts = int(now)

    def get_ts(obj: Dict[str, Any], key: str) -> Optional[int]:
        v = obj.get(key)
        if v is None:
            return None
        try:
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv
        except Exception:
            return None

    def close_of(m: Dict[str, Any]) -> int:
        return resolve_close_ts(m, str(m.get("ticker") or m.get("market_ticker") or "")) or 0

    # CLOCK TRUTH BEATS STATUS TRUTH. Kalshi's market list keeps a just-closed window
    # flagged active/open for ~2-3 minutes; the desk used to keep rediscovering that
    # corpse and idle. A market whose close_ts has passed is DEAD regardless of reported
    # status — filter to LIVE (future-close) markets before choosing. We never trade a
    # corpse, and we never let one mask the freshly-opened window.
    live = [m for m in markets if close_of(m) > now + 5]
    if not live:
        raise RuntimeError("No live (future-close) markets to pick from.")

    active, future = [], []
    for m in live:
        ct = close_of(m)
        ot = get_ts(m, "open_time") or get_ts(m, "open_ts") or get_ts(m, "open_timestamp")
        if ot is not None and ot <= now_ts < ct:
            active.append((ct, m))
        else:
            future.append((ct, m))

    pool = active or future
    pool.sort(key=lambda x: x[0])   # nearest close (unchanged tie-breaking)
    chosen = pool[0][1]

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = chosen.get("event_ticker") or (chosen.get("event") or {}).get("ticker")
    if not market_ticker or not event_ticker:
        raise RuntimeError(f"Could not determine event/market ticker from: {chosen}")
    return str(event_ticker), str(market_ticker), chosen


def market_bounds_usd(market_obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lo = hi = None
    for lo_key in ("floor_strike", "lower_strike", "strike_lower", "floor"):
        if lo_key in market_obj:
            try:
                lo = float(market_obj[lo_key]); break
            except Exception:
                lo = None
    for hi_key in ("cap_strike", "upper_strike", "strike_upper", "cap"):
        if hi_key in market_obj:
            try:
                hi = float(market_obj[hi_key]); break
            except Exception:
                hi = None
    return lo, hi


# ---- orderbook parsing ----
# The LIVE Kalshi API returns the book under the envelope key `orderbook_fp` (fixed-
# point) with prices as dollar STRINGS ("0.4700" == 47c); the legacy shape used
# `orderbook` with integer cents. The old parser (borrowed from the pre-fp bot.py
# fossil) only unwrapped `orderbook` and only read numeric levels, so it returned
# (None,None,None,None) against the real shape — gating every window against a blank
# book. We now unwrap both envelopes and read dollar-strings AND cents alike.
def _price_to_cents(v: Any) -> Optional[int]:
    """Coerce a level price to integer cents. Handles fp dollar-strings ("0.4700"->47),
    cent-strings ("47"->47), and numeric cents/dollars."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return int(round(v * 100)) if 0.0 < v <= 1.0 else int(round(v))
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(round(float(s) * 100)) if "." in s else int(s)
        except ValueError:
            return None
    return None


def _best_from_levels(levels: Any, want: str) -> Optional[int]:
    if not isinstance(levels, list) or not levels:
        return None
    best: Optional[int] = None
    for lv in levels:
        p = None
        if isinstance(lv, (list, tuple)) and len(lv) >= 1:
            p = _price_to_cents(lv[0])
        elif isinstance(lv, dict):
            for k in ("price", "yes_price", "p"):
                if k in lv:
                    p = _price_to_cents(lv[k]); break
        if p is None:
            continue
        best = p if best is None else (max(best, p) if want == "bid" else min(best, p))
    if best is None:
        return None
    return clamp_int(best, 1, 99)


def _unwrap_book(ob: Dict[str, Any]) -> Any:
    """Return the yes/no container, unwrapping either the fp or legacy envelope."""
    for key in ("orderbook_fp", "orderbook"):
        if isinstance(ob.get(key), dict):
            return ob.get(key)
    return ob


def raw_book_sample(ob: Dict[str, Any]) -> Tuple[List[str], Any]:
    """Evidence helper (F2.3): the raw response's top-level keys and one sample level,
    so a gate row can falsify the parser itself ('is the book empty, or did I fail to
    read it?')."""
    if not isinstance(ob, dict):
        return [], None
    keys = list(ob.keys())
    root = _unwrap_book(ob)
    if isinstance(root, dict):
        for side in ("yes", "no"):
            sub = root.get(side)
            if isinstance(sub, list) and sub:
                return keys, sub[0]
            if isinstance(sub, dict):
                for lk in ("bids", "asks", "buy", "sell"):
                    lv = sub.get(lk)
                    if isinstance(lv, list) and lv:
                        return keys, lv[0]
    return keys, None


def parse_best_yes_no(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    if not isinstance(ob, dict):
        return None, None, None, None
    root = _unwrap_book(ob)
    yes_bid = yes_ask = no_bid = no_ask = None
    if isinstance(root, dict) and isinstance(root.get("yes"), dict):
        y = root.get("yes", {}); n = root.get("no", {})
        yes_bid = _best_from_levels(y.get("bids", y.get("buy")), "bid")
        yes_ask = _best_from_levels(y.get("asks", y.get("sell")), "ask")
        if isinstance(n, dict):
            no_bid = _best_from_levels(n.get("bids", n.get("buy")), "bid")
            no_ask = _best_from_levels(n.get("asks", n.get("sell")), "ask")
    if isinstance(root, dict) and (isinstance(root.get("yes"), list) or isinstance(root.get("no"), list)):
        if yes_bid is None and isinstance(root.get("yes"), list):
            yes_bid = _best_from_levels(root.get("yes"), "bid")
        if no_bid is None and isinstance(root.get("no"), list):
            no_bid = _best_from_levels(root.get("no"), "bid")
    if yes_ask is None and no_bid is not None:
        yes_ask = clamp_int(100 - no_bid, 1, 99)
    if no_ask is None and yes_bid is not None:
        no_ask = clamp_int(100 - yes_bid, 1, 99)
    if yes_bid is None and no_ask is not None:
        yes_bid = clamp_int(100 - no_ask, 1, 99)
    if no_bid is None and yes_ask is not None:
        no_bid = clamp_int(100 - yes_ask, 1, 99)
    if yes_bid is not None and yes_ask is not None and yes_ask <= yes_bid:
        yes_ask = None
    if no_bid is not None and no_ask is not None and no_ask <= no_bid:
        no_ask = None
    return yes_bid, yes_ask, no_bid, no_ask


def build_order_payload(market_ticker: str, action: str, side: str, price_cents: int,
                        count: int, post_only: bool, order_type: str = "limit") -> Dict[str, Any]:
    """Borrowed from bot.py:build_order_payload; adds market-order type for T-90 flat."""
    body: Dict[str, Any] = {
        "ticker": market_ticker,
        "action": action,
        "side": side,
        "type": order_type,
        "count": int(count),
    }
    if order_type == "limit":
        if side == "yes":
            body["yes_price"] = int(price_cents)
        else:
            body["no_price"] = int(price_cents)
        if post_only:
            body["post_only"] = True
    return body

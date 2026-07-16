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


# Consecutive-identical-line aggregation (F3.4): the `[discover]` line was correct but
# printed 4x/second during a status-lag gap — loud-law excellence turned to wallpaper.
# Collapse repeats: print at most once per interval, then re-emit with a repeat count.
# Lives here (crypto-free) so the testable core can exercise it without the crypto stack.
_print_state: Dict[str, list] = {}


def throttled_print(msg: str, min_interval: float = 5.0) -> None:
    now = time.time()
    rec = _print_state.get(msg)
    if rec is None or (now - rec[0]) >= min_interval:
        suffix = f" (repeated ×{rec[1]} over {int(now - rec[0])}s)" if rec and rec[1] else ""
        print(msg + suffix, flush=True)
        _print_state[msg] = [now, 0]
    else:
        rec[1] += 1


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
    """Close-ts from a KXBTC15M ticker's date block: YYMONDD then HHMM, HHMM = CLOSE
    time in EASTERN (America/New_York). Confirmed from the live fill tape: the 00:30
    ticker (KXBTC15M-26JUL150030-30) settled at 04:30Z, i.e. 00:30 ET (EDT). (This
    corrects the earlier UTC reading — the list path used explicit close_time so the tz
    bug stayed latent until direct-ticker discovery made it load-bearing.) No interval
    add. interval_minutes kept for signature/back-compat and unused."""
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
        mm = int(dt_chunk[9:11])           # MM (close time, ET)
        return int(datetime(year, mon, day, hh, mm, tzinfo=NY).timestamp())
    except Exception:
        return None


# ---- ticker arithmetic (THE COMPUTED BELL): the ticker is math, so stop asking the
# list endpoint (which lists a newborn ~3-4 min late) and construct it directly. Format
# verified against the tape: SERIES-{YY}{MON}{DD}{HHMM}-{MM}, close time in ET, suffix =
# close minute. Boundaries at :00/:15/:30/:45 (minute is tz-agnostic; only the hour
# digits are ET).
def _ticker_from_close_et(series: str, close_et: datetime) -> str:
    return f"{series}-{close_et.strftime('%y%b%d%H%M').upper()}-{close_et.strftime('%M')}"


def _next_quarter_close_et(now_ts: float) -> datetime:
    """The next :00/:15/:30/:45 boundary STRICTLY after now, as an ET-aware datetime."""
    now_et = datetime.fromtimestamp(now_ts, tz=UTC).astimezone(NY)
    q = (now_et.minute // 15) * 15
    return now_et.replace(minute=q, second=0, microsecond=0) + timedelta(minutes=15)


def current_window_ticker(series: str, now: Optional[float] = None) -> Tuple[str, int]:
    """(ticker, close_ts) of the window currently in progress — closes at the next
    quarter-hour boundary. Covers boots mid-window."""
    now = time.time() if now is None else now
    close_et = _next_quarter_close_et(now)
    return _ticker_from_close_et(series, close_et), int(close_et.astimezone(UTC).timestamp())


def next_window_ticker(series: str, now: Optional[float] = None) -> Tuple[str, int]:
    """(ticker, close_ts) of the NEXT window — the one born when the current one closes.
    Its 200 from get_market IS the real bell."""
    now = time.time() if now is None else now
    close_et = _next_quarter_close_et(now) + timedelta(minutes=15)
    return _ticker_from_close_et(series, close_et), int(close_et.astimezone(UTC).timestamp())


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
        for side in ("yes_dollars", "no_dollars", "yes", "no"):
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
    # THE LAST KEY: per Kalshi's official Get-Market-Orderbook docs the fp inner containers
    # are `yes_dollars` / `no_dollars` (lists of [price_str, count_str] pairs), NOT
    # `yes` / `no`. F2 unwrapped the envelope but read the wrong keys — the whole "book
    # missing a side" saga was this. The book is BIDS ONLY, sorted ASCENDING (highest bid
    # last); _best_from_levels' max() handles the ordering, and the complement logic below
    # turns a YES bid at p into the NO ask at 100-p.
    y_c = root.get("yes_dollars", root.get("yes")) if isinstance(root, dict) else None
    n_c = root.get("no_dollars", root.get("no")) if isinstance(root, dict) else None
    yes_bid = yes_ask = no_bid = no_ask = None
    if isinstance(y_c, dict):
        yes_bid = _best_from_levels(y_c.get("bids", y_c.get("buy")), "bid")
        yes_ask = _best_from_levels(y_c.get("asks", y_c.get("sell")), "ask")
    if isinstance(n_c, dict):
        no_bid = _best_from_levels(n_c.get("bids", n_c.get("buy")), "bid")
        no_ask = _best_from_levels(n_c.get("asks", n_c.get("sell")), "ask")
    if yes_bid is None and isinstance(y_c, list):
        yes_bid = _best_from_levels(y_c, "bid")
    if no_bid is None and isinstance(n_c, list):
        no_bid = _best_from_levels(n_c, "bid")
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


def parse_fill(fill: Dict[str, Any], our_side: str) -> Tuple[Optional[float], int, int]:
    """Parse a Kalshi V2 fill into (price_cents, fee_cents, count) in OUR-side terms —
    ported from k_worker/kalshi.py:parse_fill (the proven engine's fill reader). For a
    BUY fill this is our entry cost; for a SELL fill it is our exit proceeds. Handles the
    fp shapes seen on the live tape (yes_price/no_price as dollar strings, count_fp,
    fee_cost) and complements NO<->YES (a value < 1 is dollars). Returns (None, fee,
    count) when price is unparseable so the caller can fall back."""
    from decimal import Decimal
    inner = fill
    for wrap_key in ("fill", "order"):
        if isinstance(fill.get(wrap_key), dict):
            inner = fill[wrap_key]
            break
    dicts = [inner, fill] if inner is not fill else [fill]

    def _get(key):
        for d in dicts:
            v = d.get(key)
            if v is not None:
                return v
        return None

    def _to_cents(raw):
        if raw is None:
            return None
        try:
            val = Decimal(str(raw))
            return float(val * 100) if val < 1 else float(val)
        except Exception:
            return None

    def _raw_cents(raw):
        try:
            return float(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None

    count = 1
    for ck in ("count", "count_fp", "quantity", "qty"):
        raw = _get(ck)
        if raw is not None:
            try:
                count = int(Decimal(str(raw)))
            except Exception:
                pass
            break

    yk, nk = ("yes_price", "yes_price_dollars"), ("no_price", "no_price_dollars")
    price = None
    if our_side == "yes":
        for k in yk:
            price = _to_cents(_get(k))
            if price is not None:
                break
        if price is None:
            price = _raw_cents(_get("yes_price_cents"))
        if price is None:
            for k in nk:
                v = _to_cents(_get(k))
                if v is not None:
                    price = 100 - v
                    break
    else:
        for k in nk:
            price = _to_cents(_get(k))
            if price is not None:
                break
        if price is None:
            price = _raw_cents(_get("no_price_cents"))
        if price is None:
            for k in yk:
                v = _to_cents(_get(k))
                if v is not None:
                    price = 100 - v
                    break
    if price is None:
        v = _to_cents(_get("price"))
        if v is not None:
            price = v if our_side == "yes" else (100 - v)

    fee_cents = 0
    for fk in ("fee", "fee_cost", "taker_fee", "maker_fee"):
        raw = _get(fk)
        if raw is not None:
            try:
                val = Decimal(str(raw))
                fee_cents = int(val * 100) if val < 1 else int(val)
            except Exception:
                pass
            break
    return price, fee_cents, count


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

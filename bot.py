import os
import json
import time
import base64
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# -----------------------------
# Env / Config
# -----------------------------
load_dotenv()


def getenv_first(keys: List[str], default: str = "") -> str:
    """Return the first non-empty env var among keys."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def getenv_by_prefix(prefixes: List[str]) -> str:
    """
    Return the value of the first env var whose NAME starts with any of the prefixes.
    Useful when Render UI truncates names like KALSHI_PRIVATE_KEY_PEM...
    """
    for name, value in os.environ.items():
        for p in prefixes:
            if name.startswith(p) and str(value).strip() != "":
                return str(value).strip()
    return ""


def env_keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])


def parse_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


API_BASE = getenv_first(["KALSHI_API_BASE", "KALSHI_BASE_URL"], "https://trading-api.kalshi.com").rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2")

# ✅ your Render uses SERIES_TICKER
SERIES = getenv_first(["SERIES", "SERIES_TICKER"], "KXBTC15M").strip()

# event-first
EVENT_TICKER = getenv_first(["EVENT_TICKER", "EVENT"], "").strip()
MARKET_TICKER_OVERRIDE = getenv_first(["MARKET_TICKER", "MARKET"], "").strip()

# ✅ your Render uses POLL_SECONDS
POLL = float(getenv_first(["POLL", "POLL_SECONDS"], "2.0"))
ROLL_CHECK_MIN_SECONDS = float(getenv_first(["ROLL_CHECK_MIN_SECONDS"], "30.0"))

DRY_RUN = parse_bool(getenv_first(["DRY_RUN"], "true"), default=True)
ENABLE_TRADING = parse_bool(getenv_first(["ENABLE_TRADING"], "true"), default=True)

# backoff for 429s
BACKOFF_START = float(getenv_first(["BACKOFF_START"], "1.0"))
BACKOFF_MAX = float(getenv_first(["BACKOFF_MAX"], "16.0"))

# quoting knobs (safe defaults; tune later)
TICK_CENTS = int(getenv_first(["TICK_CENTS"], "1"))
EDGE_CENTS = int(getenv_first(["EDGE_CENTS"], "1"))

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = getenv_first(["LOG_LEVEL"], "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# ✅ Micro-change: Credential discovery + diagnostics
# -----------------------------
# Key ID: support old + possible variants
KALSHI_KEY_ID = getenv_first(
    ["KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", "KALSHI_API_KEY"],
    ""
).strip()

# Private key: support exact names + "prefix" names like KALSHI_PRIVATE_KEY_PEM_B64
KALSHI_PRIVATE_KEY_RAW = getenv_first(
    ["KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY"],
    ""
).strip()

if not KALSHI_PRIVATE_KEY_RAW:
    # prefix match catches names like "KALSHI_PRIVATE_KEY_PEM_B64"
    KALSHI_PRIVATE_KEY_RAW = getenv_by_prefix(["KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_B64"]).strip()

# Safe diagnostics: show what the runtime process actually has (names only)
_detected_kalshi_keys = env_keys_with_prefix("KALSHI_")
log.info("[ENV] Detected KALSHI_* keys: %s", _detected_kalshi_keys if _detected_kalshi_keys else "<none>")

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_RAW:
    raise RuntimeError(
        "Missing Kalshi credentials at runtime.\n"
        f"Detected KALSHI_* keys: {_detected_kalshi_keys}\n"
        "Expected one of:\n"
        "  - KALSHI_KEY_ID or KALSHI_API_KEY_ID (or KALSHI_ACCESS_KEY / KALSHI_API_KEY)\n"
        "  - KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PEM (or any env starting with KALSHI_PRIVATE_KEY_PEM)\n"
        "\n"
        "If your keys appear in Render UI but NOT in the detected list above, they are set on a different service/environment."
    )

# -----------------------------
# Signing helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_env(raw: str) -> Any:
    """
    Supports:
      - PEM text directly (contains 'BEGIN' + 'PRIVATE KEY')
      - base64-encoded PEM (common in env vars)
    """
    s = raw.strip()

    # PEM text already
    if "BEGIN" in s and "PRIVATE KEY" in s:
        return serialization.load_pem_private_key(s.encode("utf-8"), password=None)

    # base64 -> PEM bytes
    try:
        key_bytes = base64.b64decode(s)
        return serialization.load_pem_private_key(key_bytes, password=None)
    except Exception as e:
        raise RuntimeError(
            "Could not parse private key. Provide PEM text in KALSHI_PRIVATE_KEY_PEM "
            "or base64 PEM in KALSHI_PRIVATE_KEY_B64 (or PEM-prefixed variant)."
        ) from e


PRIVATE_KEY = load_private_key_from_env(KALSHI_PRIVATE_KEY_RAW)


def sign_message(message: str) -> str:
    sig = PRIVATE_KEY.sign(
        message.encode("utf-8"),
        asy_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_signature_headers(method: str, path_with_query: str, body: str) -> Dict[str, str]:
    ts = str(now_ms())
    payload = ts + method.upper() + path_with_query + body
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_message(payload),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

# -----------------------------
# HTTP helper (with 429 backoff)
# -----------------------------
def request_json(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = API_BASE + API_PREFIX + path
    params = params or {}
    body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), sort_keys=True)

    # Sign the exact path+query
    if params:
        req = requests.Request(method.upper(), url, params=params, data=body_str)
        prepped = req.prepare()
        signed_path = prepped.path_url
    else:
        signed_path = path

    headers = build_signature_headers(method, signed_path, body_str)

    backoff = BACKOFF_START
    while True:
        resp = requests.request(method.upper(), url, params=params, data=body_str, headers=headers, timeout=20)

        if resp.status_code == 429:
            log.warning("[429] %s backing off %.1fs", path, backoff)
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        if resp.status_code >= 400:
            try:
                j = resp.json()
                raise RuntimeError(f"HTTP {resp.status_code} {path}: {j}")
            except Exception:
                text_head = (resp.text or "")[:200]
                raise RuntimeError(f"HTTP {resp.status_code} {path}: {{'_non_json': True, '_text_head': {text_head!r}}}")

        try:
            return resp.json()
        except Exception:
            text_head = (resp.text or "")[:200]
            raise RuntimeError(f"Bad JSON response for {path}: {text_head!r}")

# -----------------------------
# Rolling: SERIES -> EVENT -> MARKET
# -----------------------------
_last_roll_ts = 0.0
_active_event_ticker: Optional[str] = None
_active_market_ticker: Optional[str] = None


def _parse_dt_to_ts(s: Any) -> float:
    if not isinstance(s, str):
        return 0.0
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return 0.0


def pick_active_event(events: List[Dict[str, Any]]) -> Optional[str]:
    now = time.time()

    def score(e: Dict[str, Any]) -> Tuple[int, float]:
        status = str(e.get("status", "")).lower()
        start_ts = 0.0
        end_ts = 0.0
        for k in ("start_time", "open_time"):
            if k in e:
                start_ts = max(start_ts, _parse_dt_to_ts(e.get(k)))
        for k in ("close_time", "end_time", "expiration_time", "settlement_time"):
            if k in e:
                end_ts = max(end_ts, _parse_dt_to_ts(e.get(k)))

        in_window = (start_ts > 0 and end_ts > 0 and start_ts <= now <= end_ts)
        is_open = status in ("open", "active", "trading")

        if is_open and in_window:
            return (3, -(end_ts - now))
        if is_open:
            return (2, 0.0)
        if start_ts > now:
            return (1, -(start_ts - now))
        return (0, -1e18)

    candidates = [e for e in events if isinstance(e, dict) and (e.get("ticker") or e.get("event_ticker"))]
    if not candidates:
        return None
    best = sorted(candidates, key=score, reverse=True)[0]
    return best.get("ticker") or best.get("event_ticker")


def fetch_events_for_series(series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    # attempt #1
    try:
        data = request_json("GET", f"/series/{series_ticker}/events", params={"limit": limit})
        events = data.get("events") or data.get("data") or data.get("results") or []
        if isinstance(events, list) and events:
            return events
    except Exception as e:
        log.warning("[EVENTS] series endpoint failed: %s", e)

    # attempt #2
    data = request_json("GET", "/events", params={"series_ticker": series_ticker, "limit": limit})
    events = data.get("events") or data.get("data") or data.get("results") or []
    return events if isinstance(events, list) else []


def roll_active_event() -> str:
    global _last_roll_ts, _active_event_ticker

    if EVENT_TICKER:
        if _active_event_ticker != EVENT_TICKER:
            log.info("[ROLL] Using EVENT_TICKER override → %s", EVENT_TICKER)
        _active_event_ticker = EVENT_TICKER
        return _active_event_ticker

    now = time.time()
    if _active_event_ticker and (now - _last_roll_ts) < ROLL_CHECK_MIN_SECONDS:
        return _active_event_ticker

    events = fetch_events_for_series(SERIES, limit=250)
    if not events:
        raise RuntimeError(f"No events returned for series {SERIES}. Check API_BASE/API_PREFIX and series ticker.")

    picked = pick_active_event(events)
    if not picked:
        raise RuntimeError(f"Could not pick an active event for series {SERIES} (events={len(events)})")

    _active_event_ticker = picked
    _last_roll_ts = time.time()
    log.info("[ROLL] Series=%s → Active event → %s", SERIES, _active_event_ticker)
    return _active_event_ticker


def pick_open_market(markets: List[Dict[str, Any]]) -> Optional[str]:
    now = time.time()
    open_markets = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        status = str(m.get("status", "")).lower()
        if status in ("open", "active", "trading"):
            open_markets.append(m)

    if not open_markets:
        for m in markets:
            t = m.get("ticker") or m.get("market_ticker")
            if t:
                return t
        return None

    def score(m: Dict[str, Any]) -> Tuple[int, float]:
        close_ts = 0.0
        for k in ("close_time", "end_time", "expiration_time", "settlement_time"):
            if k in m:
                close_ts = max(close_ts, _parse_dt_to_ts(m.get(k)))
        if close_ts <= 0:
            return (1, -1e18)
        dtc = close_ts - now
        if dtc < 0:
            return (1, -1e12 + dtc)
        return (1, -dtc)

    best = sorted(open_markets, key=score, reverse=True)[0]
    return best.get("ticker") or best.get("market_ticker")


def roll_active_market() -> str:
    global _active_market_ticker

    if MARKET_TICKER_OVERRIDE:
        if _active_market_ticker != MARKET_TICKER_OVERRIDE:
            log.info("[ROLL] Using MARKET_TICKER override → %s", MARKET_TICKER_OVERRIDE)
        _active_market_ticker = MARKET_TICKER_OVERRIDE
        return _active_market_ticker

    event_ticker = roll_active_event()

    data = request_json("GET", f"/events/{event_ticker}/markets", params={"limit": 200})
    markets = data.get("markets") or data.get("data") or data.get("results") or []
    if not isinstance(markets, list) or not markets:
        raise RuntimeError(f"No markets returned for event {event_ticker}. Response keys={list(data.keys())}")

    picked = pick_open_market(markets)
    if not picked:
        raise RuntimeError(f"Could not pick a market for event {event_ticker} (markets={len(markets)})")

    if _active_market_ticker != picked:
        log.info("[ROLL] Event=%s → Active market → %s", event_ticker, picked)
    _active_market_ticker = picked
    return _active_market_ticker

# -----------------------------
# Orderbook parsing (binary)
# -----------------------------
def best_bid_from_side(side: Any) -> Optional[int]:
    if not isinstance(side, list) or not side:
        return None
    best = None
    for lvl in side:
        if isinstance(lvl, list) and len(lvl) >= 1:
            try:
                p = int(lvl[0])
                best = p if best is None else max(best, p)
            except Exception:
                continue
    return best


def get_yes_bid_ask(orderbook_payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    ob = orderbook_payload.get("orderbook") if isinstance(orderbook_payload, dict) else None
    if not isinstance(ob, dict):
        return (None, None)

    yes = ob.get("yes")
    no = ob.get("no")

    yes_bid = best_bid_from_side(yes)
    no_bid = best_bid_from_side(no)

    yes_ask = None
    if no_bid is not None:
        yes_ask = 100 - no_bid

    return (yes_bid, yes_ask)


def fetch_orderbook(market_ticker: str) -> Dict[str, Any]:
    return request_json("GET", f"/markets/{market_ticker}/orderbook")

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info(
        "API_BASE=%s API_PREFIX=%s SERIES=%s EVENT_TICKER=%s MARKET_OVERRIDE=%s POLL=%.1fs DRY_RUN=%s ENABLE_TRADING=%s",
        API_BASE, API_PREFIX, SERIES,
        EVENT_TICKER or "<auto>", MARKET_TICKER_OVERRIDE or "<none>",
        POLL, DRY_RUN, ENABLE_TRADING
    )

    while True:
        try:
            mkt = roll_active_market()
            ob = fetch_orderbook(mkt)
            yes_bid, yes_ask = get_yes_bid_ask(ob)

            if yes_bid is None and yes_ask is None:
                log.info("[QUOTE] %s YES bid=None ask=None → SKIP (empty)", mkt)
            else:
                log.info("[QUOTE] %s YES bid=%s ask=%s", mkt, yes_bid, yes_ask)

        except Exception as e:
            log.error("[LOOPERR] %s", e)

        time.sleep(POLL)


if __name__ == "__main__":
    main()
# bot.py
# Kalshi YES-only rolling 15m market maker
#
# NOTE: The ONLY intentional behavioral change in this version is:
#   - Monotonic timestamp for signing/headers (never goes backwards)
#
# Env vars used (prefix KALSHI_ matches your logs):
#   KALSHI_API_BASE (default https://api.elections.kalshi.com)
#   KALSHI_API_KEY_ID
#   KALSHI_PRIVATE_KEY_PEM_BASE64  (BASE64 of full PEM file contents)
#
# Optional:
#   API_PREFIX (default /trade-api/v2)
#   SERIES (default KXBTC15M)
#   EVENT_TICKER (default auto)
#   MARKET_OVERRIDE (default none)
#   POLL_SECONDS (default 1.5)
#   DRY_RUN (default false)
#   ENABLE_TRADING (default true)
#   POST_ONLY (default true)
#   ORDER_QTY (default 1)
#   MIN_SPREAD (default 3)  # in cents
#   EDGE (default 1)        # in cents off best bid/ask
#   MAX_YES (default 99)
#   MIN_YES (default 1)
#   LOG_LEVEL (default INFO)

import os
import json
import time
import base64
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Helpers / Env
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if not v else int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if not v else float(v)


def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])


# -----------------------------
# Monotonic timestamp (THE FIX)
# -----------------------------
_LAST_TS_MS = 0

def now_ms_monotonic() -> int:
    """
    Kalshi signatures include a timestamp.
    If the clock steps backwards (NTP/time sync), the API can reject signatures.
    This ensures timestamps are strictly non-decreasing and will never go backwards.
    """
    global _LAST_TS_MS
    ms = int(time.time() * 1000)
    if ms <= _LAST_TS_MS:
        ms = _LAST_TS_MS + 1
    _LAST_TS_MS = ms
    return ms


# -----------------------------
# Auth / Client
# -----------------------------
@dataclass
class KalshiConfig:
    api_base: str
    api_prefix: str
    key_id: str
    private_key_pem_b64: str


class KalshiClient:
    def __init__(self, cfg: KalshiConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self._private_key = self._load_private_key(cfg.private_key_pem_b64)

    @staticmethod
    def _load_private_key(pem_b64: str):
        if not pem_b64 or pem_b64.strip() == "":
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64 env var")

        try:
            pem_bytes = base64.b64decode(pem_b64)
        except Exception as e:
            raise RuntimeError(f"KALSHI_PRIVATE_KEY_PEM_BASE64 is not valid base64: {e}")

        try:
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(
                "Failed to load PEM private key. "
                "Make sure you base64-encoded the FULL PEM contents (including BEGIN/END lines). "
                f"Underlying error: {e}"
            )

    def _sign(self, message: bytes) -> str:
        # Common Kalshi approach is RSA + SHA256. Keep the exact primitive stable.
        sig = self._private_key.sign(
            message,
            asy_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        ts = str(now_ms_monotonic())  # <-- monotonic timestamp used here
        # Canonical signing string: timestamp + method + path + body
        # (Do not “improve” this unless you confirm the exact scheme you used yesterday.)
        msg = (ts + method.upper() + path + body).encode("utf-8")
        sig = self._sign(msg)
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.cfg.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request_json(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None,
                     payload: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Any:
        # endpoint should already include api_prefix like "/trade-api/v2/..."
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        path = endpoint
        if params:
            qs = urlencode(params)
            path = f"{endpoint}?{qs}"

        url = self.cfg.api_base.rstrip("/") + path
        body = "" if payload is None else json.dumps(payload, separators=(",", ":"))

        headers = self._headers(method, path, body)

        try:
            resp = self.session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                data=None if payload is None else body,
                timeout=timeout,
            )
        except Exception as e:
            raise RuntimeError(f"HTTP request failed: {method} {path}: {e}")

        text = resp.text or ""
        if resp.status_code >= 400:
            # Keep a short head for logs
            head = text[:300]
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {head}")

        if not text:
            return None
        try:
            return resp.json()
        except Exception:
            return {"_non_json": True, "_text_head": text[:500]}


# -----------------------------
# Market discovery / quoting
# -----------------------------
@dataclass
class MarketRef:
    event_ticker: str
    market_ticker: str


def pick_active_market_from_series_markets(series: str, markets: List[Dict[str, Any]]) -> Optional[MarketRef]:
    """
    Given /markets?series=..., pick the most relevant active market.
    Strategy:
      - Keep only tickers that start with the series prefix
      - Prefer those with status 'active' if present
      - Then pick the latest by lexicographic ticker (works with your KXBTC15M-26JAN.... pattern)
    """
    if not markets:
        return None

    candidates = []
    for m in markets:
        t = m.get("ticker") or m.get("market_ticker") or ""
        if not t.startswith(series + "-"):
            continue
        status = (m.get("status") or "").lower()
        candidates.append((status, t, m))

    if not candidates:
        return None

    # prefer active
    actives = [c for c in candidates if c[0] == "active"]
    pool = actives if actives else candidates

    # Choose "latest" ticker
    pool.sort(key=lambda x: x[1])
    chosen = pool[-1][2]

    market_ticker = chosen.get("ticker") or chosen.get("market_ticker")
    event_ticker = chosen.get("event_ticker") or chosen.get("eventTicker") or ""
    # If event_ticker isn't present, derive by stripping the trailing "-XX" segment
    if not event_ticker and market_ticker and "-" in market_ticker:
        event_ticker = "-".join(market_ticker.split("-")[:-1])

    if not market_ticker or not event_ticker:
        return None
    return MarketRef(event_ticker=event_ticker, market_ticker=market_ticker)


def parse_yes_bid_ask_from_market_obj(m: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Tries multiple shapes because Kalshi payloads can vary.
    We want YES best bid and YES best ask (in cents).
    """
    # Common “flat” keys:
    for bid_key, ask_key in [
        ("yes_bid", "yes_ask"),
        ("yesBid", "yesAsk"),
        ("best_yes_bid", "best_yes_ask"),
        ("bestYesBid", "bestYesAsk"),
    ]:
        b = m.get(bid_key)
        a = m.get(ask_key)
        if b is not None or a is not None:
            try:
                b2 = None if b is None else int(b)
                a2 = None if a is None else int(a)
                return b2, a2
            except Exception:
                pass

    # Sometimes nested:
    # m["yes"]["bid"] / m["yes"]["ask"]
    yes = m.get("yes")
    if isinstance(yes, dict):
        b = yes.get("bid")
        a = yes.get("ask")
        try:
            b2 = None if b is None else int(b)
            a2 = None if a is None else int(a)
            if b2 is not None or a2 is not None:
                return b2, a2
        except Exception:
            pass

    return None, None


# -----------------------------
# Orders (simple OM)
# -----------------------------
@dataclass
class WorkingOrder:
    side: str  # "buy" or "sell"
    price: int
    qty: int
    order_id: Optional[str] = None


class OrderManager:
    def __init__(self):
        self.buy: Optional[WorkingOrder] = None
        self.sell: Optional[WorkingOrder] = None

    def clear(self):
        self.buy = None
        self.sell = None


# -----------------------------
# Strategy params
# -----------------------------
API_BASE = env_str("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = env_str("API_PREFIX", "/trade-api/v2")
SERIES = env_str("SERIES", "KXBTC15M")
EVENT_TICKER = env_str("EVENT_TICKER", "").strip() or "<auto>"
MARKET_OVERRIDE = env_str("MARKET_OVERRIDE", "").strip() or "<none>"

POLL_SECONDS = env_float("POLL_SECONDS", 1.5)

DRY_RUN = env_bool("DRY_RUN", False)
ENABLE_TRADING = env_bool("ENABLE_TRADING", True)
POST_ONLY = env_bool("POST_ONLY", True)

ORDER_QTY = env_int("ORDER_QTY", 1)
MIN_SPREAD = env_int("MIN_SPREAD", 3)
EDGE = env_int("EDGE", 1)

MIN_YES = env_int("MIN_YES", 1)
MAX_YES = env_int("MAX_YES", 99)

# -----------------------------
# Main
# -----------------------------
def main():
    log.info("[ENV] Detected KALSHI_* keys: %s", keys_with_prefix("KALSHI_"))

    key_id = env_str("KALSHI_API_KEY_ID", "").strip()
    pk_b64 = env_str("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()

    cfg = KalshiConfig(
        api_base=API_BASE,
        api_prefix=API_PREFIX,
        key_id=key_id,
        private_key_pem_b64=pk_b64,
    )

    client = KalshiClient(cfg)

    log.info(
        "API_BASE=%s API_PREFIX=%s SERIES=%s EVENT_TICKER=%s MARKET_OVERRIDE=%s POLL=%.1fs DRY_RUN=%s ENABLE_TRADING=%s POST_ONLY=%s",
        API_BASE, API_PREFIX, SERIES, EVENT_TICKER, MARKET_OVERRIDE, POLL_SECONDS, DRY_RUN, ENABLE_TRADING, POST_ONLY
    )

    om = OrderManager()
    active: Optional[MarketRef] = None

    while True:
        try:
            # 1) Find active market (rolling)
            if MARKET_OVERRIDE != "<none>":
                # Derive event ticker from market if needed
                mkt = MARKET_OVERRIDE
                evt = "-".join(mkt.split("-")[:-1]) if "-" in mkt else mkt
                new_active = MarketRef(event_ticker=evt, market_ticker=mkt)
            else:
                resp = client.request_json(
                    "GET",
                    f"{API_PREFIX}/markets",
                    params={"series": SERIES},
                )

                markets = resp.get("markets") if isinstance(resp, dict) else None
                if not isinstance(markets, list):
                    markets = []

                picked = pick_active_market_from_series_markets(SERIES, markets)
                if not picked:
                    log.warning("[ROLL] Series=%s → could not pick active market (no markets)", SERIES)
                    time.sleep(POLL_SECONDS)
                    continue
                new_active = picked

            # If changed → clear OM state
            if (active is None) or (new_active.market_ticker != active.market_ticker):
                active = new_active
                log.info("[ROLL] Series=%s → Active event=%s market=%s (via /markets)", SERIES, active.event_ticker, active.market_ticker)
                om.clear()
                log.info("[OM] %s ROLL detected → cleared working orders (DRY_RUN=%s)", active.market_ticker, DRY_RUN)

            # 2) Fetch market detail (quotes)
            mkt_detail = client.request_json("GET", f"{API_PREFIX}/markets/{active.market_ticker}")
            market_obj = mkt_detail.get("market") if isinstance(mkt_detail, dict) else None
            if not isinstance(market_obj, dict):
                market_obj = {}

            yes_bid, yes_ask = parse_yes_bid_ask_from_market_obj(market_obj)
            if yes_bid is None or yes_ask is None:
                # fallback: sometimes /markets response has more fields than /markets/{ticker} depending on endpoint version
                log.info("[FALLBACK] %s /markets/{ticker} returned no usable yes bid/ask fields", active.market_ticker)
                # try to locate it from series list payload
                resp2 = client.request_json("GET", f"{API_PREFIX}/markets", params={"series": SERIES})
                markets2 = resp2.get("markets") if isinstance(resp2, dict) else []
                if isinstance(markets2, list):
                    match = next((m for m in markets2 if (m.get("ticker") == active.market_ticker)), None)
                    if isinstance(match, dict):
                        yes_bid, yes_ask = parse_yes_bid_ask_from_market_obj(match)

            log.info("[QUOTE] %s YES bid=%s ask=%s", active.market_ticker, yes_bid, yes_ask)

            # 3) Decide if we should quote
            if yes_bid is None or yes_ask is None:
                log.info("[TARGET] %s → SKIP (missing_bid_or_ask)", active.market_ticker)
                time.sleep(POLL_SECONDS)
                continue

            spread = yes_ask - yes_bid
            if spread < MIN_SPREAD:
                log.info("[TARGET] %s → SKIP (spread_too_tight(%s))", active.market_ticker, spread)
                time.sleep(POLL_SECONDS)
                continue

            # clamp and compute our target prices
            buy_px = max(MIN_YES, min(MAX_YES, yes_bid + EDGE))
            sell_px = max(MIN_YES, min(MAX_YES, yes_ask - EDGE))

            # sanity
            if buy_px >= sell_px:
                log.info("[TARGET] %s → SKIP (spread_ok_not_stable(%s) %s>=%s)", active.market_ticker, spread, buy_px, sell_px)
                time.sleep(POLL_SECONDS)
                continue

            # dry-run preview
            log.info(
                "[MM] %s target BUY@%s / SELL@%s (bid=%s ask=%s spread=%s) qty=%s DRY_RUN=%s",
                active.market_ticker, buy_px, sell_px, yes_bid, yes_ask, spread, ORDER_QTY, DRY_RUN
            )

            if not ENABLE_TRADING:
                time.sleep(POLL_SECONDS)
                continue

            # 4) Place orders (simple, no modify/cancel in this "baseline" version)
            # If you already have working orders in state, don’t re-place every loop.
            # (Keeps behavior stable and avoids thrash until we inspect fills.)
            if om.buy is None:
                place_order(client, active.market_ticker, "buy", buy_px, ORDER_QTY, post_only=POST_ONLY, dry_run=DRY_RUN)
                om.buy = WorkingOrder(side="buy", price=buy_px, qty=ORDER_QTY)

            if om.sell is None:
                place_order(client, active.market_ticker, "sell", sell_px, ORDER_QTY, post_only=POST_ONLY, dry_run=DRY_RUN)
                om.sell = WorkingOrder(side="sell", price=sell_px, qty=ORDER_QTY)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error("[LOOPERR] %s", repr(e))
            time.sleep(POLL_SECONDS)


def place_order(client: KalshiClient, market_ticker: str, side: str, price: int, qty: int,
                post_only: bool, dry_run: bool):
    """
    Places a YES order on the given market.
    This assumes the market is YES/NO and that price is in cents.
    """
    payload = {
        "ticker": market_ticker,
        "side": side.upper(),     # BUY / SELL
        "type": "limit",
        "price": price,
        "count": qty,
        "yes": True,              # YES-only
    }
    if post_only:
        payload["post_only"] = True

    if dry_run:
        log.info("[ORDER] DRY_RUN %s %s @%s x%s (post_only=%s)", market_ticker, side.upper(), price, qty, post_only)
        return

    # Endpoint your logs showed: /portfolio/orders (non_json True etc)
    resp = client.request_json("POST", f"{API_PREFIX}/portfolio/orders", payload=payload)
    log.info("[ORDER] %s %s PLACE @%s qty=%s resp=%s", market_ticker, side.upper(), price, qty, summarize(resp))


def summarize(x: Any) -> str:
    try:
        s = json.dumps(x, separators=(",", ":"), ensure_ascii=False)
        if len(s) > 240:
            return s[:240] + "…"
        return s
    except Exception:
        return str(x)[:240]


if __name__ == "__main__":
    main()
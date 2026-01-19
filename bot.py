import os
import json
import time
import base64
import logging
import datetime as dt
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-btc15m-bot")


# ----------------------------
# Load env
# ----------------------------
load_dotenv()

DEFAULT_API_BASE = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"


def env_first(*names: str, default: str = "") -> str:
    """Return first non-empty env var value among names."""
    for n in names:
        v = os.getenv(n, "")
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def sanitize_api_base(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raw = DEFAULT_API_BASE
    raw = raw.rstrip("/")
    # Prevent the classic double-prefix bug:
    idx = raw.find("/trade-api/")
    if idx != -1:
        raw = raw[:idx]
    return raw


# Map env vars (prevents "Missing KALSHI_KEY_ID" if you used older names)
KALSHI_KEY_ID = env_first("KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_API_KEY", "API_KEY_ID", "KEY_ID")
KALSHI_PRIVATE_KEY_B64 = env_first("KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY", "PRIVATE_KEY_B64")

API_BASE = sanitize_api_base(env_first("KALSHI_API_BASE", default=DEFAULT_API_BASE))

SERIES_PREFIX = env_first("SERIES_PREFIX", default="KXBTC15M")
POLL_SECONDS = int(env_first("POLL_SECONDS", default="60"))

ENABLE_TRADING = env_first("ENABLE_TRADING", default="true").lower() == "true"
CONFIRM_LIVE_TRADING = env_first("CONFIRM_LIVE_TRADING", default="false").lower() == "true"

IMBALANCE_THRESHOLD = int(env_first("IMBALANCE_THRESHOLD", default="95"))
TAKE_PROFIT_CENTS = int(env_first("TAKE_PROFIT_CENTS", default="1"))
MAX_TRADE_PCT = float(env_first("MAX_TRADE_PCT", default="0.01"))
DAILY_MAX_DRAWDOWN_PCT = float(env_first("DAILY_MAX_DRAWDOWN_PCT", default="0.20"))

SUBACCOUNT = env_first("KALSHI_SUBACCOUNT", "SUBACCOUNT", default="").strip()

log.info("=== BOT STARTED ===")
log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
log.info(f"POLL_SECONDS={POLL_SECONDS}")
log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
log.info(f"API_BASE={API_BASE}")
log.info(f"SUBACCOUNT={'(none)' if not SUBACCOUNT else SUBACCOUNT}")


def utc_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


class KalshiClient:
    def __init__(self, base: str, key_id: str, private_key_b64: str, subaccount: str = ""):
        if not key_id:
            raise RuntimeError("Missing env var: KALSHI_KEY_ID (or set KALSHI_API_KEY as fallback)")
        if not private_key_b64:
            raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")

        self.base = base.rstrip("/")
        self.key_id = key_id.strip()
        self.subaccount = (subaccount or "").strip()

        try:
            pem = base64.b64decode(private_key_b64.encode("utf-8"))
            self.private_key = serialization.load_pem_private_key(pem, password=None)
        except Exception as e:
            raise RuntimeError(f"Failed to decode/load private key from KALSHI_PRIVATE_KEY_B64: {e}")

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _full_url_and_path(self, path: str) -> Tuple[str, str]:
        # path should be like "/portfolio/balance"
        if not path.startswith("/"):
            path = "/" + path
        # ensure it has /trade-api/v2 prefix exactly once
        if not path.startswith(API_PREFIX + "/"):
            path = API_PREFIX + path
        url = self.base + path
        return url, path  # path includes /trade-api/v2/...

    def _sign(self, method: str, path_with_prefix: str, body: str, ts_ms: int) -> str:
        method = method.upper()
        msg = f"{ts_ms}{method}{path_with_prefix}{body}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None, auth: bool = False) -> Any:
        method = method.upper()
        url, signed_path = self._full_url_and_path(path)

        body_str = ""
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))

        headers: Dict[str, str] = {}
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount

        if auth:
            ts = utc_ms()
            headers["KALSHI-ACCESS-KEY"] = self.key_id
            headers["KALSHI-ACCESS-TIMESTAMP"] = str(ts)
            headers["KALSHI-ACCESS-SIGNATURE"] = self._sign(method, signed_path, body_str, ts)

        resp = self.session.request(method, url, headers=headers, data=body_str if body_str else None, timeout=20)

        if resp.status_code >= 400:
            # Print the exact URL so you can see 404 path issues immediately
            raise RuntimeError(f"HTTP {resp.status_code} url={url} body={resp.text}")

        if resp.text:
            return resp.json()
        return None

    # convenience
    def get_balance(self) -> Dict[str, Any]:
        return self.request("GET", "/portfolio/balance", auth=True)

    def get_markets(self, series_ticker: str, status: str = "open", limit: int = 50) -> Dict[str, Any]:
        # use query params without signing (public)
        url, _ = self._full_url_and_path("/markets")
        params = {"series_ticker": series_ticker, "status": status, "limit": limit}
        resp = self.session.get(url, params=params, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} url={resp.url} body={resp.text}")
        return resp.json()

    def get_orderbook(self, market_ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/markets/{market_ticker}/orderbook")

    def create_order(self, market_ticker: str, side: str, action: str, price: int, count: int) -> Dict[str, Any]:
        payload = {
            "market_ticker": market_ticker,
            "side": side,
            "action": action,
            "type": "limit",
            "price": int(price),
            "count": int(count),
        }
        return self.request("POST", "/orders", json_body=payload, auth=True)


def resolve_next_open_market(kc: KalshiClient, series_prefix: str) -> Optional[str]:
    data = kc.get_markets(series_prefix, status="open", limit=50)
    markets = data.get("markets") or data.get("data") or []
    if not markets:
        return None
    markets_sorted = sorted(markets, key=lambda m: m.get("ticker", ""))
    return markets_sorted[0].get("ticker")


def parse_best_bid_ask(orderbook: Dict[str, Any], side: str) -> Tuple[Optional[int], Optional[int]]:
    ob = orderbook.get("orderbook") or orderbook
    sb = ob.get(side) or {}
    bids = sb.get("bids") or []
    asks = sb.get("asks") or []

    best_bid = None
    best_ask = None

    if bids:
        best_bid = int(bids[0][0]) if isinstance(bids[0], (list, tuple)) else int(bids[0].get("price"))
    if asks:
        best_ask = int(asks[0][0]) if isinstance(asks[0], (list, tuple)) else int(asks[0].get("price"))

    return best_bid, best_ask


def compute_order_size(cash_cents: int, price_cents: int, max_pct: float) -> int:
    if price_cents <= 0:
        return 0
    max_cost = int(cash_cents * max_pct)
    return max(0, max_cost // price_cents)


def should_trade(yes_bid, yes_ask, no_bid, no_ask, threshold: int) -> Optional[Tuple[str, int]]:
    yes_level = max([x for x in [yes_bid, yes_ask] if x is not None], default=None)
    no_level = max([x for x in [no_bid, no_ask] if x is not None], default=None)
    if yes_level is None or no_level is None:
        return None

    # If YES is crowded (>= threshold), buy NO (cheap side) at NO ask
    if yes_level >= threshold and no_ask is not None:
        return ("no", int(no_ask))

    # If NO is crowded (>= threshold), buy YES at YES ask
    if no_level >= threshold and yes_ask is not None:
        return ("yes", int(yes_ask))

    return None


def et_day_key() -> str:
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    return now.strftime("%Y-%m-%d")


def main():
    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)

    bal = kc.get_balance()
    cash_cents = int(bal.get("balance", 0))
    log.info("Auth OK. cash=$%.2f", cash_cents / 100.0)

    day_key = et_day_key()
    start_cash_cents = cash_cents
    traded_markets_today = set()

    while True:
        try:
            # new day rollover
            dk = et_day_key()
            if dk != day_key:
                day_key = dk
                bal = kc.get_balance()
                cash_cents = int(bal.get("balance", 0))
                start_cash_cents = cash_cents
                traded_markets_today.clear()

            # daily stop
            bal = kc.get_balance()
            cash_cents = int(bal.get("balance", 0))
            drawdown = start_cash_cents - cash_cents
            if drawdown >= int(start_cash_cents * DAILY_MAX_DRAWDOWN_PCT):
                log.warning("DAILY STOP HIT: drawdown=$%.2f (>=%.0f%%). Sleeping.",
                            drawdown / 100.0, DAILY_MAX_DRAWDOWN_PCT * 100)
                time.sleep(POLL_SECONDS)
                continue

            market = resolve_next_open_market(kc, SERIES_PREFIX)
            if not market:
                log.warning("No open markets found for series=%s", SERIES_PREFIX)
                time.sleep(POLL_SECONDS)
                continue

            ob = kc.get_orderbook(market)
            yes_bid, yes_ask = parse_best_bid_ask(ob, "yes")
            no_bid, no_ask = parse_best_bid_ask(ob, "no")

            log.info("Heartbeat | market=%s | yes %s/%s no %s/%s | cash=$%.2f",
                     market, yes_bid, yes_ask, no_bid, no_ask, cash_cents / 100.0)

            if any(x is None for x in [yes_bid, yes_ask, no_bid, no_ask]):
                log.warning("Orderbook missing bid/ask. Skipping.")
                time.sleep(POLL_SECONDS)
                continue

            decision = should_trade(yes_bid, yes_ask, no_bid, no_ask, IMBALANCE_THRESHOLD)
            if not decision:
                time.sleep(POLL_SECONDS)
                continue

            side_to_buy, entry_price = decision

            # one trade per 15m market per day
            mk = f"{day_key}:{market}"
            if mk in traded_markets_today:
                time.sleep(POLL_SECONDS)
                continue

            count = compute_order_size(cash_cents, entry_price, MAX_TRADE_PCT)
            if count <= 0:
                log.warning("Order size=0 (cash=%s price=%s). Skipping.", cash_cents, entry_price)
                time.sleep(POLL_SECONDS)
                continue

            if ENABLE_TRADING and CONFIRM_LIVE_TRADING:
                log.info("BUY: market=%s side=%s price=%s count=%s", market, side_to_buy, entry_price, count)
                r1 = kc.create_order(market, side_to_buy, "buy", entry_price, count)
                log.info("BUY placed: %s", r1)

                tp = min(99, entry_price + TAKE_PROFIT_CENTS)
                log.info("TP SELL: market=%s side=%s price=%s count=%s", market, side_to_buy, tp, count)
                r2 = kc.create_order(market, side_to_buy, "sell", tp, count)
                log.info("TP placed: %s", r2)

                traded_markets_today.add(mk)
            else:
                log.info("SIGNAL ONLY: would BUY %s @%s x%s then TP @%s",
                         side_to_buy, entry_price, count, entry_price + TAKE_PROFIT_CENTS)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error("Loop error: %r", e)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
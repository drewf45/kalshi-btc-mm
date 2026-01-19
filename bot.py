import os
import json
import time
import base64
import logging
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List, Union

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
    idx = raw.find("/trade-api/")
    if idx != -1:
        raw = raw[:idx]
    return raw


KALSHI_KEY_ID = env_first(
    "KALSHI_KEY_ID",
    "KALSHI_API_KEY_ID",
    "KALSHI_API_KEY",
    "API_KEY_ID",
    "KEY_ID",
)

# Accept your env var name too
KALSHI_PRIVATE_KEY_B64 = env_first(
    "KALSHI_PRIVATE_KEY_B64",
    "KALSHI_PRIVATE_KEY_PEM_BASE64",
    "KALSHI_PRIVATE_KEY",
    "PRIVATE_KEY_B64",
)

API_BASE = sanitize_api_base(env_first("KALSHI_API_BASE", default=DEFAULT_API_BASE))

SERIES_PREFIX = env_first("SERIES_PREFIX", default="KXBTC15M")
POLL_SECONDS = int(env_first("POLL_SECONDS", default="60"))

ENABLE_TRADING = env_first("ENABLE_TRADING", default="true").lower() == "true"
CONFIRM_LIVE_TRADING = env_first("CONFIRM_LIVE_TRADING", default="false").lower() == "true"

IMBALANCE_THRESHOLD = int(env_first("EXTREME_THRESHOLD", "IMBALANCE_THRESHOLD", default="95"))
TAKE_PROFIT_CENTS = int(env_first("TAKE_PROFIT_CENTS", default="1"))

ORDER_USD_PER_SIDE = float(env_first("ORDER_USD_PER_SIDE", default="1.00"))
MAX_DAILY_LOSS_PCT = float(env_first("MAX_DAILY_LOSS_PCT", default="0.20"))

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


JsonObj = Union[Dict[str, Any], List[Any]]


class KalshiClient:
    def __init__(self, base: str, key_id: str, private_key_b64: str, subaccount: str = ""):
        if not key_id:
            raise RuntimeError("Missing env var: KALSHI_KEY_ID")
        if not private_key_b64:
            raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64 (or KALSHI_PRIVATE_KEY_PEM_BASE64)")

        self.base = base.rstrip("/")
        self.key_id = key_id.strip()
        self.subaccount = (subaccount or "").strip()

        try:
            pem = base64.b64decode(private_key_b64.encode("utf-8"))
            self.private_key = serialization.load_pem_private_key(pem, password=None)
        except Exception as e:
            raise RuntimeError(f"Failed to decode/load private key: {e}")

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _full_url_and_path(self, path: str) -> Tuple[str, str]:
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith(API_PREFIX + "/"):
            path = API_PREFIX + path
        url = self.base + path
        return url, path

    def _sign(self, method: str, path_with_prefix: str, body: str, ts_ms: int) -> str:
        method = method.upper()
        msg = f"{ts_ms}{method}{path_with_prefix}{body}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None, auth: bool = False) -> JsonObj:
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
            raise RuntimeError(f"HTTP {resp.status_code} url={url} body={resp.text}")

        if not resp.text:
            return {}

        try:
            return resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:200]}")

    def get_balance(self) -> Dict[str, Any]:
        data = self.request("GET", "/portfolio/balance", auth=True)
        if isinstance(data, list):
            # weird, but guard anyway
            return {"balance": 0, "raw": data}
        return data

    def get_markets(self, series_ticker: str, status: str = "open", limit: int = 50) -> JsonObj:
        # This endpoint often works without auth; keep it simple.
        url, _ = self._full_url_and_path("/markets")
        params = {"series_ticker": series_ticker, "status": status, "limit": limit}
        resp = self.session.get(url, params=params, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} url={resp.url} body={resp.text}")
        try:
            return resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON markets response: {resp.text[:200]}")

    def get_orderbook(self, market_ticker: str) -> JsonObj:
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
        data = self.request("POST", "/orders", json_body=payload, auth=True)
        if isinstance(data, list):
            return {"raw": data}
        return data


# ----------------------------
# Robust parsers (THIS is the fix)
# ----------------------------

def extract_markets_list(markets_resp: JsonObj) -> List[Dict[str, Any]]:
    """
    Kalshi markets response can be:
      - {"markets":[...]} or {"data":[...]} or {"results":[...]}
      - [...] (list directly)
    """
    if isinstance(markets_resp, list):
        # list of market dicts
        return [m for m in markets_resp if isinstance(m, dict)]

    if not isinstance(markets_resp, dict):
        return []

    for key in ("markets", "data", "results", "items"):
        v = markets_resp.get(key)
        if isinstance(v, list):
            return [m for m in v if isinstance(m, dict)]

    # sometimes nested one level
    v = markets_resp.get("response")
    if isinstance(v, dict):
        for key in ("markets", "data", "results", "items"):
            vv = v.get(key)
            if isinstance(vv, list):
                return [m for m in vv if isinstance(m, dict)]

    return []


def resolve_next_open_market(kc: KalshiClient, series_prefix: str) -> Optional[str]:
    resp = kc.get_markets(series_prefix, status="open", limit=50)
    markets = extract_markets_list(resp)
    if not markets:
        log.warning("No open markets parsed. Raw type=%s", type(resp).__name__)
        return None

    # Prefer earliest by ticker sort (works fine for your 15m cadence)
    markets_sorted = sorted(markets, key=lambda m: str(m.get("ticker", "")))
    ticker = markets_sorted[0].get("ticker")
    return str(ticker) if ticker else None


def extract_orderbook_obj(ob_resp: JsonObj) -> Dict[str, Any]:
    """
    Orderbook can be:
      - {"orderbook": {...}}
      - {...} already
      - sometimes {"data": {"orderbook": {...}}}
    """
    if isinstance(ob_resp, list):
        # unexpected, but if it's a list, pick first dict
        for x in ob_resp:
            if isinstance(x, dict):
                ob_resp = x
                break

    if not isinstance(ob_resp, dict):
        return {}

    if isinstance(ob_resp.get("orderbook"), dict):
        return ob_resp["orderbook"]

    if isinstance(ob_resp.get("data"), dict):
        d = ob_resp["data"]
        if isinstance(d.get("orderbook"), dict):
            return d["orderbook"]
        return d

    return ob_resp


def parse_best_bid_ask(orderbook_resp: JsonObj, side: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Expected shapes vary. We handle:
      orderbook[side] = {"bids":[[price,count],...], "asks":[[price,count],...]}
    """
    ob = extract_orderbook_obj(orderbook_resp)
    if not ob:
        return None, None

    book_side = ob.get(side)
    if not isinstance(book_side, dict):
        return None, None

    bids = book_side.get("bids") or []
    asks = book_side.get("asks") or []

    best_bid = int(bids[0][0]) if isinstance(bids, list) and bids else None
    best_ask = int(asks[0][0]) if isinstance(asks, list) and asks else None
    return best_bid, best_ask


def usd_to_contracts(usd: float, price_cents: int) -> int:
    if price_cents <= 0:
        return 0
    cost_per_contract = price_cents / 100.0
    return int(usd // cost_per_contract)


def main():
    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)

    bal = kc.get_balance()
    cash_cents = int(bal.get("balance", 0))
    log.info("Auth OK. cash=$%.2f", cash_cents / 100.0)

    start_cash_cents = cash_cents

    while True:
        try:
            bal = kc.get_balance()
            cash_cents = int(bal.get("balance", 0))

            # daily stop (simple)
            if (start_cash_cents - cash_cents) >= int(start_cash_cents * MAX_DAILY_LOSS_PCT):
                log.warning("DAILY STOP HIT. Sleeping.")
                time.sleep(POLL_SECONDS)
                continue

            market = resolve_next_open_market(kc, SERIES_PREFIX)
            if not market:
                log.warning("No open market ticker found for %s", SERIES_PREFIX)
                time.sleep(POLL_SECONDS)
                continue

            ob_resp = kc.get_orderbook(market)
            yes_bid, yes_ask = parse_best_bid_ask(ob_resp, "yes")
            no_bid, no_ask = parse_best_bid_ask(ob_resp, "no")

            log.info(
                "Heartbeat | market=%s | yes %s/%s no %s/%s | cash=$%.2f",
                market, yes_bid, yes_ask, no_bid, no_ask, cash_cents / 100.0
            )

            if any(x is None for x in [yes_bid, yes_ask, no_bid, no_ask]):
                log.warning("No quotes available. Skipping.")
                time.sleep(POLL_SECONDS)
                continue

            # "farm wins": fade extremes (buy the cheap side)
            decision = None
            if yes_bid >= IMBALANCE_THRESHOLD:
                decision = ("no", no_ask)
            elif no_bid >= IMBALANCE_THRESHOLD:
                decision = ("yes", yes_ask)

            if not decision:
                time.sleep(POLL_SECONDS)
                continue

            side, entry_price = decision
            count = usd_to_contracts(ORDER_USD_PER_SIDE, entry_price)
            if count <= 0:
                log.warning("Order size=0 for ORDER_USD_PER_SIDE=%.2f price=%s", ORDER_USD_PER_SIDE, entry_price)
                time.sleep(POLL_SECONDS)
                continue

            if ENABLE_TRADING and CONFIRM_LIVE_TRADING:
                log.info("BUY: %s @%s x%s", side, entry_price, count)
                r1 = kc.create_order(market, side, "buy", entry_price, count)
                log.info("BUY placed: %s", r1)

                tp = min(99, entry_price + TAKE_PROFIT_CENTS)
                log.info("TP SELL: %s @%s x%s", side, tp, count)
                r2 = kc.create_order(market, side, "sell", tp, count)
                log.info("TP placed: %s", r2)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error("Loop error: %r", e)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Config
# -----------------------------
API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")
KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()

# REQUIRED: base64 of PEM private key
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip() or None

FARM_SIDE = os.getenv("FARM_SIDE", "YES").strip().upper()  # "YES" only per your request earlier
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))

BOT_TAG = os.getenv("BOT_TAG", "MMBOT").strip()
MAX_BOT_RESTING_PER_TICKER = int(os.getenv("MAX_BOT_RESTING_PER_TICKER", "1"))
CLEANUP_ON_START = os.getenv("CLEANUP_ON_START", "true").lower() == "true"

# to avoid infinite paging on gigantic accounts; micro-safe
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))

# If you use subaccounts, set integer 0..32; blank = none
SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip() or None


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def must_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def load_private_key_from_b64(b64_str: str):
    """
    Expects base64-encoded PEM bytes.
    """
    raw = base64.b64decode(b64_str)
    return serialization.load_pem_private_key(raw, password=None)


# -----------------------------
# AUTH (FIXED TO MATCH KALSHI DOCS)
# -----------------------------
def _path_without_query(path: str) -> str:
    return path.split("?", 1)[0]


def sign_request_pss_b64(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """
    Kalshi: signature = RSA-PSS(SHA256) over: timestamp + METHOD + path_without_query
    IMPORTANT: DO NOT sign query params or body.
    """
    method_u = method.upper()
    p = _path_without_query(path)
    msg = f"{timestamp_ms}{method_u}{p}".encode("utf-8")

    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_headers(private_key, method: str, path: str) -> Dict[str, str]:
    ts = str(now_utc_ts_ms())
    sig = sign_request_pss_b64(private_key, ts, method, path)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }
    if SUBACCOUNT is not None:
        # Kalshi supports a subaccount header in some SDKs; leaving optional
        headers["KALSHI-SUBACCOUNT"] = str(SUBACCOUNT)

    if log.isEnabledFor(logging.INFO):
        log.info(
            "[SIGNDBG] %s %s ts=%sms signed_path=%s sign_str=%s",
            method.upper(),
            path,
            ts,
            _path_without_query(path),
            f"{ts}{method.upper()}{_path_without_query(path)}",
        )
    return headers


# -----------------------------
# HTTP
# -----------------------------
def http_request(private_key, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None) -> Tuple[int, Any]:
    url_path = path
    if params:
        qs = urlencode(params, doseq=True)
        url_path = f"{path}?{qs}"

    url = f"{API_BASE}{url_path}"
    headers = build_headers(private_key, method, url_path)

    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            r = requests.post(url, headers=headers, data=json.dumps(body) if body is not None else None, timeout=10)
        elif method.upper() == "DELETE":
            r = requests.delete(url, headers=headers, timeout=10)
        else:
            raise ValueError(f"Unsupported method: {method}")
    except Exception as e:
        raise RuntimeError(f"HTTP {method} {url_path} failed: {e}") from e

    code = r.status_code
    try:
        data = r.json()
    except Exception:
        data = r.text

    shape = type(data).__name__
    keys = list(data.keys()) if isinstance(data, dict) else None
    log.info("[%s] %s %s -> HTTP=%s shape=%s keys=%s", "REQ", method.upper(), url_path, code, shape, keys)

    return code, data


# -----------------------------
# Kalshi endpoints
# -----------------------------
def discover_api_prefix(private_key) -> str:
    # You already probe /trade-api/v2/markets; keep it stable.
    path = "/trade-api/v2/markets"
    code, data = http_request(private_key, "GET", path, params={"limit": 1})
    if code == 200:
        log.info("Discovered API prefix: /trade-api/v2 (probe %s?limit=1 -> 200)", path)
        log.info("Markets probe HTTP=200 shape=%s keys=%s", type(data).__name__, list(data.keys()) if isinstance(data, dict) else None)
        return "/trade-api/v2"
    raise RuntimeError(f"Could not probe markets: HTTP={code} body={data}")


def get_markets_by_series(private_key, series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    path = "/trade-api/v2/markets"
    code, data = http_request(private_key, "GET", path, params={"series_ticker": series_ticker, "limit": limit})
    if code != 200:
        raise RuntimeError(f"Markets list failed: HTTP={code} body={data}")
    markets = data.get("markets") or []
    log.info("[SERIES] GET %s?series_ticker=%s&limit=%s -> HTTP=200 shape=dict keys=%s", path, series_ticker, limit, list(data.keys()))
    log.info("[SERIES] Returned markets count=%s", len(markets))
    return markets


def select_next_closing_market(markets: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Find smallest positive seconds_to_close
    now = datetime.now(timezone.utc)
    best = None
    best_dt = None
    for m in markets:
        close = m.get("close_time") or m.get("close_ts") or m.get("close_time_ts")
        # close_time is usually ISO8601 Z
        if isinstance(close, str):
            try:
                dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            except Exception:
                continue
        else:
            continue

        if dt <= now:
            continue
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = m

    if not best or not best_dt:
        raise RuntimeError("No upcoming market found in series.")
    seconds_to_close = int((best_dt - now).total_seconds())
    log.info(
        "[SELECT] Next closing market: %s close=%s seconds_to_close=%s",
        best.get("ticker"),
        best_dt.isoformat().replace("+00:00", "Z"),
        seconds_to_close,
    )
    return best


def get_orderbook(private_key, market_ticker: str) -> Any:
    path = f"/trade-api/v2/markets/{market_ticker}/orderbook"
    code, data = http_request(private_key, "GET", path)
    if code != 200:
        raise RuntimeError(f"Orderbook failed: HTTP={code} body={data}")
    log.info("[BOOK] GET %s -> HTTP=200 shape=dict keys=%s", path, list(data.keys()) if isinstance(data, dict) else None)
    return data.get("orderbook")


def _normalize_book_side(side_obj: Any) -> Dict[str, Any]:
    """
    Kalshi sometimes returns:
      orderbook: {"yes": {"asks":[...], "bids":[...]}, "no": {...}}
    but in your logs, orderbook['yes'] is a list (so old code .get() blows up).
    This normalizes to dict with 'asks'/'bids' lists if possible.
    """
    if side_obj is None:
        return {"asks": [], "bids": []}

    # If it's already dict-like
    if isinstance(side_obj, dict):
        return {
            "asks": side_obj.get("asks") or [],
            "bids": side_obj.get("bids") or [],
        }

    # If it is a list, we don't know if it's asks or bids. We'll treat it as asks by default,
    # BUT we will also attempt to infer by sort direction:
    if isinstance(side_obj, list):
        # Each level often looks like {"price": 0.51, "quantity": 10} OR {"price": 51, "qty": 10}
        # We'll keep it as asks; best ask is min price.
        return {"asks": side_obj, "bids": []}

    return {"asks": [], "bids": []}


def best_quotes(orderbook: Any) -> Dict[str, Optional[Dict[str, int]]]:
    """
    Returns:
      yes_best_ask, yes_best_bid, no_best_ask, no_best_bid
    Each is {"price_cents": int, "qty": int} or None
    """
    def pick_best(levels: List[Any], want: str) -> Optional[Dict[str, int]]:
        if not levels:
            return None

        def parse_level(x: Any) -> Optional[Tuple[int, int]]:
            if not isinstance(x, dict):
                return None
            price = x.get("price")
            qty = x.get("qty") or x.get("quantity")
            if price is None or qty is None:
                return None

            # price can be float dollars or int cents
            if isinstance(price, float):
                price_cents = int(round(price * 100))
            else:
                price_cents = int(price)
                # If price looks like 0/1, maybe it's dollars as int 0/1? (unlikely) – keep.
                if 0 <= price_cents <= 1:
                    # treat as dollars -> cents
                    price_cents = int(round(float(price) * 100))
            return price_cents, int(qty)

        parsed: List[Tuple[int, int]] = []
        for lvl in levels:
            t = parse_level(lvl)
            if t:
                parsed.append(t)
        if not parsed:
            return None

        # asks: best is min price; bids: best is max price
        if want == "ask":
            p, q = min(parsed, key=lambda pq: pq[0])
        else:
            p, q = max(parsed, key=lambda pq: pq[0])
        return {"price_cents": p, "qty": q}

    # orderbook can be {"yes": {...}, "no": {...}} or list-y weirdness
    yes_raw = None
    no_raw = None
    if isinstance(orderbook, dict):
        yes_raw = orderbook.get("yes")
        no_raw = orderbook.get("no")

    yes = _normalize_book_side(yes_raw)
    no = _normalize_book_side(no_raw)

    out = {
        "yes_best_ask": pick_best(yes["asks"], "ask"),
        "yes_best_bid": pick_best(yes["bids"], "bid"),
        "no_best_ask": pick_best(no["asks"], "ask"),
        "no_best_bid": pick_best(no["bids"], "bid"),
    }
    return out


def list_orders_page(private_key, status: str, limit: int = 200, cursor: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    path = "/trade-api/v2/portfolio/orders"
    params: Dict[str, Any] = {"status": status, "limit": limit}
    if cursor:
        params["cursor"] = cursor

    code, data = http_request(private_key, "GET", path, params=params)
    if code != 200:
        raise RuntimeError(f"List orders failed: HTTP={code} body={data}")
    orders = data.get("orders") or []
    next_cursor = data.get("cursor")
    log.info("[ORDERS] resting_count=%s", len(orders))
    return orders, next_cursor


def list_orders_all(private_key, status: str, limit: int = 200, max_pages: int = 5) -> List[Dict[str, Any]]:
    all_orders: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        page, cursor = list_orders_page(private_key, status=status, limit=limit, cursor=cursor)
        all_orders.extend(page)
        if not cursor:
            break
    return all_orders


def cancel_order(private_key, order_id: str) -> None:
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    code, data = http_request(private_key, "DELETE", path)
    if code != 200:
        raise RuntimeError(f"Cancel order failed: HTTP={code} body={data}")
    log.info("[CANCEL] order_id=%s -> HTTP=200", order_id)


def place_order_yes_buy(private_key, market_ticker: str, price_cents: int, qty: int) -> None:
    path = "/trade-api/v2/portfolio/orders"
    body = {
        "market_ticker": market_ticker,
        "side": "buy",
        "yes_price": price_cents,
        "count": qty,
        # tag so we can find/cleanup later
        "client_order_id": f"{BOT_TAG}-{int(time.time()*1000)}",
    }
    code, data = http_request(private_key, "POST", path, body=body)
    if code != 201:
        raise RuntimeError(f"Place order failed: HTTP={code} body={data}")
    log.info("[PLACE] POST %s -> HTTP=201 keys=%s", path, list(data.keys()) if isinstance(data, dict) else None)
    log.info("[TRADE] placed BUY YES %s @ %sc on %s", qty, price_cents, market_ticker)


def startup_cleanup(private_key, market_ticker: str) -> None:
    # Only cancel our bot-tagged orders on this ticker (micro-safe)
    try:
        resting = list_orders_all(private_key, "resting", limit=200, max_pages=MAX_PAGES)
    except Exception as e:
        log.warning("[CLEAN] startup cleanup skipped due to error: %s", e)
        return

    mine = []
    for o in resting:
        if o.get("market_ticker") != market_ticker:
            continue
        cid = o.get("client_order_id") or ""
        if cid.startswith(BOT_TAG + "-"):
            mine.append(o)

    # Keep at most 0 on start (full wipe). Micro-safe.
    for o in mine:
        oid = o.get("order_id") or o.get("id")
        if oid:
            cancel_order(private_key, oid)

    log.info("[CLEAN] canceled %s existing bot orders on %s", len(mine), market_ticker)


# -----------------------------
# Main loop
# -----------------------------
def main():
    must_env("KALSHI_KEY_ID", KEY_ID)
    must_env("KALSHI_PRIVATE_KEY_B64", PRIVATE_KEY_B64)

    private_key = load_private_key_from_b64(PRIVATE_KEY_B64)
    log.info("Loaded RSA private key from KALSHI_PRIVATE_KEY_B64.")

    # Probe v2
    discover_api_prefix(private_key)

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", ENABLE_TRADING)
    log.info("CONFIRM_LIVE_TRADING=%s", CONFIRM_LIVE_TRADING)
    log.info("POLL_SECONDS=%s", POLL_SECONDS)
    log.info("SERIES_PREFIX=%s", SERIES_PREFIX)
    log.info("MARKET_TICKER=%s", MARKET_TICKER)
    log.info("API_BASE=%s", API_BASE)
    log.info("SUBACCOUNT=%s", SUBACCOUNT or "(none)")
    log.info("FARM_SIDE=%s BUY_PRICE_CENTS=%s BASE_SIZE=%s", FARM_SIDE, BUY_PRICE_CENTS, BASE_SIZE)
    log.info("BOT_TAG=%s MAX_BOT_RESTING_PER_TICKER=%s CLEANUP_ON_START=%s", BOT_TAG, MAX_BOT_RESTING_PER_TICKER, CLEANUP_ON_START)

    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        raise RuntimeError("ENABLE_TRADING=true but CONFIRM_LIVE_TRADING!=true. Refusing to trade.")

    selected = None
    if MARKET_TICKER:
        selected = {"ticker": MARKET_TICKER}
    else:
        markets = get_markets_by_series(private_key, SERIES_PREFIX, limit=200)
        selected = select_next_closing_market(markets)

    market_ticker = selected.get("ticker")
    if not market_ticker:
        raise RuntimeError("No market_ticker selected.")

    if CLEANUP_ON_START:
        startup_cleanup(private_key, market_ticker)

    while True:
        try:
            ob = get_orderbook(private_key, market_ticker)
            q = best_quotes(ob)
            log.info("[BEST] %s", json.dumps(q))

            # YES-only farming: place one tiny resting order if we have < MAX_BOT_RESTING_PER_TICKER
            if ENABLE_TRADING and FARM_SIDE == "YES":
                # list orders (this is what was 401'ing for you)
                resting_all = list_orders_all(private_key, "resting", limit=200, max_pages=MAX_PAGES)

                my_resting = [
                    o for o in resting_all
                    if o.get("market_ticker") == market_ticker
                    and (o.get("client_order_id") or "").startswith(BOT_TAG + "-")
                ]

                if len(my_resting) < MAX_BOT_RESTING_PER_TICKER:
                    place_order_yes_buy(private_key, market_ticker, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main() 
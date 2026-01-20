import os
import json
import time
import base64
import uuid
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
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com")

# You said: env vars are KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_B64
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

if not KALSHI_KEY_ID:
    raise RuntimeError("Missing env var: KALSHI_KEY_ID")
if not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")

# Market / strategy
MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip()
if not MARKET_TICKER:
    raise RuntimeError("Missing env var: MARKET_TICKER (example: KXBTC15M-26JAN201245-45)")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))  # you said 60 was accidental
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))  # default 1c
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))  # contracts
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def load_private_key_from_b64(b64: str):
    """
    Expects base64 of the PEM text.
    Example: base64.b64encode(open('key.pem','rb').read()).decode()
    """
    try:
        key_bytes = base64.b64decode(b64)
    except Exception as e:
        raise RuntimeError(f"Failed to base64-decode KALSHI_PRIVATE_KEY_B64: {e}")

    try:
        return serialization.load_pem_private_key(key_bytes, password=None)
    except Exception as e:
        raise RuntimeError(
            "Failed to load PEM private key from decoded bytes. "
            "Confirm KALSHI_PRIVATE_KEY_B64 is base64(PEM_file_bytes). "
            f"Error={e}"
        )


PRIVATE_KEY = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)


def sha256_b64(data: bytes) -> str:
    h = hashes.Hash(hashes.SHA256())
    h.update(data)
    return base64.b64encode(h.finalize()).decode()


def sign_request(private_key, timestamp_ms: int, method: str, path: str) -> str:
    """
    Kalshi v2 signature string:
      <timestamp_ms><METHOD><path>
    Example:
      1768930882027POST/trade-api/v2/portfolio/orders
    """
    sign_str = f"{timestamp_ms}{method.upper()}{path}"
    sig = private_key.sign(
        sign_str.encode("utf-8"),
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def kalshi_headers(method: str, path: str) -> Dict[str, str]:
    ts = now_utc_ts_ms()
    sig = sign_request(PRIVATE_KEY, ts, method, path)
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


def request(method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    """
    Signs ONLY the path (including query string) and sends JSON body if present.
    """
    if params:
        qs = urlencode(params, doseq=True)
        signed_path = f"{path}?{qs}"
    else:
        signed_path = path

    headers = kalshi_headers(method, signed_path)

    url = f"{BASE_URL}{path}"

    # Debug signature inputs
    if os.getenv("SIGN_DEBUG", "true").lower() in ("1", "true", "yes", "y"):
        body_bytes = b"" if body is None else json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        log.info(
            "[SIGNDBG] %s %s ts=%sms signed_path=%s sign_str=%s body_len=%d signing_payload_sha256_b64=%s",
            method.upper(),
            signed_path,
            headers["KALSHI-ACCESS-TIMESTAMP"],
            signed_path,
            f'{headers["KALSHI-ACCESS-TIMESTAMP"]}{method.upper()}{signed_path}',
            len(body_bytes),
            sha256_b64(body_bytes),
        )

    try:
        resp = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            json=body,
            timeout=15,
        )
    except Exception as e:
        raise RuntimeError(f"HTTP request failed: {method} {url} error={e}")

    code = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = resp.text

    log.info("[REQ] %s %s -> HTTP=%s shape=%s keys=%s",
             method.upper(),
             signed_path,
             code,
             type(data).__name__,
             list(data.keys()) if isinstance(data, dict) else None)

    return code, data


# -----------------------------
# API calls
# -----------------------------
def get_orderbook(ticker: str) -> Dict[str, Any]:
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    code, data = request("GET", path)
    if code != 200:
        raise RuntimeError(f"Orderbook failed: HTTP={code} body={data}")
    return data.get("orderbook", {})


def parse_best_levels(orderbook: Dict[str, Any]) -> Dict[str, Optional[Dict[str, int]]]:
    """
    Conservative parsing: handle common Kalshi orderbook shapes.
    We only need best bid/ask for YES/NO eventually, but right now you’re YES-only.
    """
    best = {"yes_best_bid": None, "yes_best_ask": None, "no_best_bid": None, "no_best_ask": None}

    # Some shapes use separate arrays, some nested. We’ll try likely keys.
    # If your current log already prints BEST, keep this safe.
    def best_from_side(arr: Any, want: str) -> Optional[Dict[str, int]]:
        if not isinstance(arr, list) or not arr:
            return None
        # entries can be {"price":x,"count":y} or {"price_cents":x,"qty":y}
        first = arr[0]
        if not isinstance(first, dict):
            return None
        price = first.get("price_cents", first.get("price"))
        qty = first.get("qty", first.get("count"))
        if price is None or qty is None:
            return None
        return {"price_cents": int(price), "qty": int(qty)}

    # Try common:
    # orderbook = {"yes": {"bids": [...], "asks":[...]}, "no": {...}}
    if "yes" in orderbook and isinstance(orderbook["yes"], dict):
        y = orderbook["yes"]
        best["yes_best_bid"] = best_from_side(y.get("bids"), "bid")
        best["yes_best_ask"] = best_from_side(y.get("asks"), "ask")
    if "no" in orderbook and isinstance(orderbook["no"], dict):
        n = orderbook["no"]
        best["no_best_bid"] = best_from_side(n.get("bids"), "bid")
        best["no_best_ask"] = best_from_side(n.get("asks"), "ask")

    # Other possible shape:
    # orderbook = {"bids": {"yes":[...],"no":[...]}, "asks": {"yes":[...],"no":[...]}}
    if best["yes_best_bid"] is None and isinstance(orderbook.get("bids"), dict):
        best["yes_best_bid"] = best_from_side(orderbook["bids"].get("yes"), "bid")
    if best["yes_best_ask"] is None and isinstance(orderbook.get("asks"), dict):
        best["yes_best_ask"] = best_from_side(orderbook["asks"].get("yes"), "ask")
    if best["no_best_bid"] is None and isinstance(orderbook.get("bids"), dict):
        best["no_best_bid"] = best_from_side(orderbook["bids"].get("no"), "bid")
    if best["no_best_ask"] is None and isinstance(orderbook.get("asks"), dict):
        best["no_best_ask"] = best_from_side(orderbook["asks"].get("no"), "ask")

    return best


def list_orders_page(status: str, limit: int = 200, cursor: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    path = "/trade-api/v2/portfolio/orders"
    params: Dict[str, Any] = {"status": status, "limit": limit}
    if cursor:
        params["cursor"] = cursor

    code, data = request("GET", path, params=params)
    if code != 200:
        raise RuntimeError(f"List orders failed: HTTP={code} body={data}")

    orders = data.get("orders", []) if isinstance(data, dict) else []
    next_cursor = data.get("cursor") if isinstance(data, dict) else None
    return orders, next_cursor


def list_orders_all(status: str, limit: int = 200, max_pages: int = 5) -> List[Dict[str, Any]]:
    all_orders: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    for _ in range(max_pages):
        page, cursor = list_orders_page(status=status, limit=limit, cursor=cursor)
        all_orders.extend(page)
        if not cursor:
            break
    return all_orders


def create_order_yes_buy(ticker: str, yes_price_cents: int, count: int) -> Dict[str, Any]:
    """
    FIX: send Kalshi Create Order body with REQUIRED lowercase keys:
      ticker, side, action, type, count, and yes_price
    """
    path = "/trade-api/v2/portfolio/orders"

    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": int(count),
        "yes_price": int(yes_price_cents),
        "client_order_id": str(uuid.uuid4()),
        "post_only": bool(POST_ONLY),
    }

    code, data = request("POST", path, body=body)
    if code not in (200, 201):
        raise RuntimeError(f"Place order failed: HTTP={code} body={data}")
    return data


# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info("[BOOT] BASE_URL=%s MARKET_TICKER=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s",
             BASE_URL, MARKET_TICKER, POLL_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY)

    while True:
        try:
            ob = get_orderbook(MARKET_TICKER)
            best = parse_best_levels(ob)
            log.info("[BEST] %s", json.dumps(best))

            resting = list_orders_all("resting", limit=200, max_pages=MAX_PAGES)
            log.info("[ORDERS] resting_count=%d", len(resting))

            # YES-only for now: place one tiny buy each loop (you’ll change this soon)
            create_order_yes_buy(MARKET_TICKER, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
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

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

if not KALSHI_KEY_ID:
    raise RuntimeError("Missing env var: KALSHI_KEY_ID")
if not KALSHI_PRIVATE_KEY_B64:
    raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")

MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip()
if not MARKET_TICKER:
    raise RuntimeError("Missing env var: MARKET_TICKER (example: KXBTC15M-26JAN201245-45)")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))
POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))

# Micro-change toggles (safe defaults)
LOG_SPREAD = os.getenv("LOG_SPREAD", "true").lower() in ("1", "true", "yes", "y")


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


def request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None
) -> Tuple[int, Any]:
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

    log.info(
        "[REQ] %s %s -> HTTP=%s shape=%s keys=%s",
        method.upper(),
        signed_path,
        code,
        type(data).__name__,
        list(data.keys()) if isinstance(data, dict) else None
    )

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


# -----------------------------
# Micro-change #1:
# Parse YES best bid + YES best ask (spread awareness)
# -----------------------------
def _coerce_price_to_cents(p: Any) -> Optional[int]:
    """
    Accepts price formats like:
      - 0.52 (dollars) -> 52
      - "0.52" -> 52
      - 52 (already cents) -> 52
      - "52" -> 52
    Returns int cents or None.
    """
    if p is None:
        return None
    try:
        if isinstance(p, str):
            s = p.strip()
            if not s:
                return None
            if "." in s:
                return int(round(float(s) * 100))
            return int(s)
        if isinstance(p, int):
            return int(p)
        if isinstance(p, float):
            if p <= 1.0:
                return int(round(p * 100))
            return int(round(p))
    except Exception:
        return None
    return None


def _pick_qty(level: Dict[str, Any]) -> Optional[int]:
    for k in ("qty", "count", "quantity", "size", "amount"):
        if k in level and level[k] is not None:
            try:
                return int(level[k])
            except Exception:
                pass
    return None


def _best_from_levels(levels: Any, want: str) -> Optional[Dict[str, int]]:
    """
    levels: list[dict]
    want: "bid" or "ask"
    Returns {"price_cents": int, "qty": int} or None
    """
    if not isinstance(levels, list) or not levels:
        return None

    parsed: List[Tuple[int, int]] = []
    for lv in levels:
        if not isinstance(lv, dict):
            continue

        price_raw = lv.get("price_cents", lv.get("price", lv.get("p")))
        price_cents = _coerce_price_to_cents(price_raw)
        qty = _pick_qty(lv)
        if price_cents is None or qty is None:
            continue
        parsed.append((price_cents, qty))

    if not parsed:
        return None

    # bids: max price. asks: min price.
    if want == "bid":
        price_cents, qty = max(parsed, key=lambda x: x[0])
    else:
        price_cents, qty = min(parsed, key=lambda x: x[0])

    return {"price_cents": int(price_cents), "qty": int(qty)}


def parse_best_levels(orderbook: Dict[str, Any]) -> Dict[str, Optional[Dict[str, int]]]:
    """
    Conservative parsing for best bid/ask on YES/NO.
    Micro-change: ensure YES best bid is parsed (not only ask),
    and log spread sanity downstream.
    """
    best: Dict[str, Optional[Dict[str, int]]] = {
        "yes_best_bid": None,
        "yes_best_ask": None,
        "no_best_bid": None,
        "no_best_ask": None,
    }

    # Shape A:
    # {"yes":{"bids":[...],"asks":[...]}, "no":{...}}
    if isinstance(orderbook.get("yes"), dict):
        y = orderbook["yes"]
        best["yes_best_bid"] = _best_from_levels(y.get("bids"), "bid")
        best["yes_best_ask"] = _best_from_levels(y.get("asks"), "ask")

    if isinstance(orderbook.get("no"), dict):
        n = orderbook["no"]
        best["no_best_bid"] = _best_from_levels(n.get("bids"), "bid")
        best["no_best_ask"] = _best_from_levels(n.get("asks"), "ask")

    # Shape B:
    # {"bids":{"yes":[...],"no":[...]}, "asks":{"yes":[...],"no":[...]}}
    bids = orderbook.get("bids")
    asks = orderbook.get("asks")

    if isinstance(bids, dict):
        if best["yes_best_bid"] is None:
            best["yes_best_bid"] = _best_from_levels(bids.get("yes"), "bid")
        if best["no_best_bid"] is None:
            best["no_best_bid"] = _best_from_levels(bids.get("no"), "bid")

    if isinstance(asks, dict):
        if best["yes_best_ask"] is None:
            best["yes_best_ask"] = _best_from_levels(asks.get("yes"), "ask")
        if best["no_best_ask"] is None:
            best["no_best_ask"] = _best_from_levels(asks.get("no"), "ask")

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
    Create Order body with required lowercase keys.
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
    log.info(
        "[BOOT] BASE_URL=%s MARKET_TICKER=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s",
        BASE_URL, MARKET_TICKER, POLL_SECONDS, BUY_PRICE_CENTS, BASE_SIZE, POST_ONLY
    )

    while True:
        try:
            ob = get_orderbook(MARKET_TICKER)
            best = parse_best_levels(ob)
            log.info("[BEST] %s", json.dumps(best))

            # Micro-change: log spread for YES (maker must see bid/ask)
            if LOG_SPREAD:
                yb = best.get("yes_best_bid")
                ya = best.get("yes_best_ask")
                if yb and ya:
                    spread = int(ya["price_cents"]) - int(yb["price_cents"])
                    log.info(
                        "[SPREAD] YES bid=%dc(qty=%s) ask=%dc(qty=%s) spread=%dc",
                        int(yb["price_cents"]), yb.get("qty"),
                        int(ya["price_cents"]), ya.get("qty"),
                        spread,
                    )
                    if spread < 0:
                        log.warning(
                            "[SPREADWARN] ask < bid (unexpected). raw_yes_bid=%s raw_yes_ask=%s",
                            yb, ya
                        )
                else:
                    log.warning("[SPREAD] Missing YES bid/ask (yb=%s ya=%s). Orderbook shape likely different.", yb, ya)

            resting = list_orders_all("resting", limit=200, max_pages=MAX_PAGES)
            log.info("[ORDERS] resting_count=%d", len(resting))

            # YES-only for now: place one tiny buy each loop (you’ll change this soon)
            create_order_yes_buy(MARKET_TICKER, BUY_PRICE_CENTS, BASE_SIZE)

        except Exception as e:
            log.error("[LOOPERR] %s", e, exc_info=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
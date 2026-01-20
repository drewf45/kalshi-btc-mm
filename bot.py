import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List, Tuple
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
# Config (env)
# -----------------------------
API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/")
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip()  # optional

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip() or None

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))

FARM_SIDE = os.getenv("FARM_SIDE", "YES").upper().strip()  # YES / NO
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))

BOT_TAG = os.getenv("BOT_TAG", "MMBOT").strip()
MAX_BOT_RESTING_PER_TICKER = int(os.getenv("MAX_BOT_RESTING_PER_TICKER", "1"))
CLEANUP_ON_START = os.getenv("CLEANUP_ON_START", "true").lower() == "true"
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))

# debug knobs
DUMP_ORDERBOOK_ONCE = os.getenv("DUMP_ORDERBOOK_ONCE", "true").lower() == "true"


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def _require_env() -> None:
    if not KALSHI_KEY_ID:
        raise RuntimeError("Missing env var: KALSHI_KEY_ID")
    if not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")


def load_private_key() -> Any:
    key_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(key_bytes, password=None)


PRIVATE_KEY = None


def sha256_b64(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return base64.b64encode(digest.finalize()).decode("utf-8")


def sign_request(private_key: Any, ts_ms: int, method: str, signed_path: str, body_bytes: bytes) -> str:
    """
    IMPORTANT: Kalshi signature must match EXACT request target:
      timestamp + METHOD + (path + ?query if present) + body
    """
    method = method.upper()
    message = f"{ts_ms}{method}{signed_path}".encode("utf-8") + body_bytes
    signature = private_key.sign(
        message,
        asy_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(method: str, signed_path: str, body_bytes: bytes) -> Dict[str, str]:
    ts = now_utc_ts_ms()
    sig = sign_request(PRIVATE_KEY, ts, method, signed_path, body_bytes)

    hdrs = {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Content-Type": "application/json",
    }
    if SUBACCOUNT:
        hdrs["KALSHI-SUBACCOUNT"] = SUBACCOUNT

    log.info(
        f"[SIGNDBG] {method.upper()} {signed_path} ts={ts}ms "
        f"body_len={len(body_bytes)} signing_payload_sha256_b64={sha256_b64((str(ts)+method.upper()+signed_path).encode('utf-8')+body_bytes)}"
    )
    return hdrs


def http_json(method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    # Build query exactly (and use it for BOTH URL + SIGNED PATH)
    qs = ""
    if params:
        # urlencode preserves insertion order of dict; our dicts are built deterministically
        qs = urlencode(params)

    signed_path = path + (("?" + qs) if qs else "")
    url = f"{API_BASE}{signed_path}"

    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")

    headers = auth_headers(method, signed_path, body_bytes)
    resp = requests.request(method, url, headers=headers, data=body_bytes if body is not None else None, timeout=20)

    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    return resp.status_code, data


# -----------------------------
# API prefix discovery
# -----------------------------
API_PREFIX = None


def discover_api_prefix() -> str:
    candidates = ["/trade-api/v2"]
    for pref in candidates:
        code, data = http_json("GET", f"{pref}/markets", params={"limit": 1})
        if code == 200 and isinstance(data, dict):
            log.info(f"Discovered API prefix: {pref} (probe {pref}/markets?limit=1 -> {code})")
            log.info(f"Markets probe HTTP={code} shape=dict keys={list(data.keys())}")
            return pref
    raise RuntimeError("Could not discover API prefix (expected /trade-api/v2)")


# -----------------------------
# Market selection
# -----------------------------
def get_series_markets(series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    code, data = http_json("GET", f"{API_PREFIX}/markets", params={"series_ticker": series_ticker, "limit": limit})
    log.info(f"[SERIES] GET {API_PREFIX}/markets?series_ticker={series_ticker}&limit={limit} -> HTTP={code} shape=dict keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    if code != 200:
        raise RuntimeError(f"Series markets failed: HTTP={code} body={data}")
    mkts = data.get("markets", [])
    log.info(f"[SERIES] Returned markets count={len(mkts)}")
    return mkts


def pick_next_closing_market(markets: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    best = None
    best_dt = None
    for m in markets:
        ct = m.get("close_time")
        if not ct:
            continue
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt <= now:
            continue
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = m
    if not best:
        raise RuntimeError("No future-closing markets found in series.")
    secs = int((best_dt - now).total_seconds())
    log.info(f"[SELECT] Next closing market: {best.get('ticker')} close={best.get('close_time')} seconds_to_close={secs}")
    return best


# -----------------------------
# Orderbook
# -----------------------------
_ob_dumped = False


def get_orderbook(ticker: str) -> Any:
    code, data = http_json("GET", f"{API_PREFIX}/markets/{ticker}/orderbook")
    log.info(f"[BOOK] GET {API_PREFIX}/markets/{ticker}/orderbook -> HTTP={code} shape=dict keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    if code != 200:
        raise RuntimeError(f"Orderbook failed: HTTP={code} body={data}")
    return data.get("orderbook")


def _best_from_list(levels: Any) -> Optional[Dict[str, int]]:
    """
    levels can be:
      [[price, qty], ...]
    """
    if isinstance(levels, list) and levels:
        top = levels[0]
        if isinstance(top, (list, tuple)) and len(top) >= 2:
            return {"price_cents": int(top[0]), "qty": int(top[1])}
    return None


def best_asks(orderbook: Any) -> Dict[str, Optional[Dict[str, int]]]:
    """
    Robust for multiple shapes.

    Seen shapes:
      A) orderbook = {"yes": {"asks": [[p,q],...], "bids": ...}, "no": {...}}
      B) orderbook = {"yes": [[p,q],...], "no": [[p,q],...]}  (LIST directly)
      C) orderbook = [{"side":"yes","asks":[...]}] etc (less common)
    We only need best ask for yes/no.
    """

    def _best_side(side_key: str) -> Optional[Dict[str, int]]:
        if orderbook is None:
            return None

        # dict shapes
        if isinstance(orderbook, dict):
            side_val = orderbook.get(side_key)

            # B) side_val is LIST already
            if isinstance(side_val, list):
                return _best_from_list(side_val)

            # A) side_val is dict with asks
            if isinstance(side_val, dict):
                asks = side_val.get("asks") or []
                return _best_from_list(asks)

            return None

        # list shapes (C)
        if isinstance(orderbook, list):
            for item in orderbook:
                if not isinstance(item, dict):
                    continue
                if item.get("side") == side_key:
                    asks = item.get("asks") or item.get("levels") or []
                    return _best_from_list(asks)

        return None

    return {
        "yes_best_ask": _best_side("yes"),
        "no_best_ask": _best_side("no"),
    }


# -----------------------------
# Orders
# -----------------------------
def list_orders_page(status: str, limit: int = 200, cursor: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    params: Dict[str, Any] = {"status": status, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    code, data = http_json("GET", f"{API_PREFIX}/portfolio/orders", params=params)
    log.info(f"[ORDERS] GET {API_PREFIX}/portfolio/orders?status={status}&limit={limit} -> HTTP={code} shape=dict keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    if code != 200:
        raise RuntimeError(f"List orders failed: HTTP={code} body={data}")
    orders = data.get("orders", []) or []
    next_cursor = data.get("cursor")
    return orders, next_cursor


def list_orders_all(status: str, limit: int = 200, max_pages: int = 10) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        page, cursor = list_orders_page(status=status, limit=limit, cursor=cursor)
        out.extend(page)
        if not cursor:
            break
    return out


def cancel_order(order_id: str) -> bool:
    code, data = http_json("DELETE", f"{API_PREFIX}/portfolio/orders/{order_id}")
    if code in (200, 204):
        log.info(f"[CANCEL] DELETE order_id={order_id} -> HTTP={code}")
        return True

    code2, data2 = http_json("POST", f"{API_PREFIX}/portfolio/orders/{order_id}/cancel", body={})
    if code2 in (200, 204):
        log.info(f"[CANCEL] POST order_id={order_id}/cancel -> HTTP={code2}")
        return True

    log.warning(f"[CANCEL] failed order_id={order_id} HTTP={code} body={data} fallbackHTTP={code2} fallbackBody={data2}")
    return False


def create_order(
    ticker: str,
    side: str,          # "yes" or "no"
    action: str,        # "buy" or "sell"
    count: int,
    price_cents: int,
    client_order_id: str,
) -> Dict[str, Any]:
    body = {
        "ticker": ticker,
        "action": action,
        "side": side,
        "count": count,
        "type": "limit",
        "client_order_id": client_order_id,
    }
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents

    code, data = http_json("POST", f"{API_PREFIX}/portfolio/orders", body=body)
    log.info(f"[PLACE] POST {API_PREFIX}/portfolio/orders -> HTTP={code} keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    if code != 201:
        raise RuntimeError(f"Create order failed: HTTP={code} body={data}")
    return data["order"]


def bot_client_id(ticker: str, action: str, side: str, price_cents: int) -> str:
    return f"{BOT_TAG}:{ticker}:{action}:{side}:{price_cents}"


def cleanup_bot_orders_for_ticker(resting: List[Dict[str, Any]], ticker: str, keep_client_id: Optional[str]) -> None:
    bot_orders = []
    for o in resting:
        if o.get("ticker") != ticker:
            continue
        cid = (o.get("client_order_id") or "").strip()
        if cid.startswith(f"{BOT_TAG}:"):
            bot_orders.append(o)

    if not bot_orders:
        return

    cancels = 0
    for o in bot_orders:
        cid = (o.get("client_order_id") or "").strip()
        if keep_client_id and cid == keep_client_id:
            continue
        oid = o.get("order_id")
        if oid and cancel_order(oid):
            cancels += 1

    if cancels:
        log.info(f"[CLEAN] canceled {cancels} bot resting orders on ticker={ticker}")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    global PRIVATE_KEY, API_PREFIX, _ob_dumped

    _require_env()
    PRIVATE_KEY = load_private_key()
    log.info("Loaded RSA private key from KALSHI_PRIVATE_KEY_B64.")

    API_PREFIX = discover_api_prefix()

    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"MARKET_TICKER={MARKET_TICKER}")
    log.info(f"API_BASE={API_BASE}")
    log.info(f"SUBACCOUNT={SUBACCOUNT or '(none)'}")
    log.info(f"FARM_SIDE={FARM_SIDE} BUY_PRICE_CENTS={BUY_PRICE_CENTS} BASE_SIZE={BASE_SIZE}")
    log.info(f"BOT_TAG={BOT_TAG} MAX_BOT_RESTING_PER_TICKER={MAX_BOT_RESTING_PER_TICKER} CLEANUP_ON_START={CLEANUP_ON_START}")

    if MARKET_TICKER:
        active_ticker = MARKET_TICKER
        log.info(f"[SELECT] Using MARKET_TICKER override: {active_ticker}")
    else:
        markets = get_series_markets(SERIES_PREFIX, limit=200)
        m = pick_next_closing_market(markets)
        active_ticker = m["ticker"]

    side = "yes" if FARM_SIDE == "YES" else "no"
    action = "buy"

    if CLEANUP_ON_START:
        try:
            resting_all = list_orders_all("resting", limit=200, max_pages=MAX_PAGES)
            log.info(f"[ORDERS] resting_total={len(resting_all)} (paged up to {MAX_PAGES})")
            cleanup_bot_orders_for_ticker(resting_all, ticker=active_ticker, keep_client_id=None)
        except Exception as e:
            log.warning(f"[CLEAN] startup cleanup skipped due to error: {e}")

    while True:
        try:
            ob = get_orderbook(active_ticker)

            # One-time dump so we can see the exact shape in logs if needed
            if DUMP_ORDERBOOK_ONCE and not _ob_dumped:
                try:
                    log.info(f"[OBDUMP] orderbook_type={type(ob).__name__} sample={json.dumps(ob)[:800]}")
                except Exception:
                    log.info(f"[OBDUMP] orderbook_type={type(ob).__name__} (not json-serializable)")
                _ob_dumped = True

            b = best_asks(ob)
            log.info(f"[BEST] {json.dumps(b)}")

            resting_all = list_orders_all("resting", limit=200, max_pages=MAX_PAGES)
            log.info(f"[ORDERS] resting_total={len(resting_all)} (paged up to {MAX_PAGES})")

            desired_client_id = bot_client_id(active_ticker, action, side, BUY_PRICE_CENTS)

            already = False
            bot_resting_for_ticker = 0
            for o in resting_all:
                if o.get("ticker") != active_ticker:
                    continue
                cid = (o.get("client_order_id") or "").strip()
                if cid.startswith(f"{BOT_TAG}:"):
                    bot_resting_for_ticker += 1
                if cid == desired_client_id:
                    already = True

            cleanup_bot_orders_for_ticker(resting_all, ticker=active_ticker, keep_client_id=desired_client_id)

            if bot_resting_for_ticker > MAX_BOT_RESTING_PER_TICKER:
                log.warning(
                    f"[GUARD] bot_resting_for_ticker={bot_resting_for_ticker} exceeds cap={MAX_BOT_RESTING_PER_TICKER}. "
                    f"Skipping new placement this loop."
                )
                time.sleep(POLL_SECONDS)
                continue

            if already:
                log.info(f"[HOLD] desired order already resting client_order_id={desired_client_id}")
                time.sleep(POLL_SECONDS)
                continue

            if not ENABLE_TRADING:
                log.info("[DRYRUN] ENABLE_TRADING is false; not placing.")
                time.sleep(POLL_SECONDS)
                continue
            if not CONFIRM_LIVE_TRADING:
                log.info("[SAFETY] CONFIRM_LIVE_TRADING is false; not placing.")
                time.sleep(POLL_SECONDS)
                continue

            order = create_order(
                active_ticker,
                side=side,
                action=action,
                count=BASE_SIZE,
                price_cents=BUY_PRICE_CENTS,
                client_order_id=desired_client_id,
            )
            log.info(
                f"[TRADE] placed {action.upper()} {side.upper()} {BASE_SIZE} @ {BUY_PRICE_CENTS}c "
                f"on {active_ticker} client_order_id={desired_client_id} order_id={order.get('order_id')}"
            )

        except Exception as e:
            log.error(f"[LOOPERR] {e}", exc_info=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
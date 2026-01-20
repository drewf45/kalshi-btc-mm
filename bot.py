import os
import json
import time
import base64
import binascii
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Env helpers
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Kalshi auth/sign
# -----------------------------
def load_private_key() -> Any:
    """
    Supports:
      - KALSHI_PRIVATE_KEY_B64: base64-encoded PEM
      - KALSHI_PRIVATE_KEY_PATH: path to PEM file
    """
    b64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()

    pem_bytes: Optional[bytes] = None

    if b64:
        try:
            pem_bytes = base64.b64decode(b64, validate=True)
        except binascii.Error as e:
            raise RuntimeError(f"KALSHI_PRIVATE_KEY_B64 is not valid base64: {e}")

        if b"BEGIN" not in pem_bytes:
            raise RuntimeError(
                "Decoded KALSHI_PRIVATE_KEY_B64 does not look like a PEM (missing 'BEGIN'). "
                "Make sure you base64-encoded the PEM file contents."
            )

        log.info("Loaded RSA private key from KALSHI_PRIVATE_KEY_B64.")
        log.info(f"[BOOT] KALSHI_PRIVATE_KEY_B64 len={len(b64)} decoded_pem_bytes={len(pem_bytes)}")

    elif path:
        with open(path, "rb") as f:
            pem_bytes = f.read()
        log.info("Loaded RSA private key from KALSHI_PRIVATE_KEY_PATH.")
        log.info(f"[BOOT] KALSHI_PRIVATE_KEY_PATH={path} pem_bytes={len(pem_bytes)}")

    else:
        raise RuntimeError("Missing PRIVATE_KEY (set KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PATH).")

    priv = serialization.load_pem_private_key(pem_bytes, password=None)

    # safe pubkey fingerprint for matching key-id/key-pair issues
    try:
        pub = priv.public_key()
        pub_der = pub.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        h = hashes.Hash(hashes.SHA256())
        h.update(pub_der)
        fp = h.finalize().hex()[:16]
        log.info(f"[BOOT] pubkey_fp={fp}")
    except Exception:
        pass

    return priv


PRIVATE_KEY = load_private_key()

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
if not KALSHI_KEY_ID:
    # fallback
    KALSHI_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()

API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").strip()

# discovered at runtime
API_PREFIX = os.getenv("API_PREFIX", "").strip()  # optional override


def sign_request(method: str, path: str, ts_ms: int, body: bytes) -> Dict[str, str]:
    if PRIVATE_KEY is None:
        raise RuntimeError("Missing PRIVATE_KEY (set KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PATH).")
    if not KALSHI_KEY_ID:
        raise RuntimeError("Missing KALSHI_KEY_ID (or KALSHI_API_KEY_ID).")

    # canonical signing payload
    payload = (method.upper() + "\n" + path + "\n" + str(ts_ms) + "\n").encode("utf-8") + body

    # --- MICRO FIX ---
    # Sign the RAW payload bytes. cryptography hashes internally when you specify hashes.SHA256().
    signature = PRIVATE_KEY.sign(payload, asy_padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode("utf-8")
    # --- END MICRO FIX ---

    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
        "Content-Type": "application/json",
    }

    # Debug: keep your hash logging (hash of payload bytes)
    try:
        digest = hashes.Hash(hashes.SHA256())
        digest.update(payload)
        payload_hash = digest.finalize()
        sha_b64 = base64.b64encode(payload_hash).decode("utf-8")
        log.info(f"[SIGNDBG] {method.upper()} {path} ts={ts_ms}ms body_len={len(body)} signing_payload_sha256_b64={sha_b64}")
    except Exception:
        pass

    return headers


def kalshi_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None
) -> Tuple[int, Any]:
    if params:
        qs = urlencode(params)
        full_path = f"{path}?{qs}"
    else:
        full_path = path

    body_bytes = b""
    if json_body is not None:
        body_bytes = json.dumps(json_body, separators=(",", ":")).encode("utf-8")

    ts = now_ms()
    headers = sign_request(method, full_path, ts, body_bytes)

    url = API_BASE.rstrip("/") + full_path

    resp = requests.request(method.upper(), url, headers=headers, data=body_bytes if body_bytes else None, timeout=15)
    text = resp.text.strip()
    try:
        data = resp.json() if text else {}
    except Exception:
        data = {"raw": text}

    return resp.status_code, data


def discover_api_prefix() -> str:
    global API_PREFIX
    if API_PREFIX:
        return API_PREFIX

    code, data = kalshi_request("GET", "/trade-api/v2/markets", params={"limit": 1})
    if code == 200:
        API_PREFIX = "/trade-api/v2"
        log.info("Discovered API prefix: /trade-api/v2 (probe /trade-api/v2/markets?limit=1 -> 200)")
        shape = "dict" if isinstance(data, dict) else type(data).__name__
        keys = list(data.keys()) if isinstance(data, dict) else []
        log.info(f"Markets probe HTTP={code} shape={shape} keys={keys}")
        return API_PREFIX

    raise RuntimeError(f"Could not discover API prefix (probe status={code} body={data})")


# -----------------------------
# Market + book helpers
# -----------------------------
def get_series_markets(series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    prefix = discover_api_prefix()
    code, data = kalshi_request("GET", f"{prefix}/markets", params={"series_ticker": series_ticker, "limit": limit})
    shape = "dict" if isinstance(data, dict) else type(data).__name__
    keys = list(data.keys()) if isinstance(data, dict) else []
    log.info(f"[SERIES] GET {prefix}/markets?series_ticker={series_ticker}&limit={limit} -> HTTP={code} shape={shape} keys={keys}")
    if code != 200:
        raise RuntimeError(f"Series markets fetch failed: HTTP={code} body={data}")
    markets = data.get("markets", []) if isinstance(data, dict) else []
    log.info(f"[SERIES] Returned markets count={len(markets)}")
    return markets


def parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def select_next_closing_market(markets: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    best = None
    best_dt = None

    for m in markets:
        close = m.get("close_time") or m.get("close_time_utc") or m.get("closeTime")
        if not close:
            continue
        try:
            close_dt = parse_iso(close)
        except Exception:
            continue
        if close_dt <= now:
            continue
        if best_dt is None or close_dt < best_dt:
            best_dt = close_dt
            best = m

    if not best or not best_dt:
        raise RuntimeError("No future-closing market found in series list.")

    seconds_to_close = int((best_dt - now).total_seconds())
    log.info(f"[SELECT] Next closing market: {best.get('ticker')} close={best_dt.isoformat().replace('+00:00','Z')} seconds_to_close={seconds_to_close}")
    return best


def get_orderbook(market_ticker: str) -> Dict[str, Any]:
    prefix = discover_api_prefix()
    code, data = kalshi_request("GET", f"{prefix}/markets/{market_ticker}/orderbook")
    shape = "dict" if isinstance(data, dict) else type(data).__name__
    keys = list(data.keys()) if isinstance(data, dict) else []
    log.info(f"[BOOK] GET {prefix}/markets/{market_ticker}/orderbook -> HTTP={code} shape={shape} keys={keys}")
    if code != 200:
        raise RuntimeError(f"Orderbook fetch failed: HTTP={code} body={data}")
    return data


def best_levels_from_orderbook(ob: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "yes_best_bid": None,
        "yes_best_ask": None,
        "no_best_bid": None,
        "no_best_ask": None,
    }

    book = ob.get("orderbook", {}) if isinstance(ob, dict) else {}
    yes = book.get("yes", {}) if isinstance(book, dict) else {}
    no = book.get("no", {}) if isinstance(book, dict) else {}

    def pick_best(side_obj: Dict[str, Any], which: str) -> Optional[Dict[str, int]]:
        arr = side_obj.get(which, [])
        if not isinstance(arr, list) or not arr:
            return None
        lvl = arr[0]
        if not isinstance(lvl, dict):
            return None
        price = lvl.get("price") or lvl.get("price_cents")
        qty = lvl.get("count") or lvl.get("quantity") or lvl.get("qty")
        if price is None:
            return None
        try:
            price_i = int(price)
        except Exception:
            return None
        try:
            qty_i = int(qty) if qty is not None else 0
        except Exception:
            qty_i = 0
        return {"price_cents": price_i, "qty": qty_i}

    out["yes_best_bid"] = pick_best(yes, "bids")
    out["yes_best_ask"] = pick_best(yes, "asks")
    out["no_best_bid"] = pick_best(no, "bids")
    out["no_best_ask"] = pick_best(no, "asks")

    log.info("[BEST] %s", json.dumps({k: v for k, v in out.items() if v is not None}))
    return out


# -----------------------------
# Orders helpers
# -----------------------------
def list_orders(status: str, limit: int = 200) -> List[Dict[str, Any]]:
    prefix = discover_api_prefix()
    code, data = kalshi_request("GET", f"{prefix}/portfolio/orders", params={"status": status, "limit": limit})
    shape = "dict" if isinstance(data, dict) else type(data).__name__
    keys = list(data.keys()) if isinstance(data, dict) else []
    log.info(f"[ORDERS] GET {prefix}/portfolio/orders?status={status}&limit={limit} -> HTTP={code} shape={shape} keys={keys}")
    if code != 200:
        raise RuntimeError(f"List orders failed: HTTP={code} body={data}")
    return data.get("orders", []) if isinstance(data, dict) else []


def cancel_order(order_id: str) -> bool:
    prefix = discover_api_prefix()

    code, data = kalshi_request("DELETE", f"{prefix}/portfolio/orders/{order_id}")
    if code in (200, 204):
        log.info(f"[CANCEL] order_id={order_id} -> HTTP={code}")
        return True

    code2, data2 = kalshi_request("POST", f"{prefix}/portfolio/orders/{order_id}/cancel")
    if code2 in (200, 204):
        log.info(f"[CANCEL] order_id={order_id} -> HTTP={code2} (fallback)")
        return True

    log.warning(f"[CANCEL] Failed order_id={order_id} HTTP={code} body={data} fallback_http={code2} fallback_body={data2}")
    return False


def place_order(ticker: str, action: str, side: str, price_cents: int, count: int, subaccount: Optional[str] = None) -> Tuple[int, Any]:
    prefix = discover_api_prefix()
    payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": action,
        "side": side,
        "type": "limit",
        "count": int(count),
    }
    if side == "yes":
        payload["yes_price"] = int(price_cents)
    else:
        payload["no_price"] = int(price_cents)

    if subaccount:
        payload["subaccount"] = subaccount

    code, data = kalshi_request("POST", f"{prefix}/portfolio/orders", json_body=payload)
    log.info(f"[ORDER] POST {prefix}/portfolio/orders -> {code} {json.dumps(data)[:1200]}")
    return code, data


def find_resting_order(orders: List[Dict[str, Any]], ticker: str, action: str, side: str) -> Optional[Dict[str, Any]]:
    for o in orders:
        if o.get("ticker") != ticker:
            continue
        if o.get("action") != action:
            continue
        if o.get("side") != side:
            continue
        if o.get("status") != "resting":
            continue
        return o
    return None


def order_price_cents(order: Dict[str, Any]) -> Optional[int]:
    if order.get("side") == "yes":
        v = order.get("yes_price")
    else:
        v = order.get("no_price")
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def order_age_seconds(order: Dict[str, Any]) -> Optional[int]:
    ct = order.get("created_time")
    if not ct:
        return None
    try:
        dt = parse_iso(ct)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


# -----------------------------
# Main strategy loop (1 market at a time)
# -----------------------------
def main() -> None:
    ENABLE_TRADING = env_bool("ENABLE_TRADING", False)
    CONFIRM_LIVE_TRADING = env_bool("CONFIRM_LIVE_TRADING", False)

    POLL_SECONDS = env_int("POLL_SECONDS", 1)

    SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
    MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip() or None
    SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip() or None

    FARM_SIDE = os.getenv("FARM_SIDE", "YES").strip().upper()
    BUY_PRICE_CENTS = env_int("BUY_PRICE_CENTS", 1)
    TARGET_PROFIT_CENTS = env_int("TARGET_PROFIT_CENTS", 1)

    ENTRY_TTL_SECONDS = env_int("ENTRY_TTL_SECONDS", 20)
    EXIT_TTL_SECONDS = env_int("EXIT_TTL_SECONDS", 60)
    STALE_REPRICE = env_bool("STALE_REPRICE", True)

    ESCALATE_AFTER_POLLS = env_int("ESCALATE_AFTER_POLLS", 0)

    SIZE_BASE = env_int("SIZE_BASE", 1)

    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"MARKET_TICKER={MARKET_TICKER}")
    log.info(f"API_BASE={API_BASE}")
    log.info(f"SUBACCOUNT={SUBACCOUNT}")
    log.info(f"FARM_SIDE={FARM_SIDE} BUY_PRICE_CENTS={BUY_PRICE_CENTS} TARGET_PROFIT_CENTS={TARGET_PROFIT_CENTS}")
    log.info(f"ENTRY_TTL_SECONDS={ENTRY_TTL_SECONDS} EXIT_TTL_SECONDS={EXIT_TTL_SECONDS} STALE_REPRICE={STALE_REPRICE}")
    log.info(f"ESCALATE_AFTER_POLLS={ESCALATE_AFTER_POLLS} (0 disables)")
    log.info(f"SIZING base={SIZE_BASE}")
    log.info(f"[BOOT] KALSHI_KEY_ID len={len(KALSHI_KEY_ID)}")

    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        raise RuntimeError("Refusing to trade: ENABLE_TRADING=True but CONFIRM_LIVE_TRADING!=True")

    if MARKET_TICKER is None:
        markets = get_series_markets(SERIES_PREFIX, limit=200)
        m = select_next_closing_market(markets)
        MARKET_TICKER = m["ticker"]

    side = "yes" if FARM_SIDE == "YES" else "no"
    buy_price = BUY_PRICE_CENTS
    sell_price = min(99, buy_price + TARGET_PROFIT_CENTS)

    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    polls = 0

    while True:
        try:
            polls += 1

            resting = list_orders("resting", limit=200)

            entry_rest = find_resting_order(resting, MARKET_TICKER, "buy", side)
            exit_rest = find_resting_order(resting, MARKET_TICKER, "sell", side)

            if entry_rest:
                entry_order_id = entry_rest.get("order_id") or entry_rest.get("id") or entry_order_id
            if exit_rest:
                exit_order_id = exit_rest.get("order_id") or exit_rest.get("id") or exit_order_id

            if entry_rest:
                age = order_age_seconds(entry_rest)
                p = order_price_cents(entry_rest)

                if STALE_REPRICE and p is not None and p != buy_price:
                    log.warning(f"[ENTRY] Stale price detected resting_buy={p} desired={buy_price}. Cancelling order_id={entry_order_id}")
                    cancel_order(entry_order_id or "")
                    entry_order_id = None
                    entry_rest = None

                elif age is not None and age >= ENTRY_TTL_SECONDS:
                    log.warning(f"[ENTRY] TTL exceeded age={age}s >= {ENTRY_TTL_SECONDS}s. Cancelling order_id={entry_order_id}")
                    cancel_order(entry_order_id or "")
                    entry_order_id = None
                    entry_rest = None

                else:
                    log.info(f"[ENTRY] Resting ok order_id={entry_order_id} price={p} age={age}s")

            if not entry_rest and not exit_rest:
                ob = get_orderbook(MARKET_TICKER)
                _levels = best_levels_from_orderbook(ob)

                if ENABLE_TRADING:
                    log.info(f"[ORDER] BUY {side.upper()} {SIZE_BASE}@{buy_price}c on {MARKET_TICKER}")
                    code, data = place_order(
                        ticker=MARKET_TICKER,
                        action="buy",
                        side=side,
                        price_cents=buy_price,
                        count=SIZE_BASE,
                        subaccount=SUBACCOUNT,
                    )
                    if code == 201 and isinstance(data, dict) and "order" in data:
                        entry_order_id = data["order"].get("order_id")
                        log.info(f"[ENTRY] Placed entry order_id={entry_order_id}")
                    else:
                        log.warning(f"[ENTRY] Unexpected order response code={code} body={data}")

                else:
                    log.info("[DRYRUN] ENABLE_TRADING=False; skipping order placement.")

            if exit_rest:
                age = order_age_seconds(exit_rest)
                p = order_price_cents(exit_rest)

                if age is not None and age >= EXIT_TTL_SECONDS:
                    log.warning(f"[EXIT] TTL exceeded age={age}s >= {EXIT_TTL_SECONDS}s. Cancelling order_id={exit_order_id}")
                    cancel_order(exit_order_id or "")
                    exit_order_id = None
                else:
                    log.info(f"[EXIT] Resting ok order_id={exit_order_id} price={p} age={age}s")

            if not exit_rest:
                filled = list_orders("filled", limit=200)
                recent_fill = None
                for o in filled:
                    if o.get("ticker") != MARKET_TICKER:
                        continue
                    if o.get("action") != "buy":
                        continue
                    if o.get("side") != side:
                        continue
                    recent_fill = o
                    break

                if recent_fill:
                    if entry_rest:
                        log.warning("[STATE] Found filled buy but entry still resting. (Will proceed with exit placement)")

                    if ENABLE_TRADING:
                        log.info(f"[ORDER] SELL {side.upper()} {SIZE_BASE}@{sell_price}c on {MARKET_TICKER}")
                        code, data = place_order(
                            ticker=MARKET_TICKER,
                            action="sell",
                            side=side,
                            price_cents=sell_price,
                            count=SIZE_BASE,
                            subaccount=SUBACCOUNT,
                        )
                        if code == 201 and isinstance(data, dict) and "order" in data:
                            exit_order_id = data["order"].get("order_id")
                            log.info(f"[EXIT] Placed exit order_id={exit_order_id}")
                        else:
                            log.warning(f"[EXIT] Unexpected order response code={code} body={data}")
                    else:
                        log.info("[DRYRUN] ENABLE_TRADING=False; skipping exit placement.")

            log.info(f"[HEARTBEAT] alive ticker={MARKET_TICKER} polls={polls} entry_id={entry_order_id} exit_id={exit_order_id}")

        except Exception as e:
            log.error(f"[LOOPERR] {e}", exc_info=True)

        time.sleep(max(1, POLL_SECONDS))


if __name__ == "__main__":
    main()
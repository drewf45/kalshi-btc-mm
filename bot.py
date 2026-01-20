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
# Helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

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

def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default

# -----------------------------
# Kalshi Auth
# -----------------------------
def load_private_key_from_b64(b64_str: str):
    """
    Expects base64-encoded PEM bytes (the file contents).
    """
    pem_bytes = base64.b64decode(b64_str)
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    return key, pem_bytes

def pubkey_fingerprint_hex(private_key) -> str:
    pub = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    h = hashes.Hash(hashes.SHA256())
    h.update(pub)
    return h.finalize().hex()[:16]

def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """
    Kalshi requires signing: timestamp + METHOD + path_without_query
    IMPORTANT: do NOT include query params in the signed message.
    """
    path_no_q = path.split("?", 1)[0]
    msg = f"{timestamp_ms}{method.upper()}{path_no_q}".encode("utf-8")

    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

# -----------------------------
# Config
# -----------------------------
API_BASE = env_str("API_BASE", "https://api.elections.kalshi.com")
API_PREFIX = "/trade-api/v2"

KALSHI_KEY_ID = env_str("KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_B64 = env_str("KALSHI_PRIVATE_KEY_B64")

SUBACCOUNT = env_str("SUBACCOUNT")  # optional

ENABLE_TRADING = env_bool("ENABLE_TRADING", False)
CONFIRM_LIVE_TRADING = env_bool("CONFIRM_LIVE_TRADING", False)

POLL_SECONDS = env_int("POLL_SECONDS", 1)

SERIES_PREFIX = env_str("SERIES_PREFIX", "KXBTC15M")
MARKET_TICKER = env_str("MARKET_TICKER")  # if set, overrides auto-selection

FARM_SIDE = env_str("FARM_SIDE", "YES").upper()  # YES only right now
BUY_PRICE_CENTS = env_int("BUY_PRICE_CENTS", 1)

ENTRY_TTL_SECONDS = env_int("ENTRY_TTL_SECONDS", 20)
EXIT_TTL_SECONDS = env_int("EXIT_TTL_SECONDS", 60)

ESCALATE_AFTER_POLLS = env_int("ESCALATE_AFTER_POLLS", 0)  # 0 disables
BASE_SIZE = env_int("BASE_SIZE", 1)

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

# -----------------------------
# HTTP Wrapper
# -----------------------------
def kalshi_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    if params:
        qs = urlencode(params, doseq=True)
        full_path = f"{path}?{qs}"
    else:
        full_path = path

    url = f"{API_BASE}{full_path}"
    ts = str(now_ms())

    headers = {}
    if KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_B64:
        signature = sign_request(PRIVATE_KEY, ts, method, full_path)
        headers = {
            "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
        signed_path = full_path.split("?", 1)[0]
        log.info(f"[SIGNDBG] {method.upper()} {full_path} ts={ts}ms signed_path={signed_path}")

    data = None
    if body is not None:
        data = json.dumps(body)

    resp = SESSION.request(method=method.upper(), url=url, headers=headers, data=data, timeout=15)
    try:
        payload = resp.json()
    except Exception:
        payload = resp.text
    return resp.status_code, payload

# -----------------------------
# API Calls
# -----------------------------
def get_markets(series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    code, data = kalshi_request("GET", f"{API_PREFIX}/markets", params={"series_ticker": series_ticker, "limit": limit})
    log.info(f"[SERIES] GET {API_PREFIX}/markets?series_ticker={series_ticker}&limit={limit} -> HTTP={code} shape={type(data).__name__} keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}")
    if code != 200:
        raise RuntimeError(f"Get markets failed: HTTP={code} body={data}")
    return data.get("markets", [])

def list_orders(status: str = "resting", limit: int = 200) -> List[Dict[str, Any]]:
    code, data = kalshi_request("GET", f"{API_PREFIX}/portfolio/orders", params={"status": status, "limit": limit})
    log.info(f"[ORDERS] GET {API_PREFIX}/portfolio/orders?status={status}&limit={limit} -> HTTP={code} shape={type(data).__name__} keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}")
    if code != 200:
        raise RuntimeError(f"List orders failed: HTTP={code} body={data}")
    return data.get("orders", [])

def create_order(
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: int,
    order_type: str = "limit",
) -> Dict[str, Any]:
    """
    - 'side' is REQUIRED by Kalshi (CreateOrderRequest.Side)
    - Must provide exactly one of: yes_price / no_price (cents)
    """
    side_l = side.lower().strip()
    if side_l not in ("yes", "no"):
        raise ValueError(f"side must be 'yes' or 'no', got {side}")

    body: Dict[str, Any] = {
        "ticker": ticker,
        "action": action,      # "buy" or "sell"
        "type": order_type,    # "limit"
        "count": int(count),
        "client_order_id": f"mm-{ticker}-{now_ms()}",
        "side": side_l,        # ✅ REQUIRED FIELD
    }

    if side_l == "yes":
        body["yes_price"] = int(price_cents)
    else:
        body["no_price"] = int(price_cents)

    if SUBACCOUNT:
        body["subaccount"] = SUBACCOUNT

    code, data = kalshi_request("POST", f"{API_PREFIX}/portfolio/orders", body=body)
    log.info(f"[PLACE] POST {API_PREFIX}/portfolio/orders -> HTTP={code} keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}")
    if code not in (200, 201):
        raise RuntimeError(f"Create order failed: HTTP={code} body={data}")
    return data

# -----------------------------
# Market Selection
# -----------------------------
def parse_close_ts(mkt: Dict[str, Any]) -> Optional[int]:
    close = mkt.get("close_time") or mkt.get("closeTime") or mkt.get("close")
    if not close or not isinstance(close, str):
        return None
    try:
        if close.endswith("Z"):
            close = close.replace("Z", "+00:00")
        dt = datetime.fromisoformat(close)
        return int(dt.timestamp())
    except Exception:
        return None

def select_next_closing_market(markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    now_s = int(time.time())
    best = None
    best_close = None
    for m in markets:
        ts = parse_close_ts(m)
        if ts is None or ts <= now_s:
            continue
        if best is None or ts < best_close:
            best = m
            best_close = ts
    return best

# -----------------------------
# Boot
# -----------------------------
if not KALSHI_PRIVATE_KEY_B64:
    raise SystemExit("Missing env KALSHI_PRIVATE_KEY_B64")
if not KALSHI_KEY_ID:
    raise SystemExit("Missing env KALSHI_KEY_ID")

PRIVATE_KEY, PEM_BYTES = load_private_key_from_b64(KALSHI_PRIVATE_KEY_B64)
log.info("Loaded RSA private key from KALSHI_PRIVATE_KEY_B64.")
log.info(f"[BOOT] KALSHI_PRIVATE_KEY_B64 len={len(KALSHI_PRIVATE_KEY_B64)} decoded_pem_bytes={len(PEM_BYTES)}")
log.info(f"[BOOT] pubkey_fp={pubkey_fingerprint_hex(PRIVATE_KEY)}")

# -----------------------------
# Main Loop
# -----------------------------
def main():
    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"MARKET_TICKER={MARKET_TICKER}")
    log.info(f"API_BASE={API_BASE}")
    log.info(f"SUBACCOUNT={SUBACCOUNT}")
    log.info(f"FARM_SIDE={FARM_SIDE} BUY_PRICE_CENTS={BUY_PRICE_CENTS}")
    log.info(f"ENTRY_TTL_SECONDS={ENTRY_TTL_SECONDS} EXIT_TTL_SECONDS={EXIT_TTL_SECONDS}")
    log.info(f"ESCALATE_AFTER_POLLS={ESCALATE_AFTER_POLLS} (0 disables)")
    log.info(f"SIZING base={BASE_SIZE}")

    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        raise SystemExit("ENABLE_TRADING=True but CONFIRM_LIVE_TRADING is not True. Refusing to run live.")

    active_ticker = MARKET_TICKER

    while True:
        try:
            if not active_ticker:
                mkts = get_markets(SERIES_PREFIX, limit=200)
                log.info(f"[SERIES] Returned markets count={len(mkts)}")
                m = select_next_closing_market(mkts)
                if not m:
                    log.warning("[SELECT] No future markets found; sleeping.")
                    time.sleep(POLL_SECONDS)
                    continue
                active_ticker = m.get("ticker")
                close_time = m.get("close_time") or m.get("closeTime") or m.get("close")
                close_ts = parse_close_ts(m)
                secs_to_close = (close_ts - int(time.time())) if close_ts else None
                log.info(f"[SELECT] Next closing market: {active_ticker} close={close_time} seconds_to_close={secs_to_close}")

            resting = list_orders("resting", limit=200)
            log.info(f"[ORDERS] resting_count={len(resting)}")

            if not ENABLE_TRADING:
                log.info("[DRYRUN] Trading disabled; polling only.")
                time.sleep(POLL_SECONDS)
                continue

            # YES-only entry attempt
            side = "yes"
            action = "buy"
            create_order(active_ticker, side=side, action=action, count=BASE_SIZE, price_cents=BUY_PRICE_CENTS)
            log.info(f"[TRADE] placed {action.upper()} {side.upper()} {BASE_SIZE} @ {BUY_PRICE_CENTS}c on {active_ticker}")

            time.sleep(POLL_SECONDS)

        except RuntimeError as e:
            log.error(f"[LOOPERR] {e}", exc_info=True)
            time.sleep(max(POLL_SECONDS, 2))
        except Exception as e:
            log.error(f"[LOOPERR] {e}", exc_info=True)
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
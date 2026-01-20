import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

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
# Env
# -----------------------------
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.kalshi.com")
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
MARKET_TICKER = os.getenv("MARKET_TICKER", "KXBTC15M-26JAN200900-00")

LOOP_SECONDS = float(os.getenv("LOOP_SECONDS", "1"))
BUY_QTY = int(os.getenv("BUY_QTY", "1"))

# For now: YES-only, keep it simple
MAX_YES_PRICE_CENTS = int(os.getenv("MAX_YES_PRICE_CENTS", "99"))

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)

def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_private_key(path: str):
    with open(path, "rb") as f:
        key_bytes = f.read()
    return serialization.load_pem_private_key(key_bytes, password=None)

PRIVATE_KEY = load_private_key(KALSHI_PRIVATE_KEY_PATH) if KALSHI_PRIVATE_KEY_PATH else None

def sha256_b64(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return base64.b64encode(digest.finalize()).decode()

def sign_request(method: str, path: str, ts_ms: int, body: bytes) -> Dict[str, str]:
    """
    Kalshi signature: sign payload = f"{ts_ms}{method}{path}{sha256(body)}"
    (Your existing bot already does this; keep consistent with your working auth.)
    """
    if PRIVATE_KEY is None:
        raise RuntimeError("Missing PRIVATE_KEY (set KALSHI_PRIVATE_KEY_PATH).")
    body_hash_b64 = sha256_b64(body)
    signing_payload = f"{ts_ms}{method.upper()}{path}{body_hash_b64}".encode()

    signature = PRIVATE_KEY.sign(
        signing_payload,
        asy_padding.PKCS1v15(),
        hashes.SHA256()
    )
    sig_b64 = base64.b64encode(signature).decode()

    log.info(
        "[SIGNDBG] %s %s ts=%sms body_len=%s signing_payload_sha256_b64=%s",
        method.upper(), path, ts_ms, len(body), sha256_b64(signing_payload)
    )

    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
    }

def kalshi_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    url = f"{KALSHI_API_BASE}{path}"
    body = b""
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()

    ts_ms = now_utc_ts_ms()
    headers = sign_request(method, path, ts_ms, body)

    resp = SESSION.request(method.upper(), url, headers=headers, data=body if body else None, timeout=10)
    try:
        j = resp.json()
    except Exception:
        j = {"_raw": resp.text}

    log.info("[HTTP] %s %s -> %s", method.upper(), path, resp.status_code)
    if resp.status_code >= 400:
        log.warning("[HTTPERR] %s", j)
        raise RuntimeError(f"HTTP {resp.status_code}: {j}")
    return j

# -----------------------------
# Orderbook parsing (FIX)
# -----------------------------
def _normalize_levels(levels: Any) -> List[Tuple[int, int]]:
    """
    Kalshi docs: orderbook.yes and orderbook.no are arrays of bids
    where each bid is [price, quantity].  [oai_citation:1‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/orderbook_responses?utm_source=chatgpt.com)

    But we also tolerate dict-ish formats defensively.
    Returns list of (price_cents, qty).
    """
    out: List[Tuple[int, int]] = []
    if levels is None:
        return out

    if isinstance(levels, list):
        for x in levels:
            # Common: [price, qty]
            if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (int, float)) and isinstance(x[1], (int, float)):
                out.append((int(x[0]), int(x[1])))
                continue
            # Sometimes: {"price": 32, "quantity": 10} or variants
            if isinstance(x, dict):
                p = x.get("price") or x.get("price_cents")
                q = x.get("quantity") or x.get("qty")
                if p is not None and q is not None:
                    out.append((int(p), int(q)))
    elif isinstance(levels, dict):
        # If someone passes a dict keyed by price -> qty
        for k, v in levels.items():
            try:
                out.append((int(k), int(v)))
            except Exception:
                pass

    return out

def parse_best_from_bids_only(orderbook_json: dict) -> Tuple[dict, bool]:
    """
    Returns:
      yes_best_bid: {"price_cents": int, "qty": int} or None
      yes_best_ask: derived from no_best_bid => 100 - no_best_bid_price
      no_best_bid: ...
      no_best_ask: derived from yes_best_bid => 100 - yes_best_bid_price

    NOTE: asks are *derived*, because API returns bids only.  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/orderbook_responses?utm_source=chatgpt.com)
    """
    ob = orderbook_json.get("orderbook")
    if not isinstance(ob, dict):
        return ({"yes_best_bid": None, "yes_best_ask": None, "no_best_bid": None, "no_best_ask": None}, False)

    yes_levels = _normalize_levels(ob.get("yes"))
    no_levels = _normalize_levels(ob.get("no"))

    yes_best_bid = None
    no_best_bid = None

    if yes_levels:
        p, q = max(yes_levels, key=lambda t: t[0])  # best bid = highest price
        yes_best_bid = {"price_cents": p, "qty": q}

    if no_levels:
        p, q = max(no_levels, key=lambda t: t[0])
        no_best_bid = {"price_cents": p, "qty": q}

    # Derived asks (complements)
    yes_best_ask = None
    no_best_ask = None

    if no_best_bid is not None:
        yes_best_ask = {"price_cents": max(0, 100 - int(no_best_bid["price_cents"])), "qty": int(no_best_bid["qty"])}

    if yes_best_bid is not None:
        no_best_ask = {"price_cents": max(0, 100 - int(yes_best_bid["price_cents"])), "qty": int(yes_best_bid["qty"])}

    # Basic sanity:
    # In a clean book, yes_best_bid + no_best_bid <= 100 (otherwise crossed/arby-ish)
    ok = True
    if yes_best_bid and no_best_bid:
        if int(yes_best_bid["price_cents"]) + int(no_best_bid["price_cents"]) > 100:
            ok = False

    return (
        {
            "yes_best_bid": yes_best_bid,
            "yes_best_ask": yes_best_ask,
            "no_best_bid": no_best_bid,
            "no_best_ask": no_best_ask,
        },
        ok,
    )

# -----------------------------
# Market Data
# -----------------------------
def get_orderbook(ticker: str) -> dict:
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    j = kalshi_request("GET", path)
    log.info("[BOOK] GET %s -> keys=%s", path, list(j.keys()) if isinstance(j, dict) else type(j).__name__)
    return j

# -----------------------------
# Trading (YES-only, minimal)
# -----------------------------
def place_limit_buy_yes(ticker: str, price_cents: int, qty: int) -> dict:
    """
    NOTE: You must match your account’s required fields.
    This is a typical shape; if your current bot already has working place-order payload,
    swap only the 'price' and 'count' fields into your existing payload shape.
    """
    path = "/trade-api/v2/orders"
    payload = {
        "ticker": ticker,
        "client_order_id": f"mm-yes-{int(time.time()*1000)}",
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": qty,
        "price": price_cents,
    }
    return kalshi_request("POST", path, payload)

def decide_yes_bid(best: dict) -> Optional[Tuple[int, int]]:
    """
    Strategy (still simple):
    - We want to rest near top-of-book, but NEVER above derived YES ask.
    - If derived ask is missing, just don't trade.
    """
    yb = best["yes_best_bid"]
    ya = best["yes_best_ask"]

    if yb is None or ya is None:
        return None

    best_bid = int(yb["price_cents"])
    best_ask = int(ya["price_cents"])

    # If market is super wide, we step up by 1c from best bid (maker-ish),
    # but never cross the derived ask.
    target = min(best_bid + 1, best_ask - 1)

    # Guardrails
    target = max(1, min(target, MAX_YES_PRICE_CENTS))
    if target >= best_ask:
        # No room to be a maker without crossing; skip.
        return None

    return (target, BUY_QTY)

# -----------------------------
# Main loop
# -----------------------------
def main():
    log.info("Starting Kalshi YES-only MM bot @ %s ticker=%s", iso_utc(), MARKET_TICKER)

    while True:
        try:
            ob = get_orderbook(MARKET_TICKER)
            best, ok = parse_best_from_bids_only(ob)

            log.info("[BEST] %s ok=%s", json.dumps(best), ok)
            if not ok:
                log.warning("[MM] Book sanity failed (crossed?). Standing down.")
                time.sleep(LOOP_SECONDS)
                continue

            decision = decide_yes_bid(best)
            if decision is None:
                yb = best["yes_best_bid"]["price_cents"] if best["yes_best_bid"] else None
                ya = best["yes_best_ask"]["price_cents"] if best["yes_best_ask"] else None
                log.info("[MM] No valid maker YES bid (bid=%s ask=%s). Standing down.", yb, ya)
                time.sleep(LOOP_SECONDS)
                continue

            price_cents, qty = decision
            yb = best["yes_best_bid"]["price_cents"] if best["yes_best_bid"] else None
            ya = best["yes_best_ask"]["price_cents"] if best["yes_best_ask"] else None
            log.info("[MM] Desired BUY YES %s@%sc (bid=%s ask=%s MAX=%s)", qty, price_cents, yb, ya, MAX_YES_PRICE_CENTS)

            # 🔒 Keep this disabled until you confirm payload fields match your working bot:
            # resp = place_limit_buy_yes(MARKET_TICKER, price_cents, qty)
            # log.info("[ORDER] %s", resp)

        except Exception as e:
            log.exception("[LOOPERR] %s", e)

        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    main()
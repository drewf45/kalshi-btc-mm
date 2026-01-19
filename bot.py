import os
import json
import time
import base64
import logging
import hashlib
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")

# ----------------------------
# Env
# ----------------------------
API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = "/trade-api/v2"  # hardcode to avoid discovery hangs

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID")  # required
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64")  # required

# Market / behavior
MARKET_TICKER = os.getenv("MARKET_TICKER", "KXBTC15M-26JAN190845")  # you can change
SUBACCOUNT = os.getenv("SUBACCOUNT")  # optional
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

# tiny test order settings
ORDER_SIDE = os.getenv("ORDER_SIDE", "yes").lower()  # yes/no
ORDER_PRICE_CENTS = int(os.getenv("ORDER_PRICE_CENTS", "1"))  # 1 cent default
ORDER_QTY = int(os.getenv("ORDER_QTY", "1"))  # 1 contract default

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))  # seconds
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))

# ----------------------------
# Helpers
# ----------------------------
def die(msg: str):
    raise RuntimeError(msg)

def b64decode_str(s: str) -> bytes:
    return base64.b64decode(s.encode("utf-8"))

def load_rsa_private_key_from_b64(b64: str):
    raw = b64decode_str(b64)
    # raw may be PEM bytes OR the literal PEM text that was base64'ed
    # either way, serialization.load_pem_private_key can handle PEM bytes
    try:
        key = serialization.load_pem_private_key(raw, password=None)
        return key
    except Exception:
        # If the user base64'ed the PEM *string* including headers, raw is still PEM bytes,
        # so failing here means it's not PEM format at all.
        die("Could not load RSA private key from KALSHI_PRIVATE_KEY_B64. Ensure you base64-encoded the entire PEM file contents.")

def now_ms() -> int:
    return int(time.time() * 1000)

def canonical_body(body) -> str:
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(body, separators=(",", ":"), sort_keys=True)
    return str(body)

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# ----------------------------
# Kalshi Client (RSA signing)
# ----------------------------
class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key_b64: str, subaccount: str | None = None):
        self.api_base = api_base
        self.key_id = key_id
        self.subaccount = subaccount
        self.session = requests.Session()
        self.rsa_key = load_rsa_private_key_from_b64(private_key_b64)

    def _sign(self, message: bytes) -> str:
        sig = self.rsa_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str, body) -> dict:
        ts = str(now_ms())
        body_str = canonical_body(body)
        body_hash = sha256_hex(body_str)

        # Common Kalshi-style signing string pattern:
        # METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + BODY_SHA256
        signing_str = "\n".join([method.upper(), path, ts, body_hash])
        sig_b64 = self._sign(signing_str.encode("utf-8"))

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount
        return headers

    def request(self, method: str, path: str, body=None):
        url = f"{self.api_base}{path}"
        data = None if body is None else json.dumps(body)
        last_err = None

        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                headers = self._headers(method, path, body)
                resp = self.session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    data=data,
                    timeout=HTTP_TIMEOUT,
                )
                text = resp.text
                try:
                    parsed = resp.json()
                except Exception:
                    parsed = {"_raw": text}

                if resp.status_code >= 400:
                    log.error("HTTP %s %s -> %s %s", method, path, resp.status_code, text[:400])
                return resp.status_code, parsed
            except requests.RequestException as e:
                last_err = e
                log.warning("HTTP attempt %s/%s failed: %s", attempt, HTTP_RETRIES, repr(e))
                time.sleep(0.6 * attempt)

        die(f"HTTP request failed after retries: {repr(last_err)}")

    # Convenience endpoints
    def get_balance(self):
        return self.request("GET", f"{API_PREFIX}/balance")

    def get_market(self, ticker: str):
        return self.request("GET", f"{API_PREFIX}/markets/{ticker}")

    def get_orderbook(self, ticker: str):
        return self.request("GET", f"{API_PREFIX}/markets/{ticker}/orderbook")

    def place_order(self, ticker: str, side: str, price_cents: int, qty: int):
        # Kalshi typical order payload. If your API expects a different schema,
        # the error response will show it and we’ll adjust.
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": side.upper(),  # YES / NO
            "type": "limit",
            "price": price_cents,
            "count": qty,
        }
        return self.request("POST", f"{API_PREFIX}/orders", payload)

# ----------------------------
# Parsers + Debug
# ----------------------------
def dollars_from_balance_payload(payload):
    """
    We’ve seen balance endpoint shape:
      {'balance': int, 'portfolio_value': ..., 'updated_ts': ...}
    That 'balance' might be:
      - cents (int)
      - dollars*100 (int)
      - or something else
    We’ll log it and interpret conservatively as cents.
    """
    if not isinstance(payload, dict):
        return None

    bal = payload.get("balance")
    if isinstance(bal, int):
        # Assume cents (most common). Convert to dollars.
        return bal / 100.0
    if isinstance(bal, dict):
        # Sometimes APIs return {'cash': ...}
        for k in ["cash", "available_cash", "available", "usd", "cents"]:
            v = bal.get(k)
            if isinstance(v, int):
                return v / 100.0
            if isinstance(v, (float, str)):
                try:
                    return float(v)
                except Exception:
                    pass
    return None

def best_levels_from_orderbook(ob):
    """
    Orderbook shapes differ:
      - dict with keys like 'yes'/'no'
      - dict with 'bids'/'asks'
      - or nested 'orderbook'
    We'll robustly detect common variants and log the shape.
    """
    if not isinstance(ob, dict):
        return None

    # normalize
    root = ob.get("orderbook") if isinstance(ob.get("orderbook"), dict) else ob

    # Variant A: {'yes': {'bids': [...], 'asks': [...]}, 'no': {...}}
    if "yes" in root and isinstance(root["yes"], dict):
        yes = root["yes"]
        no = root.get("no", {})
        return {
            "yes_best_bid": (yes.get("bids") or [None])[0],
            "yes_best_ask": (yes.get("asks") or [None])[0],
            "no_best_bid": (no.get("bids") or [None])[0] if isinstance(no, dict) else None,
            "no_best_ask": (no.get("asks") or [None])[0] if isinstance(no, dict) else None,
        }

    # Variant B: {'bids': [...], 'asks': [...]}
    if "bids" in root and "asks" in root:
        return {
            "best_bid": (root.get("bids") or [None])[0],
            "best_ask": (root.get("asks") or [None])[0],
        }

    return None

# ----------------------------
# Main loop
# ----------------------------
def main():
    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", ENABLE_TRADING)
    log.info("CONFIRM_LIVE_TRADING=%s", CONFIRM_LIVE_TRADING)
    log.info("POLL_SECONDS=%s", POLL_SECONDS)
    log.info("MARKET_TICKER=%s", MARKET_TICKER)
    log.info("API_BASE=%s", API_BASE)
    log.info("SUBACCOUNT=%s", SUBACCOUNT)

    if not KALSHI_KEY_ID:
        die("Missing env var: KALSHI_KEY_ID")
    if not KALSHI_PRIVATE_KEY_B64:
        die("Missing env var: KALSHI_PRIVATE_KEY_B64")

    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)

    # Balance check
    code, bal_payload = kc.get_balance()
    log.info("Balance HTTP=%s shape=%s keys=%s", code, type(bal_payload).__name__, list(bal_payload.keys()) if isinstance(bal_payload, dict) else None)
    cash = dollars_from_balance_payload(bal_payload)
    log.info("Parsed cash=%s", f"${cash:,.2f}" if cash is not None else "UNKNOWN (see balance payload logs)")

    # Market + orderbook
    m_code, market = kc.get_market(MARKET_TICKER)
    log.info("Market HTTP=%s shape=%s keys=%s", m_code, type(market).__name__, list(market.keys()) if isinstance(market, dict) else None)
    if m_code >= 400:
        log.error("Market fetch failed. Check ticker. Exiting.")
        return

    ob_code, ob = kc.get_orderbook(MARKET_TICKER)
    log.info("Orderbook HTTP=%s shape=%s keys=%s", ob_code, type(ob).__name__, list(ob.keys()) if isinstance(ob, dict) else None)
    levels = best_levels_from_orderbook(ob)
    log.info("Orderbook best levels: %s", levels)

    # Optional: place a tiny test order
    if ENABLE_TRADING:
        if not CONFIRM_LIVE_TRADING:
            log.warning("ENABLE_TRADING true but CONFIRM_LIVE_TRADING is false. Not placing orders.")
        else:
            side = "YES" if ORDER_SIDE == "yes" else "NO"
            log.warning("PLACING ORDER: ticker=%s side=%s price_cents=%s qty=%s", MARKET_TICKER, side, ORDER_PRICE_CENTS, ORDER_QTY)
            o_code, o_resp = kc.place_order(MARKET_TICKER, side, ORDER_PRICE_CENTS, ORDER_QTY)
            log.info("Order response HTTP=%s shape=%s keys=%s", o_code, type(o_resp).__name__, list(o_resp.keys()) if isinstance(o_resp, dict) else None)
            log.info("Order response body (truncated): %s", json.dumps(o_resp)[:800])

    # Loop
    while True:
        try:
            ob_code, ob = kc.get_orderbook(MARKET_TICKER)
            levels = best_levels_from_orderbook(ob)
            log.info("Tick %s orderbook best: %s", datetime.now(timezone.utc).isoformat(), levels)
        except Exception as e:
            log.exception("Loop error: %r", e)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
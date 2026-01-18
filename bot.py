import os
import time
import json
import base64
import requests
from datetime import datetime, timezone
from typing import Dict, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key


# =========================
# CONFIG
# =========================
API_BASE = "https://api.kalshi.com/trade-api/v2"

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()  # 15-min BTC series
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").strip().lower() == "true"

# Strategy / Risk
IMBALANCE_THRESHOLD = float(os.getenv("IMBALANCE_THRESHOLD", "0.95"))  # 95% one-sided
TRADE_RISK_PCT = float(os.getenv("TRADE_RISK_PCT", "0.01"))            # 1% per trade
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.20"))    # 20% daily stop

# Kalshi creds (must exist for real trading)
API_KEY_ID = (os.getenv("KALSHI_API_KEY_ID") or "").strip()
PRIVATE_KEY_PEM_B64 = (os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64") or "").strip()

# Optional: “force this ticker” to test
MARKET_TICKER_OVERRIDE = (os.getenv("MARKET_TICKER_OVERRIDE") or "").strip()


# =========================
# LOGGING
# =========================
def log(msg: str):
    print(msg, flush=True)


# =========================
# TIME + MARKET
# =========================
def utc_now():
    return datetime.now(timezone.utc)

def current_15m_market_ticker() -> str:
    """
    15-minute BTC market ticker format:
      KXBTC15M-18JAN261930  (NO seconds)
    Uses UTC buckets so it matches Kalshi tickers.
    """
    now = utc_now().replace(second=0, microsecond=0)
    minute_bucket = (now.minute // 15) * 15
    bucket = now.replace(minute=minute_bucket)

    # Format: DDMMMYYHHMM (uppercase)
    stamp = bucket.strftime("%d%b%y%H%M").upper()
    return f"{SERIES_PREFIX}-{stamp}"


# =========================
# RSA SIGNING (CORRECT)
# =========================
_cached_private_key = None

def load_private_key():
    global _cached_private_key

    if _cached_private_key is not None:
        return _cached_private_key

    if not PRIVATE_KEY_PEM_B64:
        return None

    # Clean whitespace just in case Render stored with accidental spaces
    b64_clean = "".join(PRIVATE_KEY_PEM_B64.split())

    try:
        pem_bytes = base64.b64decode(b64_clean)
    except Exception as e:
        log(f"ERROR: Could not base64-decode KALSHI_PRIVATE_KEY_PEM_BASE64: {e}")
        return None

    try:
        _cached_private_key = load_pem_private_key(pem_bytes, password=None)
        return _cached_private_key
    except Exception as e:
        log(f"ERROR: Could not parse RSA private key PEM: {e}")
        return None


def kalshi_signature(method: str, path: str, body: str, ts: str) -> Optional[str]:
    """
    Kalshi v2 signing is RSA-SHA256 over:
      timestamp + method + path + body
    """
    pk = load_private_key()
    if pk is None:
        return None

    msg = (ts + method.upper() + path + body).encode("utf-8")

    sig = pk.sign(
        msg,
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")


def auth_headers(method: str, path: str, body: str) -> Optional[Dict[str, str]]:
    if not API_KEY_ID:
        return None

    ts = str(int(time.time()))
    sig = kalshi_signature(method, path, body, ts)
    if not sig:
        return None

    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


# =========================
# API WRAPPER
# =========================
def kalshi_request(method: str, path: str, payload=None):
    body = json.dumps(payload) if payload is not None else ""

    headers = auth_headers(method, path, body)
    if headers is None:
        raise RuntimeError("Kalshi credentials missing or invalid (cannot sign requests).")

    url = API_BASE + path
    resp = requests.request(method, url, headers=headers, data=body, timeout=20)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")

    return resp.json()


# =========================
# ACCOUNT + MARKET DATA
# =========================
def fetch_balance() -> float:
    data = kalshi_request("GET", "/portfolio/balance")
    # balance endpoint typically returns {"balance": <number>}
    bal = data.get("balance")
    if bal is None:
        raise RuntimeError(f"Unexpected balance payload: {data}")
    return float(bal)


def fetch_market(ticker: str) -> dict:
    data = kalshi_request("GET", f"/markets/{ticker}")
    m = data.get("market")
    if m is None:
        raise RuntimeError(f"Unexpected market payload: {data}")
    return m


def best_yes_no_asks(market: dict):
    yes_asks = market.get("yes_asks") or []
    no_asks = market.get("no_asks") or []
    yes_price = yes_asks[0]["price"] if yes_asks else None
    no_price = no_asks[0]["price"] if no_asks else None
    return yes_price, no_price


def market_imbalance(market: dict) -> Optional[float]:
    """
    Uses volume_yes / (volume_yes + volume_no)
    If no volume yet -> None
    """
    vy = market.get("volume_yes", 0) or 0
    vn = market.get("volume_no", 0) or 0
    total = vy + vn
    if total <= 0:
        return None
    return float(vy) / float(total)


# =========================
# ORDER PLACEMENT
# =========================
def place_order(ticker: str, side: str, price: int, qty: int):
    if not ENABLE_TRADING:
        log("READ-ONLY MODE: trade skipped")
        return

    payload = {
        "ticker": ticker,
        "side": side,      # "yes" or "no"
        "type": "limit",
        "price": int(price),
        "quantity": int(qty),
    }
    kalshi_request("POST", "/orders", payload)
    log(f"ORDER PLACED: {ticker} {side.upper()} qty={qty} price={price}")


# =========================
# MAIN LOOP
# =========================
daily_start_equity = None
last_day = None

def creds_healthcheck() -> bool:
    """
    Prints EXACTLY what’s missing and prevents the NoneType crash.
    """
    missing = []
    if not API_KEY_ID:
        missing.append("KALSHI_API_KEY_ID")
    if not PRIVATE_KEY_PEM_B64:
        missing.append("KALSHI_PRIVATE_KEY_PEM_BASE64")

    if missing:
        log("⚠️ WARNING: Kalshi credentials missing — running in READ-ONLY mode")
        log("Missing env vars: " + ", ".join(missing))
        return False

    # Try load key once
    pk = load_private_key()
    if pk is None:
        log("⚠️ WARNING: Private key failed to load — running in READ-ONLY mode")
        return False

    return True


def main():
    global daily_start_equity, last_day, ENABLE_TRADING

    log("=== BOT STARTED ===")
    log(f"ENABLE_TRADING={ENABLE_TRADING}")
    log(f"POLL_SECONDS={POLL_SECONDS}")
    log(f"SERIES_PREFIX={SERIES_PREFIX}")
    if MARKET_TICKER_OVERRIDE:
        log(f"MARKET_TICKER_OVERRIDE={MARKET_TICKER_OVERRIDE}")

    creds_ok = creds_healthcheck()

    # If creds not ok, force read-only (prevents “backwards” mistakes)
    if not creds_ok:
        ENABLE_TRADING = False

    while True:
        try:
            # Daily reset
            today = utc_now().date()
            if last_day != today:
                last_day = today
                if creds_ok:
                    daily_start_equity = fetch_balance()
                    log(f"New UTC trading day. Starting equity={daily_start_equity:.2f}")
                else:
                    daily_start_equity = None
                    log("New UTC trading day. (read-only)")

            # Market
            ticker = MARKET_TICKER_OVERRIDE or current_15m_market_ticker()
            log(f"CHECKING MARKET: {ticker}")

            # If creds missing, don’t call Kalshi endpoints (avoid spam)
            if not creds_ok:
                time.sleep(POLL_SECONDS)
                continue

            # Risk controls
            balance = fetch_balance()
            if daily_start_equity is not None:
                daily_pnl = balance - daily_start_equity
                if daily_pnl <= -daily_start_equity * MAX_DAILY_LOSS_PCT:
                    log("DAILY LOSS LIMIT HIT — STOPPING TRADES FOR TODAY")
                    time.sleep(POLL_SECONDS)
                    continue

            market = fetch_market(ticker)

            imb = market_imbalance(market)
            if imb is None:
                log("No liquidity yet")
                time.sleep(POLL_SECONDS)
                continue

            yes_price, no_price = best_yes_no_asks(market)

            # 1% per trade sizing
            # contracts ~$1 max payout, so qty approx = 1% equity in contracts
            qty = max(1, int(balance * TRADE_RISK_PCT))

            # Strategy:
            # If 95%+ of volume is YES -> crowd is YES-heavy -> take NO
            # If 95%+ is NO -> take YES
            if imb >= IMBALANCE_THRESHOLD:
                if no_price is None:
                    log("Edge found (crowd YES-heavy) but no NO asks yet")
                else:
                    log(f"EDGE: YES-heavy ({imb:.3f}) -> BUY NO @ {no_price} qty={qty}")
                    place_order(ticker, "no", no_price, qty)

            elif imb <= (1.0 - IMBALANCE_THRESHOLD):
                if yes_price is None:
                    log("Edge found (crowd NO-heavy) but no YES asks yet")
                else:
                    log(f"EDGE: NO-heavy ({imb:.3f}) -> BUY YES @ {yes_price} qty={qty}")
                    place_order(ticker, "yes", yes_price, qty)

            else:
                log(f"No edge (imbalance={imb:.3f})")

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
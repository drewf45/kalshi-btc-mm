import os
import time
import json
import base64
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc-15m")


# -----------------------------
# Config (Render env vars)
# -----------------------------
ET = ZoneInfo("America/New_York")

# MUST BE THIS for production per docs
DEFAULT_PROD_BASE = "https://api.elections.kalshi.com"
DEFAULT_DEMO_BASE = "https://demo-api.kalshi.co"

KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", DEFAULT_PROD_BASE).strip()

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
# Put the PRIVATE KEY PEM into this env var as BASE64 (see steps below)
KALSHI_PRIVATE_KEY_PEM_BASE64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30").strip())
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").strip().lower() in ("1", "true", "yes")

# Risk knobs (you mentioned these)
EXTREME_THRESHOLD = float(os.getenv("EXTREME_THRESHOLD", "0.95"))
BET_PCT_OF_LIQUIDITY = float(os.getenv("BET_PCT_OF_LIQUIDITY", "0.01"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.20"))

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})


# -----------------------------
# Helpers: Kalshi signing
# Per docs: signature = base64(RSA-PSS-SHA256(timestamp + METHOD + path_without_query))
# Headers:
#   KALSHI-ACCESS-KEY
#   KALSHI-ACCESS-TIMESTAMP (ms)
#   KALSHI-ACCESS-SIGNATURE
# -----------------------------
def load_private_key_from_env() -> object:
    if not KALSHI_PRIVATE_KEY_PEM_BASE64:
        raise RuntimeError(
            "Missing KALSHI_PRIVATE_KEY_PEM_BASE64. "
            "You MUST download your Kalshi .key file when you create the API key, "
            "then base64 it and paste into Render env vars."
        )
    try:
        pem_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_PEM_BASE64)
        return serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as e:
        raise RuntimeError(f"Failed to load private key from base64 env var: {e}")


def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{path_no_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def kalshi_headers(private_key, method: str, path: str) -> dict:
    if not KALSHI_API_KEY_ID:
        raise RuntimeError("Missing KALSHI_API_KEY_ID in env vars.")
    ts = str(int(time.time() * 1000))
    sig = sign_request(private_key, ts, method, path)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


def kalshi_get(private_key, path: str, timeout: int = 20) -> dict:
    url = KALSHI_API_BASE.rstrip("/") + path
    headers = kalshi_headers(private_key, "GET", path)
    resp = SESSION.get(url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")
    return resp.json()


# -----------------------------
# Market ticker generation
# Your market URLs look like: KXBTC15M-26JAN181815
# Which is: DDMMMYYHHMM (ET) upper month.
# We always "look ahead to the next 15 minutes"
# -----------------------------
def ceil_to_next_15(dt: datetime) -> datetime:
    # dt is timezone-aware
    minute = dt.minute
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        return (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return dt.replace(minute=next_q, second=0, microsecond=0)


def format_kalshi_time(dt: datetime) -> str:
    # DDMMMYYHHMM with upper month, e.g. 26JAN181815 (Jan 26 2018 18:15)
    # Here YY is year % 100; Kalshi uses 2-digit year in many tickers.
    day = f"{dt.day:02d}"
    mon = dt.strftime("%b").upper()
    yy = dt.strftime("%y")
    hhmm = dt.strftime("%H%M")
    return f"{day}{mon}{yy}{hhmm}"


def next_market_ticker(now_et: datetime) -> str:
    nxt = ceil_to_next_15(now_et)
    return f"{SERIES_PREFIX}-{format_kalshi_time(nxt)}"


# -----------------------------
# Main loop
# -----------------------------
def main():
    # Hard safety: force correct prod base unless user intentionally overrides
    if "api.elections.kalshi.com" not in KALSHI_API_BASE and "demo-api.kalshi.co" not in KALSHI_API_BASE:
        log.warning(f"KALSHI_API_BASE looks unusual: {KALSHI_API_BASE}")

    private_key = load_private_key_from_env()

    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"API_BASE={KALSHI_API_BASE}")

    while True:
        try:
            now_et = datetime.now(ET)
            ticker = next_market_ticker(now_et)
            log.info(f"Heartbeat ET now={now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} | nextTicker={ticker}")

            # 1) Try direct fetch
            path = f"/trade-api/v2/markets/{ticker}"
            market = kalshi_get(private_key, path)

            # Log a small, stable subset so logs don't explode
            # (keys may differ, so we do safe extraction)
            log.info("Market fetched OK")
            log.info(f"market_ticker={market.get('market', {}).get('ticker') or market.get('ticker')}")
            log.info(f"status={market.get('market', {}).get('status') or market.get('status')}")
            log.info(f"yes_ask={market.get('market', {}).get('yes_ask') or market.get('yes_ask')} "
                     f"yes_bid={market.get('market', {}).get('yes_bid') or market.get('yes_bid')}")

            # Trading logic intentionally not implemented here.
            # You asked for: "buy/sell when 95% on one side" etc.
            # Once the API is stable, we can add order placement safely.

        except Exception as e:
            log.error(f"LOOP ERROR: {repr(e)}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
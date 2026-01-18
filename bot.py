#!/usr/bin/env python3
import os
import sys
import time
import math
import json
import requests
from datetime import datetime, timezone

# -----------------------------
# FORCE UNBUFFERED LOGS (RENDER)
# -----------------------------
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

def log(msg):
    print(msg, flush=True)

# -----------------------------
# CONFIG (SAFE DEFAULTS)
# -----------------------------
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").lower() == "true"

# -----------------------------
# AUTH HEADERS (NO PRIVATE KEY HEADER!)
# -----------------------------
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "").strip()

if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY_PEM:
    log("⚠️  WARNING: Kalshi credentials missing — running in READ-ONLY mode")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ⚠️ DO NOT PUT PRIVATE KEY IN HEADERS
if KALSHI_API_KEY_ID:
    HEADERS["KALSHI-ACCESS-KEY"] = KALSHI_API_KEY_ID

# -----------------------------
# TIME HELPERS
# -----------------------------
def current_15m_bucket():
    """
    Returns current UTC time floored to nearest 15-minute boundary
    """
    now = datetime.now(timezone.utc)
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)

def format_market_ticker(dt):
    """
    KXBTC15M-18JAN261900
    """
    return f"{SERIES_PREFIX}-{dt.strftime('%d%b%y%H%M').upper()}"

# -----------------------------
# API HELPERS
# -----------------------------
def get_market(ticker):
    url = f"{BASE_URL}/markets/{ticker}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()

def get_orderbook(ticker):
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()

def has_liquidity(orderbook):
    if not orderbook:
        return False
    for side in ["yes", "no"]:
        bids = orderbook.get(side, {}).get("bids", [])
        asks = orderbook.get(side, {}).get("asks", [])
        if bids or asks:
            return True
    return False

# -----------------------------
# MAIN LOOP
# -----------------------------
def main():
    log("=== BOT STARTED ===")
    log(f"ENABLE_TRADING={ENABLE_TRADING}")
    log(f"POLL_SECONDS={POLL_SECONDS}")
    log(f"SERIES_PREFIX={SERIES_PREFIX}")

    last_market = None

    while True:
        try:
            bucket = current_15m_bucket()
            ticker = format_market_ticker(bucket)

            if ticker != last_market:
                log(f"\nCHECKING MARKET: {ticker}")
                last_market = ticker

            market = get_market(ticker)
            if not market:
                log("Market not live yet")
                time.sleep(POLL_SECONDS)
                continue

            orderbook = get_orderbook(ticker)
            if not has_liquidity(orderbook):
                log("No liquidity yet")
                time.sleep(POLL_SECONDS)
                continue

            log("✅ MARKET HAS LIQUIDITY")

            if ENABLE_TRADING:
                log("⚠️ Trading enabled — but logic intentionally disabled for safety")
            else:
                log("Trading disabled (ENABLE_TRADING=False)")

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(30)

# -----------------------------
# ENTRYPOINT
# -----------------------------
if __name__ == "__main__":
    main()
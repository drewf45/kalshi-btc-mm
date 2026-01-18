import os
import time
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
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
# Timezones
# -----------------------------
ET = ZoneInfo("America/New_York")


# -----------------------------
# Config (Render env vars)
# -----------------------------
DEFAULT_PROD_BASE = "https://api.elections.kalshi.com"

# If you set this wrong in Render, it WILL break you. We'll force-correct it.
RAW_API_BASE = os.getenv("KALSHI_API_BASE", DEFAULT_PROD_BASE).strip()

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_PEM_BASE64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30").strip())
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").strip().lower() in ("1", "true", "yes")

# Optional override (if you ever want to force a known ticker)
MARKET_TICKER_OVERRIDE = os.getenv("MARKET_TICKER_OVERRIDE", "").strip()

# Strategy knobs (your preferences)
EXTREME_THRESHOLD = float(os.getenv("EXTREME_THRESHOLD", "0.95"))  # 95% one-sided
BET_PCT_OF_LIQUIDITY = float(os.getenv("BET_PCT_OF_LIQUIDITY", "0.01"))  # 1% per bet
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.20"))  # stop if -20% daily


SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})


# -----------------------------
# Force-correct the API base
# (This is the exact issue shown in your log.)
# -----------------------------
def normalize_api_base(raw: str) -> str:
    raw = (raw or "").strip().rstrip("/")
    # If you point to these, you get DNS failures or "moved" errors (your logs show both).
    bad_hosts = ("api.kalshi.com", "trading-api.kalshi.com", "api.elections.kalski.com")
    if any(bad in raw for bad in bad_hosts):
        log.warning(f"API_BASE was set to '{raw}' (known bad). Forcing to {DEFAULT_PROD_BASE}")
        return DEFAULT_PROD_BASE
    # If blank, also force.
    if not raw:
        log.warning(f"API_BASE was blank. Forcing to {DEFAULT_PROD_BASE}")
        return DEFAULT_PROD_BASE
    return raw


KALSHI_API_BASE = normalize_api_base(RAW_API_BASE)


# -----------------------------
# Kalshi signing helpers
# Headers:
#   KALSHI-ACCESS-KEY
#   KALSHI-ACCESS-TIMESTAMP (ms)
#   KALSHI-ACCESS-SIGNATURE
# Signature message:
#   timestamp_ms + METHOD + path_without_query
# -----------------------------
def load_private_key_from_env():
    if not KALSHI_PRIVATE_KEY_PEM_BASE64:
        raise RuntimeError(
            "Missing KALSHI_PRIVATE_KEY_PEM_BASE64. "
            "You must base64-encode your downloaded Kalshi private .key file and set it as an env var."
        )
    try:
        pem_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_PEM_BASE64)
        return serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as e:
        raise RuntimeError(f"Failed to load private key from KALSHI_PRIVATE_KEY_PEM_BASE64: {e}")


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
        raise RuntimeError("Missing KALSHI_API_KEY_ID env var.")
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
# Robust time parsing
# -----------------------------
def parse_iso_dt(s: str):
    # Handles "Z" and offsets
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None


# -----------------------------
# Find the NEXT 15m BTC market WITHOUT guessing the ticker string
# This is the fix for your “wrong date / -30 / seconds” issues.
# -----------------------------
def list_open_markets(private_key, series_ticker: str, limit: int = 200) -> list:
    # Kalshi list endpoint (v2)
    # /trade-api/v2/markets?limit=200&status=open&series_ticker=KXBTC15M
    path = f"/trade-api/v2/markets?limit={limit}&status=open&series_ticker={series_ticker}"
    data = kalshi_get(private_key, path)
    # Response shape may be { "markets": [...] } or { "data": [...] }
    markets = data.get("markets") or data.get("data") or []
    if not isinstance(markets, list):
        return []
    return markets


def pick_next_market(markets: list, now_et: datetime) -> dict | None:
    """
    Choose the soonest market that is relevant for the next 15m boundary.
    Different API responses sometimes expose time fields differently.
    We'll try common keys and pick the market with the smallest close/end time after now.
    """
    now_utc = now_et.astimezone(timezone.utc)

    candidates = []
    for m in markets:
        # common keys seen across Kalshi APIs
        # We try a bunch and accept the first parseable datetime.
        time_fields = [
            "close_time", "close_ts",
            "end_time", "end_ts",
            "settle_time", "settle_ts",
            "expiration_time", "expiration_ts",
        ]

        dt = None
        for k in time_fields:
            v = m.get(k)
            if isinstance(v, str):
                dt = parse_iso_dt(v)
                if dt:
                    break
        if not dt:
            # some APIs use numeric timestamps (seconds or ms)
            for k in time_fields:
                v = m.get(k)
                if isinstance(v, (int, float)):
                    # guess ms vs seconds
                    if v > 10_000_000_000:  # ms
                        dt = datetime.fromtimestamp(v / 1000, tz=timezone.utc)
                    else:  # seconds
                        dt = datetime.fromtimestamp(v, tz=timezone.utc)
                    break

        if not dt:
            continue

        # Only consider markets that haven't closed yet
        if dt <= now_utc:
            continue

        candidates.append((dt, m))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def extract_ticker(market: dict) -> str | None:
    # Some shapes: {"ticker": "..."} or {"market": {"ticker": "..."}}
    if not market:
        return None
    if isinstance(market.get("ticker"), str):
        return market["ticker"]
    if isinstance(market.get("market"), dict) and isinstance(market["market"].get("ticker"), str):
        return market["market"]["ticker"]
    return None


# -----------------------------
# Main loop
# -----------------------------
def main():
    private_key = load_private_key_from_env()

    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"API_BASE={KALSHI_API_BASE}")
    if MARKET_TICKER_OVERRIDE:
        log.warning(f"MARKET_TICKER_OVERRIDE is set: {MARKET_TICKER_OVERRIDE} (this will force a single market)")

    while True:
        start = time.time()
        try:
            now_et = datetime.now(ET)

            # 1) Decide which market ticker to use
            if MARKET_TICKER_OVERRIDE:
                ticker = MARKET_TICKER_OVERRIDE
                log.info(f"Heartbeat ET now={now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} | using OVERRIDE ticker={ticker}")
                market = kalshi_get(private_key, f"/trade-api/v2/markets/{ticker}")
                log.info("Market fetched OK (override).")
            else:
                log.info(f"Heartbeat ET now={now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} | resolving NEXT open market from series {SERIES_PREFIX}")
                markets = list_open_markets(private_key, SERIES_PREFIX, limit=200)
                log.info(f"Open markets returned: {len(markets)}")

                nxt = pick_next_market(markets, now_et)
                if not nxt:
                    log.warning("No open markets with a future close/end time were found. Will retry.")
                    time.sleep(POLL_SECONDS)
                    continue

                ticker = extract_ticker(nxt)
                if not ticker:
                    log.warning("Next market found but ticker missing in payload. Will retry.")
                    time.sleep(POLL_SECONDS)
                    continue

                log.info(f"Resolved next market ticker={ticker}")
                market = kalshi_get(private_key, f"/trade-api/v2/markets/{ticker}")
                log.info("Market fetched OK (resolved).")

            # 2) Log a stable subset
            mm = market.get("market") if isinstance(market.get("market"), dict) else market
            yes_ask = mm.get("yes_ask")
            yes_bid = mm.get("yes_bid")
            no_ask = mm.get("no_ask")
            no_bid = mm.get("no_bid")
            status = mm.get("status")

            log.info(f"market_ticker={mm.get('ticker')} status={status} yes_bid={yes_bid} yes_ask={yes_ask} no_bid={no_bid} no_ask={no_ask}")

            # 3) Trading placeholder (we’ll add once your connectivity + auth are 100%)
            if ENABLE_TRADING:
                log.info("ENABLE_TRADING=True but order placement is not yet enabled in this file.")
                # Next step (once stable): fetch orderbook and place 1-contract micro bets
                # when one side dominance >= EXTREME_THRESHOLD.

        except Exception as e:
            log.error(f"LOOP ERROR: {repr(e)}")

        # Hard guarantee: log cycle duration so you never feel like it “went silent”
        elapsed = time.time() - start
        log.info(f"Loop complete in {elapsed:.2f}s; sleeping {POLL_SECONDS}s")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
import os
import time
from datetime import datetime, timezone
import os, time
print("=== BOT STARTED ===", flush=True)
print("POLL_SECONDS =", os.getenv("POLL_SECONDS"), "SERIES_TICKER =", os.getenv("SERIES_TICKER"), flush=True)

# =====================
# CONFIG (from Render env vars)
# =====================
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))
MARKET_SYMBOL = os.getenv("MARKET_SYMBOL", "BTC-15MIN")

# =====================
# HELPERS
# =====================
def utc_day():
    return datetime.now(timezone.utc).date()

# =====================
# MAIN BOT LOOP
# =====================
def main():
    print("=== Kalshi BTC Market Maker Bot ===")
    print(f"Market: {MARKET_SYMBOL}")
    print(f"Max Daily Loss: ${MAX_DAILY_LOSS}")
    print("Bot started successfully")

    current_day = utc_day()
    daily_pnl = 0.0

    while True:
        try:
            # Reset daily PnL at UTC midnight
            if utc_day() != current_day:
                print("🔄 New UTC day — resetting PnL")
                current_day = utc_day()
                daily_pnl = 0.0

            # Hard stop if loss limit hit
            if daily_pnl <= -MAX_DAILY_LOSS:
                print("🛑 DAILY LOSS LIMIT HIT — sleeping until reset")
                time.sleep(60)
                continue

            # ===== PLACEHOLDER FOR TRADING LOGIC =====
            # Next step we will:
            # - read BTC 15m orderbook
            # - place maker bid + ask
            # - track fills
            # - update daily_pnl
            # ========================================

            print(f"[{datetime.utcnow()}] Cycle running | PnL=${daily_pnl:.2f}")

            # Run every 15 minutes
            time.sleep(15 * 60)

        except Exception as e:
            print("❌ ERROR:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
    import os
import time
import json
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_KEY = os.getenv("KALSHI_API_KEY", "").strip()
API_PRIVATE_KEY_PEM = os.getenv("KALSHI_API_SECRET", "").strip()  # RSA private key PEM text
MARKET_SYMBOL = os.getenv("MARKET_SYMBOL", "BTC-15MIN").strip()
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))

# If you keep MARKET_SYMBOL=BTC-15MIN, map it to the real series ticker.
SERIES_MAP = {
    "BTC-15MIN": "kxbtc15m",
    "KXBTC15M": "kxbtc15m",
    "kxbtc15m": "kxbtc15m",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})


def utc_ms() -> str:
    return str(int(time.time() * 1000))


def load_private_key_pem(pem_text: str) -> str:
    # Render env vars can store multiline; if you pasted with \n, convert back.
    return pem_text.replace("\\n", "\n")


def kalshi_sign_request(method: str, path: str, body: str, timestamp_ms: str) -> str:
    """
    Kalshi uses RSA-PSS signatures for authenticated requests (KALSHI-ACCESS-SIGNATURE).  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
    NOTE: We do the signing by calling OpenSSL via python's cryptography? (kept minimal here)
    If your existing bot already signs correctly, keep your signer and plug in the new endpoints below.
    """
    # This placeholder is here so you don't accidentally deploy a "fake signer".
    # If your bot is currently authenticating and running, you ALREADY have signing working.
    raise RuntimeError(
        "Signing not implemented in this snippet because your existing bot already has it. "
        "Paste the orderbook + market resolution functions into your existing signed client."
    )


def request_public(method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    r = SESSION.request(method, url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_open_market_ticker_for_series(series_ticker: str) -> str:
    """
    Use GET /markets with series_ticker and status=open.  [oai_citation:3‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/market/get-markets)
    """
    data = request_public(
        "GET",
        "/markets",
        params={
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 100,
        },
    )
    markets = data.get("markets", [])
    if not markets:
        raise RuntimeError(f"No open markets returned for series_ticker={series_ticker}")

    # Pick the one closing soonest (generally the "current" 15-min window)
    markets_sorted = sorted(markets, key=lambda m: m.get("close_time") or "")
    return markets_sorted[0]["ticker"]


def get_orderbook_public_if_allowed(market_ticker: str, depth: int = 1) -> Dict[str, Any]:
    """
    Endpoint: GET /markets/{ticker}/orderbook (supports depth).  [oai_citation:4‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
    If your API requires auth for this endpoint, call your signed request function instead.
    """
    return request_public("GET", f"/markets/{market_ticker}/orderbook", params={"depth": depth})


def best_levels_from_orderbook(ob: Dict[str, Any]) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """
    Returns:
      best_yes_bid = (price_dollars, size_fp)
      best_no_bid  = (price_dollars, size_fp)
    Prefer fixed-point counts: orderbook_fp.  [oai_citation:5‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
    """
    fp = ob.get("orderbook_fp", {})
    yes = fp.get("yes_dollars", [])
    no = fp.get("no_dollars", [])
    best_yes = yes[0] if yes else None
    best_no = no[0] if no else None
    if best_yes:
        best_yes = (best_yes[0], best_yes[1])
    if best_no:
        best_no = (best_no[0], best_no[1])
    return best_yes, best_no


def main_loop():
    series_ticker = SERIES_MAP.get(MARKET_SYMBOL, MARKET_SYMBOL)  # allow user to pass kxbtc15m directly
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting. MARKET_SYMBOL={MARKET_SYMBOL} -> series={series_ticker}")
    print(f"Max daily loss configured: ${MAX_DAILY_LOSS:.2f}")

    while True:
        try:
            mkt = get_open_market_ticker_for_series(series_ticker)
            ob = get_orderbook_public_if_allowed(mkt, depth=1)  # depth=1 gives top-of-book
            best_yes, best_no = best_levels_from_orderbook(ob)

            print(f"[{datetime.now(timezone.utc).isoformat()}] market={mkt}")
            print(f"  best_yes_bid: {best_yes}")
            print(f"  best_no_bid : {best_no}")

        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat()}] ERROR: {e}")

        # 15 minutes
        time.sleep(15 * 60)


if __name__ == "__main__":
    main_loop()
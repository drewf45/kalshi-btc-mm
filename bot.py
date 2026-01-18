import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

# =====================
# CONFIG (Render env vars)
# =====================
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").strip()

# REQUIRED
SERIES_TICKER = os.getenv("SERIES_TICKER", "").strip()  # e.g. kxbtc15m

# OPTIONAL
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))      # 60 for testing, 900 for 15m
ORDERBOOK_DEPTH = int(os.getenv("ORDERBOOK_DEPTH", "1"))  # 1 = top-of-book
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20")) # not used yet (read-only mode)

# How often to refresh the open market ticker (avoids 429 rate limits)
MARKET_REFRESH_SECONDS = int(os.getenv("MARKET_REFRESH_SECONDS", str(12 * 60)))  # 12 minutes

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})


# =====================
# HELPERS
# =====================
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def request_public(method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    r = SESSION.request(method, url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_open_market_ticker_for_series(series_ticker: str) -> str:
    """
    Find currently open markets for the series and pick the one closing soonest
    (usually the current 15-minute window).
    """
    data = request_public(
        "GET",
        "/markets",
        params={
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 200,
        },
    )

    markets = data.get("markets", [])
    if not markets:
        raise RuntimeError(f"No open markets returned for series_ticker={series_ticker}")

    markets_sorted = sorted(markets, key=lambda m: m.get("close_time") or "")
    return markets_sorted[0]["ticker"]


def get_orderbook(market_ticker: str, depth: int) -> Dict[str, Any]:
    return request_public("GET", f"/markets/{market_ticker}/orderbook", params={"depth": depth})


def best_levels_from_orderbook(ob: Dict[str, Any]) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """
    Prefer orderbook_fp format:
      orderbook_fp.yes_dollars = [[price_str, size_str], ...]
      orderbook_fp.no_dollars  = [[price_str, size_str], ...]
    """
    fp = ob.get("orderbook_fp", {}) or {}
    yes = fp.get("yes_dollars", []) or []
    no = fp.get("no_dollars", []) or []

    best_yes = tuple(yes[0]) if yes else None
    best_no = tuple(no[0]) if no else None
    return best_yes, best_no


# =====================
# MAIN LOOP (READ-ONLY)
# =====================
def main() -> None:
    print("=== BOT STARTED ===", flush=True)
    print(f"[{now_utc()}] BASE_URL={BASE_URL}", flush=True)
    print(f"[{now_utc()}] SERIES_TICKER={SERIES_TICKER or '(missing)'}", flush=True)
    print(f"[{now_utc()}] POLL_SECONDS={POLL_SECONDS} ORDERBOOK_DEPTH={ORDERBOOK_DEPTH}", flush=True)
    print(f"[{now_utc()}] MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}", flush=True)
    print(f"[{now_utc()}] MAX_DAILY_LOSS=${MAX_DAILY_LOSS:.2f} (not used yet - read-only mode)", flush=True)

    if not SERIES_TICKER:
        raise RuntimeError("Missing env var SERIES_TICKER. Set SERIES_TICKER=kxbtc15m in Render.")

    current_market: Optional[str] = None
    market_last_refresh: float = 0.0

    while True:
        try:
            now = time.time()

            # Refresh market ticker only every ~12 minutes (avoids 429 rate limits)
            if current_market is None or (now - market_last_refresh) > MARKET_REFRESH_SECONDS:
                current_market = get_open_market_ticker_for_series(SERIES_TICKER)
                market_last_refresh = now
                print(f"\n[{now_utc()}] refreshed market -> {current_market}", flush=True)

            # Pull orderbook (top-of-book by default)
            ob = get_orderbook(current_market, depth=ORDERBOOK_DEPTH)
            best_yes, best_no = best_levels_from_orderbook(ob)

            print(f"[{now_utc()}] market={current_market}", flush=True)
            print(f"  best_yes_bid: {best_yes}", flush=True)
            print(f"  best_no_bid : {best_no}", flush=True)

        except Exception as e:
            print(f"\n[{now_utc()}] ERROR: {e}", flush=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
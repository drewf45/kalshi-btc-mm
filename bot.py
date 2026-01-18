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

# OPTIONAL (nice to have)
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))     # 60 for testing, 900 for 15m
ORDERBOOK_DEPTH = int(os.getenv("ORDERBOOK_DEPTH", "1")) # 1 = top-of-book, 10/25 for deeper view

# Not used yet (kept for later trading phase)
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))

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
    Finds currently open markets for the series and picks the one closing soonest
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

    # close_time is ISO; sorting lexicographically works for ISO timestamps
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
# MAIN LOOP
# =====================
def main() -> None:
    print("=== BOT STARTED ===", flush=True)
    print(f"[{now_utc()}] BASE_URL={BASE_URL}", flush=True)
    print(f"[{now_utc()}] SERIES_TICKER={SERIES_TICKER or '(missing)'}", flush=True)
    print(f"[{now_utc()}] POLL_SECONDS={POLL_SECONDS} ORDERBOOK_DEPTH={ORDERBOOK_DEPTH}", flush=True)
    print(f"[{now_utc()}] MAX_DAILY_LOSS=${MAX_DAILY_LOSS:.2f} (not used yet - read-only mode)", flush=True)

    if not SERIES_TICKER:
        raise RuntimeError("Missing env var SERIES_TICKER. Set SERIES_TICKER=kxbtc15m in Render.")

    while True:
        try:
            mkt = get_open_market_ticker_for_series(SERIES_TICKER)
            ob = get_orderbook(mkt, depth=ORDERBOOK_DEPTH)
            best_yes, best_no = best_levels_from_orderbook(ob)

            print(f"\n[{now_utc()}] market={mkt}", flush=True)
            print(f"  best_yes_bid: {best_yes}", flush=True)
            print(f"  best_no_bid : {best_no}", flush=True)

        except Exception as e:
            print(f"\n[{now_utc()}] ERROR: {e}", flush=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
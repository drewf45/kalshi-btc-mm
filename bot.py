import os
import time
import json
import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Any, Dict, Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# =========================
# ENV CONFIG (Render)
# =========================
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com").rstrip("/")
API_KEY = os.getenv("KALSHI_API_KEY", "").strip()

# Paste RSA private key PEM into Render env var (multiline). If you pasted with \n, we convert back.
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY", "").strip().replace("\\n", "\n")

# IMPORTANT: Use the actual market ticker from the app page you want to trade
# Example from your link: KXBTC15M-26JAN171930
MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip()

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
SIDE = os.getenv("SIDE", "yes").strip().lower()  # "yes" or "no"
BET_DOLLARS = float(os.getenv("BET_DOLLARS", "1"))  # target dollars per wager
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))

# Safety / throttling
REQUEST_TIMEOUT = 15
MAX_BACKOFF_SECONDS = 300


# =========================
# BASIC VALIDATION
# =========================
if SIDE not in ("yes", "no"):
    raise ValueError("SIDE must be 'yes' or 'no'")

if not MARKET_TICKER:
    raise ValueError(
        "MARKET_TICKER is required. Set it to the exact ticker from the Kalshi market page, "
        "e.g. KXBTC15M-26JAN171930"
    )

if not API_KEY:
    raise ValueError("KALSHI_API_KEY is required")

if ENABLE_TRADING and not PRIVATE_KEY_PEM:
    raise ValueError("KALSHI_PRIVATE_KEY is required when ENABLE_TRADING=true (RSA private key PEM)")


# =========================
# SIGNING (Kalshi RSA-PSS)
# Based on Kalshi docs quick start + auth guide:
# signature = RSA-PSS-SHA256 over: "{timestamp}{method}{path}"
# =========================
def _load_private_key(pem_text: str):
    return serialization.load_pem_private_key(
        pem_text.encode("utf-8"),
        password=None,
    )


def _timestamp_ms() -> str:
    return str(int(time.time() * 1000))


def _sign(timestamp_ms: str, method: str, path: str, private_key) -> str:
    message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


# =========================
# HTTP HELPERS
# =========================
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-btc-bot/1.0"})


def _make_headers(method: str, path: str, authed: bool) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not authed:
        return headers

    private_key = _load_private_key(PRIVATE_KEY_PEM)
    ts = _timestamp_ms()
    signature = _sign(ts, method, path, private_key)
    headers.update(
        {
            "KALSHI-ACCESS-KEY": API_KEY,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
    )
    return headers


def request_json(method: str, path: str, *, authed: bool, params: Optional[Dict[str, Any]] = None, body: Any = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    headers = _make_headers(method, path, authed=authed)

    r = SESSION.request(
        method=method.upper(),
        url=url,
        headers=headers,
        params=params,
        data=None if body is None else json.dumps(body),
        timeout=REQUEST_TIMEOUT,
    )

    # Raise with readable info
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)

    return r.json()


# =========================
# MARKET DATA
# =========================
def get_orderbook(market_ticker: str, depth: int = 1) -> Dict[str, Any]:
    # Public endpoint (no auth required for market data)
    return request_json(
        "GET",
        f"/trade-api/v2/markets/{market_ticker}/orderbook",
        authed=False,
        params={"depth": depth},
    )


def top_of_book_prices(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (best_yes_bid_cents, best_no_bid_cents).
    Kalshi orderbook returns bids for YES and NO.
    """
    # Prefer orderbook "yes"/"no" arrays if present
    yes_bids = ob.get("yes", {}).get("bids", [])
    no_bids = ob.get("no", {}).get("bids", [])

    best_yes = int(yes_bids[0][0]) if yes_bids else None
    best_no = int(no_bids[0][0]) if no_bids else None
    return best_yes, best_no


# =========================
# ORDER PLACEMENT
# =========================
def compute_count_for_budget(price_cents: int, budget_dollars: float) -> int:
    """
    Approximate spend = count * price_cents/100.
    We choose the largest count that stays <= budget_dollars, minimum 1.
    """
    if price_cents <= 0:
        return 1
    max_count = int((budget_dollars * 100) // price_cents)
    return max(1, max_count)


def place_test_order(market_ticker: str, side: str, price_cents: int, count: int) -> Dict[str, Any]:
    """
    POST /trade-api/v2/portfolio/orders
    Limit buy order on YES or NO.
    """
    path = "/trade-api/v2/portfolio/orders"
    payload = {
        "ticker": market_ticker,
        "action": "buy",
        "side": side,        # "yes" or "no"
        "count": count,      # contracts
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
    }

    # Kalshi expects yes_price or no_price depending on side
    if side == "yes":
        payload["yes_price"] = int(price_cents)
    else:
        payload["no_price"] = int(price_cents)

    return request_json("POST", path, authed=True, body=payload)


# =========================
# DAILY LOSS TRACKING (simple placeholder)
# NOTE: This does NOT compute real PnL yet; it just enforces a manual stop if you wire pnl later.
# =========================
def utc_day() -> date:
    return datetime.now(timezone.utc).date()


# =========================
# MAIN LOOP
# =========================
def main():
    print("=== BOT STARTED ===", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BASE_URL={BASE_URL}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MARKET_TICKER={MARKET_TICKER}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] POLL_SECONDS={POLL_SECONDS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MAX_DAILY_LOSS=${MAX_DAILY_LOSS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] ENABLE_TRADING={ENABLE_TRADING} SIDE={SIDE} BET_DOLLARS=${BET_DOLLARS}", flush=True)

    current_day = utc_day()
    daily_pnl = 0.0  # placeholder until you calculate real pnl from fills

    backoff = 0

    while True:
        try:
            # Reset daily PnL at UTC midnight
            if utc_day() != current_day:
                print("🔄 New UTC day — resetting daily_pnl", flush=True)
                current_day = utc_day()
                daily_pnl = 0.0

            # Hard stop if loss limit hit
            if daily_pnl <= -MAX_DAILY_LOSS:
                print("🛑 DAILY LOSS LIMIT HIT — sleeping until reset", flush=True)
                time.sleep(60)
                continue

            ob = get_orderbook(MARKET_TICKER, depth=1)
            best_yes, best_no = top_of_book_prices(ob)

            # For binary markets:
            # best ask for YES is approximately (100 - best NO bid)
            # best ask for NO  is approximately (100 - best YES bid)
            yes_ask = (100 - best_no) if best_no is not None else None
            no_ask = (100 - best_yes) if best_yes is not None else None

            print(f"[{datetime.now(timezone.utc).isoformat()}] Top of book:", flush=True)
            print(f"  best_yes_bid={best_yes}c  best_no_bid={best_no}c  yes_ask≈{yes_ask}c  no_ask≈{no_ask}c", flush=True)

            # Choose a "fill-likely" test price:
            # If buying YES, we buy at yes_ask to likely fill.
            # If buying NO,  we buy at no_ask to likely fill.
            if SIDE == "yes":
                if yes_ask is None:
                    print("⚠️ No yes_ask available yet; skipping this cycle.", flush=True)
                else:
                    price_cents = int(yes_ask)
                    count = compute_count_for_budget(price_cents, BET_DOLLARS)
                    print(f"Planned TEST order: BUY YES @ {price_cents}c x {count} (≈${count*price_cents/100:.2f})", flush=True)

                    if ENABLE_TRADING:
                        resp = place_test_order(MARKET_TICKER, "yes", price_cents, count)
                        order = resp.get("order", resp)
                        print(f"✅ Order sent. order_id={order.get('order_id')} status={order.get('status')}", flush=True)
                    else:
                        print("🧪 Trading disabled (ENABLE_TRADING=false) — not sending order.", flush=True)

            else:  # SIDE == "no"
                if no_ask is None:
                    print("⚠️ No no_ask available yet; skipping this cycle.", flush=True)
                else:
                    price_cents = int(no_ask)
                    count = compute_count_for_budget(price_cents, BET_DOLLARS)
                    print(f"Planned TEST order: BUY NO @ {price_cents}c x {count} (≈${count*price_cents/100:.2f})", flush=True)

                    if ENABLE_TRADING:
                        resp = place_test_order(MARKET_TICKER, "no", price_cents, count)
                        order = resp.get("order", resp)
                        print(f"✅ Order sent. order_id={order.get('order_id')} status={order.get('status')}", flush=True)
                    else:
                        print("🧪 Trading disabled (ENABLE_TRADING=false) — not sending order.", flush=True)

            backoff = 0
            time.sleep(POLL_SECONDS)

        except requests.HTTPError as e:
            # Rate limit + general HTTP errors
            status = getattr(e.response, "status_code", None)
            msg = str(e)

            if status == 429:
                backoff = min(MAX_BACKOFF_SECONDS, max(5, backoff * 2 if backoff else 10))
                print(f"⏳ 429 rate limited — backing off {backoff}s. {msg}", flush=True)
                time.sleep(backoff)
            else:
                print(f"❌ HTTP ERROR: {msg}", flush=True)
                time.sleep(10)

        except Exception as e:
            print(f"❌ ERROR: {e}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
import os
import time
import json
import uuid
import base64
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import requests

# --- Requires cryptography ---
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ==========================================================
# ✅ FIX: Robust env var loading + loud debug prints
# - Handles: wrong var names, secret-file confusion, newline issues
# - Supports PEM via:
#     1) KALSHI_PRIVATE_KEY_PEM_B64 (recommended on mobile)
#     2) KALSHI_PRIVATE_KEY_PEM (multiline OR with \n)
# - Supports key id via:
#     1) KALSHI_API_KEY_ID
#     2) KALSHI_API_KEY_ID (same)
# ==========================================================

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _must_env(*names: str) -> str:
    for n in names:
        v = _env(n, "")
        if v:
            return v
    raise RuntimeError(f"Missing required env var. Tried: {', '.join(names)}")

def load_private_key_from_env():
    """
    Preferred: KALSHI_PRIVATE_KEY_PEM_B64 = base64( PEM bytes )
    Fallback : KALSHI_PRIVATE_KEY_PEM     = PEM text (multiline or with \\n)
    """
    b64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
    if b64:
        try:
            pem_bytes = base64.b64decode(b64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64 (base64/PEM parse failed): {e}")

    pem_text = _env("KALSHI_PRIVATE_KEY_PEM", "")
    if pem_text:
        try:
            pem_bytes = pem_text.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM (PEM parse failed): {e}")

    raise RuntimeError("Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM")

# =========================
# CONFIG (Render env vars)
# =========================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# ✅ FIX: accept either spelling; your logs showed mismatch issues earlier
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Strategy / risk
POLL_SECONDS = int(_env("POLL_SECONDS", "60"))
MARKET_REFRESH_SECONDS = int(_env("MARKET_REFRESH_SECONDS", "60"))
MAX_DAILY_LOSS = float(_env("MAX_DAILY_LOSS", "20"))

# Wagering
BET_DOLLARS = float(_env("BET_DOLLARS", "1"))
MIN_CONTRACTS = int(_env("MIN_CONTRACTS", "1"))

# If provided, bot will ONLY trade this exact market ticker (example: KXBTC15M-26JAN172000).
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")

# Otherwise bot will generate tickers using this prefix (Kalshi BTC 15m uses KXBTC15M-YYMMMDDHHMM).
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")

USE_NEXT_QUARTER_HOUR_END = _env("USE_NEXT_QUARTER_HOUR_END", "true").lower() in ("1", "true", "yes", "y")
DRY_RUN = _env("DRY_RUN", "false").lower() in ("1", "true", "yes", "y")
STOP_AFTER_WAGERS = int(_env("STOP_AFTER_WAGERS", "0"))

# ✅ FIX: ET offset is configurable; you can set -4 during DST
ET_UTC_OFFSET_HOURS = int(_env("ET_UTC_OFFSET_HOURS", "-5"))

# =========================
# ET helpers (no pytz)
# =========================
def now_et_naive() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=ET_UTC_OFFSET_HOURS)).replace(tzinfo=None)

def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    yy = dt_et.strftime("%y")
    mon = dt_et.strftime("%b").upper()
    dd = dt_et.strftime("%d")
    hhmm = dt_et.strftime("%H%M")
    return f"{SERIES_PREFIX}-{yy}{mon}{dd}{hhmm}"

def next_quarter_hour_end(dt_et: datetime) -> datetime:
    minute = dt_et.minute
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        return dt_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt_et.replace(minute=next_q, second=0, microsecond=0)

def compute_target_market_ticker() -> str:
    if MARKET_TICKER_OVERRIDE:
        return MARKET_TICKER_OVERRIDE
    dt_et = now_et_naive()
    if USE_NEXT_QUARTER_HOUR_END:
        dt_et = next_quarter_hour_end(dt_et)
    return format_kalshi_btc15m_ticker(dt_et)

# =========================
# Kalshi signing (RSA-PSS)
# =========================
def make_signature(private_key, timestamp_ms: str, method: str, path_with_query: str, body: str) -> str:
    payload = (timestamp_ms + method.upper() + path_with_query + body).encode("utf-8")
    sig = private_key.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

@dataclass
class KalshiClient:
    base_url: str
    key_id: str

    def __post_init__(self):
        if not self.key_id:
            raise RuntimeError("Missing KALSHI_API_KEY_ID (set this exact env var in Render → Environment Variables).")

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})
        self.private_key = load_private_key_from_env()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"

        body = ""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

        path_with_query = path
        if params:
            from urllib.parse import urlencode
            qs = urlencode(params)
            path_with_query = f"{path}?{qs}"

        ts = str(int(time.time() * 1000))
        sig = make_signature(self.private_key, ts, method, path_with_query, body)

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

        resp = self.session.request(
            method=method,
            url=url,
            params=params,
            data=body if body else None,
            headers=headers,
            timeout=20,
        )

        if resp.status_code == 429:
            raise RuntimeError("429_RATE_LIMIT")

        resp.raise_for_status()
        return resp.json()

    def get_orderbook(self, market_ticker: str, depth: int = 1) -> Dict[str, Any]:
        return self._request("GET", f"/markets/{market_ticker}/orderbook", params={"depth": depth})

    def create_order_fok_buy(self, market_ticker: str, side: str, price_cents: int, count: int) -> Dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,            # "yes" or "no"
            "action": "buy",
            "type": "limit",
            "count": int(count),
            "time_in_force": "fill_or_kill",
            "client_order_id": client_order_id,
        }
        if side == "yes":
            payload["yes_price"] = int(price_cents)
        else:
            payload["no_price"] = int(price_cents)
        return self._request("POST", "/portfolio/orders", json_body=payload)

# =========================
# Orderbook parsing
# =========================
def extract_top_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    We want BEST ASK for YES and NO to BUY.
    Supports multiple payload shapes.
    """
    fp = ob.get("orderbook_fp") or {}

    # Newer shapes sometimes provide *_asks arrays in fp
    yes_asks = fp.get("yes_dollars_asks")
    no_asks  = fp.get("no_dollars_asks")

    if isinstance(yes_asks, list) and isinstance(no_asks, list) and yes_asks and no_asks:
        return (
            {"price_dollars": yes_asks[0][0], "size": yes_asks[0][1]},
            {"price_dollars": no_asks[0][0],  "size": no_asks[0][1]},
        )

    # Fallback: non-fp
    yes = ob.get("yes", {})
    no  = ob.get("no", {})
    yes_asks_nf = yes.get("asks", [])
    no_asks_nf  = no.get("asks", [])
    best_yes = yes_asks_nf[0] if yes_asks_nf else None
    best_no  = no_asks_nf[0] if no_asks_nf else None
    return best_yes, best_no

def dollars_str_to_cents(price_dollars: str) -> int:
    cents = int(round(float(price_dollars) * 100))
    return max(1, min(99, cents))

def choose_majority_side(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    if not best_yes_ask or not best_no_ask:
        return None

    if "price_dollars" in best_yes_ask:
        yes_cents = dollars_str_to_cents(best_yes_ask["price_dollars"])
    else:
        yes_cents = int(best_yes_ask.get("price", 0))

    if "price_dollars" in best_no_ask:
        no_cents = dollars_str_to_cents(best_no_ask["price_dollars"])
    else:
        no_cents = int(best_no_ask.get("price", 0))

    if yes_cents <= 0 or no_cents <= 0:
        return None

    # "Majority" = higher price
    return ("yes", yes_cents) if yes_cents >= no_cents else ("no", no_cents)

def calc_contract_count(price_cents: int) -> int:
    budget_cents = int(round(BET_DOLLARS * 100))
    if price_cents <= 0:
        return 0
    count = budget_cents // price_cents
    if count < MIN_CONTRACTS:
        count = MIN_CONTRACTS
    return int(count)

# =========================
# MAIN LOOP
# =========================
def utc_day() -> datetime.date:
    return datetime.now(timezone.utc).date()

def main():
    print("=== BOT STARTED ===", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BASE_URL={BASE_URL}", flush=True)

    # ✅ Debug which env vars are actually present (no secret leakage)
    print("ENV_HAS_KALSHI_API_KEY_ID =", bool(_env("KALSHI_API_KEY_ID") or _env("KALSHI_API_KEYID")), flush=True)
    print("ENV_HAS_PEM_TEXT          =", bool(_env('KALSHI_PRIVATE_KEY_PEM')), flush=True)
    print("ENV_HAS_PEM_B64           =", bool(_env('KALSHI_PRIVATE_KEY_PEM_B64')), flush=True)

    print(f"[{datetime.now(timezone.utc).isoformat()}] POLL_SECONDS={POLL_SECONDS} MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MAX_DAILY_LOSS=${MAX_DAILY_LOSS} BET_DOLLARS=${BET_DOLLARS} DRY_RUN={DRY_RUN}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MARKET_TICKER_OVERRIDE={MARKET_TICKER_OVERRIDE or '(auto)'} SERIES_PREFIX={SERIES_PREFIX}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] ET_UTC_OFFSET_HOURS={ET_UTC_OFFSET_HOURS}", flush=True)

    client = KalshiClient(base_url=BASE_URL, key_id=KALSHI_API_KEY_ID)

    current_day = utc_day()
    daily_pnl = 0.0
    last_market_refresh = 0.0
    market_ticker = compute_target_market_ticker()
    last_wagered_market: Optional[str] = None
    wagers_done = 0
    backoff_seconds = 1

    while True:
        try:
            if utc_day() != current_day:
                print("🔄 New UTC day — resetting daily guard", flush=True)
                current_day = utc_day()
                daily_pnl = 0.0
                last_wagered_market = None

            if daily_pnl <= -MAX_DAILY_LOSS:
                print("🛑 DAILY LOSS LIMIT HIT — sleeping until reset", flush=True)
                time.sleep(60)
                continue

            now = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now

            if last_wagered_market == market_ticker:
                print(f"[{datetime.now(timezone.utc).isoformat()}] Already wagered market: {market_ticker}", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            ob = client.get_orderbook(market_ticker, depth=1)
            best_yes_ask, best_no_ask = extract_top_asks(ob)

            choice = choose_majority_side(best_yes_ask, best_no_ask)
            if not choice:
                print(f"[{datetime.now(timezone.utc).isoformat()}] No usable orderbook yet for {market_ticker}", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            side, price_cents = choice
            count = calc_contract_count(price_cents)
            est_cost = (count * price_cents) / 100.0

            print(
                f"[{datetime.now(timezone.utc).isoformat()}] market={market_ticker} "
                f"yes_ask={best_yes_ask} no_ask={best_no_ask} -> "
                f"BET {side.upper()} price={price_cents}c x{count} (est_cost=${est_cost:.2f})",
                flush=True,
            )

            if DRY_RUN:
                print("🧪 DRY_RUN=true (not placing order)", flush=True)
                last_wagered_market = market_ticker
                wagers_done += 1
            else:
                resp = client.create_order_fok_buy(market_ticker, side=side, price_cents=price_cents, count=count)
                order = resp.get("order", {}) if isinstance(resp, dict) else {}
                print(f"✅ Order response: {order}", flush=True)

                last_wagered_market = market_ticker
                wagers_done += 1

            if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                print(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.", flush=True)
                return

            backoff_seconds = 1
            time.sleep(POLL_SECONDS)

        except Exception as e:
            if str(e) == "429_RATE_LIMIT":
                print(f"[{datetime.now(timezone.utc).isoformat()}] ⚠️ 429 rate limit. Backing off {backoff_seconds}s", flush=True)
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 120)
                continue

            print(f"[{datetime.now(timezone.utc).isoformat()}] ❌ ERROR: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()

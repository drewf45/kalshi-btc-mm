import os
import time
import json
import uuid
import base64
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, Deque
from collections import deque

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# =========================
# Helpers
# =========================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "y")

def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(msg: str):
    print(f"[{iso_utc()}] {msg}", flush=True)


# =========================
# CONFIG (Render env vars)
# =========================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# Required secrets
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Private key: recommended to store as base64 in Render on mobile
#   KALSHI_PRIVATE_KEY_PEM_B64 = base64( PEM bytes )
# Fallback:
#   KALSHI_PRIVATE_KEY_PEM = full PEM text (multiline OR with \n)
KALSHI_PRIVATE_KEY_PEM_B64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
KALSHI_PRIVATE_KEY_PEM = _env("KALSHI_PRIVATE_KEY_PEM", "")

# Looping / risk
POLL_SECONDS = int(_env("POLL_SECONDS", "60"))
MAX_DAILY_LOSS = float(_env("MAX_DAILY_LOSS", "20"))  # guard only (not true PnL)
DRY_RUN = _bool("DRY_RUN", "false")

# Wager sizing
BET_DOLLARS = float(_env("BET_DOLLARS", "1"))
MIN_CONTRACTS = int(_env("MIN_CONTRACTS", "1"))

# Market selection
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")  # if set, trades ONLY this market
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")
MARKET_REFRESH_SECONDS = int(_env("MARKET_REFRESH_SECONDS", "60"))
USE_NEXT_QUARTER_HOUR_END = _bool("USE_NEXT_QUARTER_HOUR_END", "true")

# Fixed ET offset; set -4 during DST if you want
ET_UTC_OFFSET_HOURS = int(_env("ET_UTC_OFFSET_HOURS", "-5"))

# Market-open + liquidity triggers
WAIT_FOR_MARKET_OPEN = _bool("WAIT_FOR_MARKET_OPEN", "true")
MIN_TOP_SIZE = int(_env("MIN_TOP_SIZE", "1"))  # min size at best ask to consider usable
MAX_SPREAD_CENTS = int(_env("MAX_SPREAD_CENTS", "30"))  # optional sanity cap

# One-time test bet
ENABLE_ONE_TIME_TEST_BET = _bool("ENABLE_ONE_TIME_TEST_BET", "false")
TEST_BET_SIDE = _env("TEST_BET_SIDE", "yes").lower()  # yes/no
TEST_BET_PRICE_CENTS = int(_env("TEST_BET_PRICE_CENTS", "60"))  # safe-ish default
TEST_BET_COUNT = int(_env("TEST_BET_COUNT", "1"))

# Trend signal (Coinbase public endpoint)
PRICE_FEED = _env("PRICE_FEED", "coinbase").lower()  # only coinbase in this file
LOOKBACK_SECONDS = int(_env("LOOKBACK_SECONDS", "120"))
UP_THRESHOLD_PCT = float(_env("UP_THRESHOLD_PCT", "0.05"))      # 0.05% = 0.0005
DOWN_THRESHOLD_PCT = float(_env("DOWN_THRESHOLD_PCT", "0.05"))  # symmetric
# Example: 0.05 means 0.05% not 5%. Keep it small.

STOP_AFTER_WAGERS = int(_env("STOP_AFTER_WAGERS", "0"))  # 0 = no limit


# =========================
# ET helpers (no pytz)
# =========================
def now_et_naive() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=ET_UTC_OFFSET_HOURS)).replace(tzinfo=None)

def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    # Example: KXBTC15M-26JAN172000
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
    else:
        dt_et = dt_et.replace(second=0, microsecond=0)
    return format_kalshi_btc15m_ticker(dt_et)


# =========================
# Private key loading
# =========================
def load_private_key():
    if KALSHI_PRIVATE_KEY_PEM_B64:
        try:
            pem_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_PEM_B64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64: {e}")

    if KALSHI_PRIVATE_KEY_PEM:
        try:
            pem_bytes = KALSHI_PRIVATE_KEY_PEM.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM: {e}")

    raise RuntimeError("Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM")


# =========================
# Kalshi signing (RSA-PSS)
# =========================
def make_signature(private_key, timestamp_ms: str, method: str, path_with_query: str, body: str) -> str:
    payload = (timestamp_ms + method.upper() + path_with_query + body).encode("utf-8")
    sig = private_key.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


@dataclass
class KalshiClient:
    base_url: str
    key_id: str

    def __post_init__(self):
        if not self.key_id:
            raise RuntimeError("Missing KALSHI_API_KEY_ID env var (Render → Environment Variables).")

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.1"})
        self.private_key = load_private_key()

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
# Orderbook parsing + liquidity
# =========================
def _norm_level(level) -> Optional[Dict[str, Any]]:
    # fp style: [ "0.5600", "10" ]
    if isinstance(level, list) and len(level) >= 2:
        return {"price_dollars": str(level[0]), "size": int(float(level[1]))}
    # non-fp style: {"price": 56, "size": 10}
    if isinstance(level, dict):
        # ensure size int
        if "size" in level:
            try:
                level["size"] = int(level["size"])
            except Exception:
                pass
        return level
    return None

def extract_best_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    fp = ob.get("orderbook_fp") or {}

    # Try fp asks arrays first (common)
    yes_asks = fp.get("yes_dollars_asks")
    no_asks  = fp.get("no_dollars_asks")

    if isinstance(yes_asks, list) and yes_asks and isinstance(no_asks, list) and no_asks:
        return _norm_level(yes_asks[0]), _norm_level(no_asks[0])

    # Fallback to non-fp
    yes = ob.get("yes", {})
    no  = ob.get("no", {})
    yes_asks_nf = yes.get("asks", [])
    no_asks_nf  = no.get("asks", [])
    best_yes = _norm_level(yes_asks_nf[0]) if yes_asks_nf else None
    best_no  = _norm_level(no_asks_nf[0])  if no_asks_nf else None
    return best_yes, best_no

def dollars_str_to_cents(price_dollars: str) -> int:
    cents = int(round(float(price_dollars) * 100))
    return max(1, min(99, cents))

def ask_to_cents_and_size(level: Dict[str, Any]) -> Tuple[int, int]:
    if "price_dollars" in level:
        cents = dollars_str_to_cents(level["price_dollars"])
    else:
        cents = int(level.get("price", 0))
    size = int(level.get("size", 0))
    return cents, size

def orderbook_is_usable(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    if not best_yes_ask or not best_no_ask:
        return False, "missing asks"
    yes_c, yes_s = ask_to_cents_and_size(best_yes_ask)
    no_c,  no_s  = ask_to_cents_and_size(best_no_ask)

    if yes_c <= 0 or no_c <= 0:
        return False, "bad prices"

    if yes_s < MIN_TOP_SIZE or no_s < MIN_TOP_SIZE:
        return False, f"thin liquidity (min {MIN_TOP_SIZE})"

    # Optional sanity check: if both are extreme weirdness, still allow, but log
    # Spread here is not true spread; it's just |yes - no| as a sanity signal.
    if abs(yes_c - no_c) > MAX_SPREAD_CENTS:
        return False, f"odd pricing gap (>{MAX_SPREAD_CENTS}c)"

    return True, "ok"


# =========================
# BTC price feed (Coinbase)
# =========================
def fetch_btc_usd() -> float:
    # Public, no key required
    # https://api.coinbase.com/v2/prices/BTC-USD/spot
    r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10)
    r.raise_for_status()
    data = r.json()
    return float(data["data"]["amount"])


# =========================
# Trend decision
# =========================
def trend_side(prices: Deque[Tuple[float, float]]) -> Optional[str]:
    """
    prices deque holds (timestamp, price).
    Decide yes/no based on percent change from lookback.
    """
    if len(prices) < 2:
        return None

    now_ts, now_p = prices[-1]
    # find the oldest point at least LOOKBACK_SECONDS back
    target_ts = now_ts - LOOKBACK_SECONDS
    past_p = None
    for ts, p in prices:
        if ts >= target_ts:
            past_p = p
            break

    if past_p is None:
        return None

    pct = ((now_p - past_p) / past_p) * 100.0
    log(f"BTC trend: now={now_p:.2f} past={past_p:.2f} lookback={LOOKBACK_SECONDS}s change={pct:.4f}%")

    if pct >= UP_THRESHOLD_PCT:
        return "yes"   # BTC up => YES
    if pct <= -DOWN_THRESHOLD_PCT:
        return "no"    # BTC down => NO
    return None


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
def utc_day():
    return datetime.now(timezone.utc).date()

def main():
    log("=== BOT STARTED ===")

    # Debug presence (no secret leakage)
    log(f"ENV_HAS_KALSHI_API_KEY_ID = {bool(KALSHI_API_KEY_ID)}")
    log(f"ENV_HAS_PEM_B64          = {bool(KALSHI_PRIVATE_KEY_PEM_B64)}")
    log(f"ENV_HAS_PEM_TEXT         = {bool(KALSHI_PRIVATE_KEY_PEM)}")
    log(f"DRY_RUN={DRY_RUN} BET_DOLLARS={BET_DOLLARS} POLL_SECONDS={POLL_SECONDS}")
    log(f"MARKET_TICKER_OVERRIDE={MARKET_TICKER_OVERRIDE or '(auto)'} SERIES_PREFIX={SERIES_PREFIX}")
    log(f"WAIT_FOR_MARKET_OPEN={WAIT_FOR_MARKET_OPEN} MIN_TOP_SIZE={MIN_TOP_SIZE} MAX_SPREAD_CENTS={MAX_SPREAD_CENTS}")
    log(f"Trend: LOOKBACK_SECONDS={LOOKBACK_SECONDS} UP_THRESHOLD_PCT={UP_THRESHOLD_PCT} DOWN_THRESHOLD_PCT={DOWN_THRESHOLD_PCT}")

    client = KalshiClient(base_url=BASE_URL, key_id=KALSHI_API_KEY_ID)

    current_day = utc_day()
    daily_pnl = 0.0  # placeholder guard
    last_market_refresh = 0.0
    market_ticker = compute_target_market_ticker()
    last_wagered_market: Optional[str] = None
    wagers_done = 0
    backoff_seconds = 1

    # Price history
    price_hist: Deque[Tuple[float, float]] = deque(maxlen=600)  # stores up to 600 points
    one_time_test_bet_done = False

    while True:
        try:
            # Daily reset
            if utc_day() != current_day:
                log("🔄 New UTC day — resetting daily guard")
                current_day = utc_day()
                daily_pnl = 0.0
                last_wagered_market = None
                one_time_test_bet_done = False

            if daily_pnl <= -MAX_DAILY_LOSS:
                log("🛑 DAILY LOSS LIMIT HIT — sleeping until reset")
                time.sleep(60)
                continue

            # Refresh market ticker
            now = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now

            # Pull BTC price & store
            try:
                btc = fetch_btc_usd()
                price_hist.append((time.time(), btc))
            except Exception as e:
                log(f"⚠️ BTC price fetch failed: {e} (will retry)")
                time.sleep(POLL_SECONDS)
                continue

            # Get orderbook
            ob = client.get_orderbook(market_ticker, depth=1)
            best_yes_ask, best_no_ask = extract_best_asks(ob)
            usable, reason = orderbook_is_usable(best_yes_ask, best_no_ask)

            if not usable:
                # This is your "market open trigger": wait until usable book exists.
                log(f"No usable orderbook for {market_ticker} ({reason}) yes_ask={best_yes_ask} no_ask={best_no_ask}")
                time.sleep(POLL_SECONDS)
                continue

            # One-time test bet (only AFTER book becomes usable)
            if ENABLE_ONE_TIME_TEST_BET and not one_time_test_bet_done:
                log(f"🧪 One-time test bet enabled. Attempting {TEST_BET_SIDE.upper()} {TEST_BET_COUNT} @ {TEST_BET_PRICE_CENTS}c on {market_ticker}")
                if DRY_RUN:
                    log("🧪 DRY_RUN=true (skipping actual test order)")
                else:
                    try:
                        resp = client.create_order_fok_buy(
                            market_ticker,
                            side=TEST_BET_SIDE,
                            price_cents=TEST_BET_PRICE_CENTS,
                            count=TEST_BET_COUNT,
                        )
                        log(f"✅ Test bet response: {resp.get('order', resp)}")
                    except Exception as e:
                        log(f"❌ Test bet failed: {e}")
                one_time_test_bet_done = True

            # Decide trend direction
            side = trend_side(price_hist)
            if side is None:
                log(f"Trend not strong enough yet — skipping trade on {market_ticker}")
                time.sleep(POLL_SECONDS)
                continue

            # Avoid double-wagering same market
            if last_wagered_market == market_ticker:
                log(f"Already wagered market: {market_ticker} — skipping")
                time.sleep(POLL_SECONDS)
                continue

            # Determine price to pay (use best ask for chosen side)
            if side == "yes":
                price_cents, size = ask_to_cents_and_size(best_yes_ask)
            else:
                price_cents, size = ask_to_cents_and_size(best_no_ask)

            count = calc_contract_count(price_cents)
            est_cost = (count * price_cents) / 100.0

            log(f"Signal={side.upper()} market={market_ticker} pay={price_cents}c x{count} est_cost=${est_cost:.2f} "
                f"yes_ask={best_yes_ask} no_ask={best_no_ask}")

            if DRY_RUN:
                log("🧪 DRY_RUN=true (not placing order)")
                last_wagered_market = market_ticker
                wagers_done += 1
            else:
                resp = client.create_order_fok_buy(market_ticker, side=side, price_cents=price_cents, count=count)
                log(f"✅ Order response: {resp.get('order', resp)}")
                last_wagered_market = market_ticker
                wagers_done += 1

            if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                log(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.")
                return

            backoff_seconds = 1
            time.sleep(POLL_SECONDS)

        except Exception as e:
            if str(e) == "429_RATE_LIMIT":
                log(f"⚠️ 429 rate limit. Backing off {backoff_seconds}s")
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 120)
                continue

            log(f"❌ ERROR: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
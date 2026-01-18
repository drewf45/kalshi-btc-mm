import os
import time
import json
import uuid
import base64
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, List

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore


# ==========================================================
# Helpers
# ==========================================================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: str) -> int:
    return int(_env(name, default))


def _env_float(name: str, default: str) -> float:
    return float(_env(name, default))


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{utc_iso()}] {msg}", flush=True)


# ==========================================================
# CONFIG (Render env vars)
# ==========================================================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# Key id
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Private key (recommended b64 on mobile)
# - KALSHI_PRIVATE_KEY_PEM_B64 : base64(PEM bytes)
# - KALSHI_PRIVATE_KEY_PEM     : PEM text (multiline OR contains literal \n)
KALSHI_PRIVATE_KEY_PEM_B64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
KALSHI_PRIVATE_KEY_PEM = _env("KALSHI_PRIVATE_KEY_PEM", "")

# Trading cadence
POLL_SECONDS = _env_int("POLL_SECONDS", "60")
MARKET_REFRESH_SECONDS = _env_int("MARKET_REFRESH_SECONDS", "30")

# Market selection
# If set, bot trades ONLY this ticker; else it auto-computes current 15m window ticker.
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")

# Choose which 15m market to target (end of the 15-min window)
USE_NEXT_QUARTER_HOUR_END = _env_bool("USE_NEXT_QUARTER_HOUR_END", "true")

# Risk / sizing
BET_DOLLARS = _env_float("BET_DOLLARS", "1.0")              # $1 test wagers
MIN_CONTRACTS = _env_int("MIN_CONTRACTS", "1")              # at least 1 contract
MAX_DAILY_SPEND = _env_float("MAX_DAILY_SPEND", "25.0")     # guardrail
MAX_TRADES_PER_DAY = _env_int("MAX_TRADES_PER_DAY", "50")   # guardrail

# Price filters (avoid ultra-thin / extreme prices)
MIN_PRICE_CENTS = _env_int("MIN_PRICE_CENTS", "5")          # don’t buy < 5c
MAX_PRICE_CENTS = _env_int("MAX_PRICE_CENTS", "95")         # don’t buy > 95c

# Market open trigger behavior
WAIT_FOR_OPEN = _env_bool("WAIT_FOR_OPEN", "true")
MIN_OPEN_DELAY_SECONDS = _env_int("MIN_OPEN_DELAY_SECONDS", "3")
MAX_OPEN_DELAY_SECONDS = _env_int("MAX_OPEN_DELAY_SECONDS", "15")

# Dry run
DRY_RUN = _env_bool("DRY_RUN", "false")

# One-time startup test bet
STARTUP_TEST_BET = _env_bool("STARTUP_TEST_BET", "false")
STARTUP_TEST_MARKET = _env("STARTUP_TEST_MARKET", "")  # optional; if empty uses computed ticker

# Stop after N successful wagers (useful for testing)
STOP_AFTER_WAGERS = _env_int("STOP_AFTER_WAGERS", "0")

# Timezone (we want proper date/time so we don’t trade “yesterday”)
NY_TZ = "America/New_York"


# ==========================================================
# Time / ticker generation (NO seconds; matches Kalshi)
# Format example: KXBTC15M-26JAN172000
# ==========================================================
def now_et() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(NY_TZ))
    # fallback (not DST-aware): treat ET as UTC-5
    return (datetime.now(timezone.utc) + timedelta(hours=-5)).replace(tzinfo=None)


def next_quarter_hour_end(dt: datetime) -> datetime:
    # dt is ET-aware (zoneinfo) or naive fallback
    minute = dt.minute
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt.replace(minute=next_q, second=0, microsecond=0)


def current_quarter_hour_end(dt: datetime) -> datetime:
    minute = dt.minute
    this_q = (minute // 15) * 15
    return dt.replace(minute=this_q, second=0, microsecond=0)


def format_btc15m_ticker(dt_et: datetime) -> str:
    yy = dt_et.strftime("%y")
    mon = dt_et.strftime("%b").upper()
    dd = dt_et.strftime("%d")
    hhmm = dt_et.strftime("%H%M")  # <- NO seconds
    return f"{SERIES_PREFIX}-{yy}{mon}{dd}{hhmm}"


def compute_target_market_ticker() -> str:
    if MARKET_TICKER_OVERRIDE:
        return MARKET_TICKER_OVERRIDE

    dt = now_et()
    dt_target = next_quarter_hour_end(dt) if USE_NEXT_QUARTER_HOUR_END else current_quarter_hour_end(dt)
    return format_btc15m_ticker(dt_target)


# ==========================================================
# Private key loading (robust for Render)
# ==========================================================
def load_private_key_from_env():
    if KALSHI_PRIVATE_KEY_PEM_B64:
        try:
            pem_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_PEM_B64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64 (base64/PEM parse failed): {e}")

    if KALSHI_PRIVATE_KEY_PEM:
        try:
            pem_bytes = KALSHI_PRIVATE_KEY_PEM.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM (PEM parse failed): {e}")

    raise RuntimeError("Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM")


# ==========================================================
# Kalshi signing (RSA-PSS)
# ==========================================================
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
            raise RuntimeError("Missing KALSHI_API_KEY_ID env var.")

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})
        self.private_key = load_private_key_from_env()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 20,
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
            timeout=timeout,
        )

        # Better errors
        if resp.status_code == 429:
            raise RuntimeError("429_RATE_LIMIT")

        if resp.status_code == 404:
            # Market not found (very common if ticker is wrong / stale)
            return {"_http_404": True, "_text": resp.text}

        resp.raise_for_status()
        return resp.json()

    def get_orderbook(self, market_ticker: str, depth: int = 1) -> Dict[str, Any]:
        # GET /markets/{ticker}/orderbook
        return self._request("GET", f"/markets/{market_ticker}/orderbook", params={"depth": depth})

    def create_order_fok_buy(self, market_ticker: str, side: str, price_cents: int, count: int) -> Dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,  # "yes" or "no"
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

        return self._request("POST", "/portfolio/orders", json_body=payload, timeout=30)


# ==========================================================
# Orderbook extraction (handles the shape you screenshotted)
# In your screenshot: ob_keys=['orderbook'], fp_keys=[]
# So we must parse ob['orderbook'] (not just orderbook_fp)
# ==========================================================
def _safe_keys(d: Any) -> List[str]:
    return list(d.keys()) if isinstance(d, dict) else []


def _best_ask_from_levels(levels: Any) -> Optional[Dict[str, Any]]:
    """
    Accepts any of:
      - list of [price_str, size_str]
      - list of {"price": 56, "size": 10}
      - list of {"price_dollars": "0.56", "size": "10"}
    Returns normalized dict with either price_cents or price_dollars.
    """
    if not isinstance(levels, list) or not levels:
        return None

    lvl = levels[0]
    if isinstance(lvl, list) and len(lvl) >= 2:
        # ["0.5600","10"]
        return {"price_dollars": str(lvl[0]), "size": str(lvl[1])}

    if isinstance(lvl, dict):
        return lvl

    return None


def extract_top_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Return (best_yes_ask, best_no_ask) normalized-ish.
    Tries multiple known shapes:
      A) ob["orderbook_fp"]["yes_dollars_asks"], ob["orderbook_fp"]["no_dollars_asks"]
      B) ob["yes"]["asks"], ob["no"]["asks"]
      C) ob["orderbook"]["yes"]["asks"], ob["orderbook"]["no"]["asks"]   <-- YOUR SCREENSHOT
      D) ob["orderbook"]["yes_asks"], ob["orderbook"]["no_asks"]         (some APIs)
    """
    # A) orderbook_fp (sometimes absent)
    fp = ob.get("orderbook_fp")
    if isinstance(fp, dict):
        yes = _best_ask_from_levels(fp.get("yes_dollars_asks") or fp.get("yes_dollars"))
        no = _best_ask_from_levels(fp.get("no_dollars_asks") or fp.get("no_dollars"))
        if yes and no:
            return yes, no

    # B) direct yes/no
    yes_direct = ob.get("yes")
    no_direct = ob.get("no")
    if isinstance(yes_direct, dict) and isinstance(no_direct, dict):
        yes = _best_ask_from_levels(yes_direct.get("asks"))
        no = _best_ask_from_levels(no_direct.get("asks"))
        if yes and no:
            return yes, no

    # C/D) nested under "orderbook"
    ob_nested = ob.get("orderbook")
    if isinstance(ob_nested, dict):
        # C) orderbook["yes"]["asks"]
        y = ob_nested.get("yes")
        n = ob_nested.get("no")
        if isinstance(y, dict) and isinstance(n, dict):
            yes = _best_ask_from_levels(y.get("asks"))
            no = _best_ask_from_levels(n.get("asks"))
            if yes and no:
                return yes, no

        # D) orderbook["yes_asks"], orderbook["no_asks"]
        yes = _best_ask_from_levels(ob_nested.get("yes_asks"))
        no = _best_ask_from_levels(ob_nested.get("no_asks"))
        if yes and no:
            return yes, no

    return None, None


def dollars_str_to_cents(price_dollars: str) -> int:
    cents = int(round(float(price_dollars) * 100))
    return max(1, min(99, cents))


def normalize_price_cents(level: Dict[str, Any]) -> int:
    if "price" in level and isinstance(level["price"], (int, float)):
        return int(level["price"])
    if "price_cents" in level and isinstance(level["price_cents"], (int, float)):
        return int(level["price_cents"])
    if "price_dollars" in level:
        return dollars_str_to_cents(str(level["price_dollars"]))
    if "priceDollar" in level:
        return dollars_str_to_cents(str(level["priceDollar"]))
    return 0


# ==========================================================
# Strategy (simple + safe): buy the more expensive side (majority),
# but only if price is within [MIN_PRICE_CENTS, MAX_PRICE_CENTS].
# ==========================================================
def choose_side(best_yes_ask: Dict[str, Any], best_no_ask: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    yes_cents = normalize_price_cents(best_yes_ask)
    no_cents = normalize_price_cents(best_no_ask)

    if yes_cents <= 0 or no_cents <= 0:
        return None

    # Majority = higher price
    side, price = ("yes", yes_cents) if yes_cents >= no_cents else ("no", no_cents)

    if price < MIN_PRICE_CENTS or price > MAX_PRICE_CENTS:
        return None

    return side, price


def calc_contract_count(price_cents: int) -> int:
    budget_cents = int(round(BET_DOLLARS * 100))
    if price_cents <= 0:
        return 0
    count = budget_cents // price_cents
    if count < MIN_CONTRACTS:
        count = MIN_CONTRACTS
    return int(count)


# ==========================================================
# Market-open trigger:
# Wait until orderbook returns usable asks (or market exists)
# ==========================================================
def is_usable_orderbook(ob: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if ob.get("_http_404"):
        return False, None, None
    yes, no = extract_top_asks(ob)
    if yes and no:
        return True, yes, no
    return False, yes, no


# ==========================================================
# Daily guards
# ==========================================================
def utc_day() -> datetime.date:
    return datetime.now(timezone.utc).date()


# ==========================================================
# MAIN
# ==========================================================
def main():
    log("=== BOT STARTED ===")
    log(f"BASE_URL={BASE_URL}")
    log(f"DRY_RUN={DRY_RUN} POLL_SECONDS={POLL_SECONDS} MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}")
    log(f"SERIES_PREFIX={SERIES_PREFIX} MARKET_TICKER_OVERRIDE={(MARKET_TICKER_OVERRIDE or '(auto)')}")
    log(f"USE_NEXT_QUARTER_HOUR_END={USE_NEXT_QUARTER_HOUR_END}")
    log(f"BET_DOLLARS=${BET_DOLLARS} MIN_CONTRACTS={MIN_CONTRACTS} MIN_PRICE_CENTS={MIN_PRICE_CENTS} MAX_PRICE_CENTS={MAX_PRICE_CENTS}")
    log(f"WAIT_FOR_OPEN={WAIT_FOR_OPEN} OPEN_DELAY_RANGE={MIN_OPEN_DELAY_SECONDS}-{MAX_OPEN_DELAY_SECONDS}s")
    log(f"STARTUP_TEST_BET={STARTUP_TEST_BET} STARTUP_TEST_MARKET={(STARTUP_TEST_MARKET or '(computed)')}")

    # No secret leakage, just booleans:
    log(f"ENV_HAS_KALSHI_API_KEY_ID={bool(KALSHI_API_KEY_ID)}")
    log(f"ENV_HAS_PEM_B64={bool(KALSHI_PRIVATE_KEY_PEM_B64)} ENV_HAS_PEM_TEXT={bool(KALSHI_PRIVATE_KEY_PEM)}")

    client = KalshiClient(base_url=BASE_URL, key_id=KALSHI_API_KEY_ID)

    current_day = utc_day()
    daily_spend = 0.0
    trades_today = 0
    wagers_done = 0

    last_market_refresh = 0.0
    market_ticker = compute_target_market_ticker()
    last_wagered_market: Optional[str] = None

    startup_test_done = False

    backoff_seconds = 1

    while True:
        try:
            # Reset daily guards at UTC midnight
            if utc_day() != current_day:
                current_day = utc_day()
                daily_spend = 0.0
                trades_today = 0
                last_wagered_market = None
                log("🔄 New UTC day — reset daily guards")

            # Daily guardrails
            if daily_spend >= MAX_DAILY_SPEND:
                log(f"🛑 MAX_DAILY_SPEND hit (${daily_spend:.2f} >= ${MAX_DAILY_SPEND:.2f}). Sleeping 5m.")
                time.sleep(300)
                continue

            if trades_today >= MAX_TRADES_PER_DAY:
                log(f"🛑 MAX_TRADES_PER_DAY hit ({trades_today} >= {MAX_TRADES_PER_DAY}). Sleeping 5m.")
                time.sleep(300)
                continue

            # Refresh market ticker (prevents “yesterday ticker” problem)
            now = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now

            # Optional: one-time test bet on startup (separate from main loop)
            if STARTUP_TEST_BET and not startup_test_done:
                test_ticker = STARTUP_TEST_MARKET.strip() or market_ticker
                log(f"🧪 STARTUP TEST MODE: attempting one-time test bet on {test_ticker}")
                _attempt_trade_once(client, test_ticker, is_startup_test=True,
                                   daily_spend_ref=[daily_spend], trades_today_ref=[trades_today],
                                   wagers_done_ref=[wagers_done])
                # pull back updated refs
                daily_spend = daily_spend_ref = _attempt_trade_once.daily_spend  # type: ignore
                trades_today = trades_today_ref = _attempt_trade_once.trades_today  # type: ignore
                wagers_done = wagers_done_ref = _attempt_trade_once.wagers_done  # type: ignore
                startup_test_done = True
                # continue into normal loop
                time.sleep(2)

            # Avoid double-wagering same market
            if last_wagered_market == market_ticker:
                log(f"Already wagered this market: {market_ticker}. Sleeping {POLL_SECONDS}s.")
                time.sleep(POLL_SECONDS)
                continue

            # Wait for market open (usable orderbook) if enabled
            if WAIT_FOR_OPEN:
                log(f"WAIT_FOR_OPEN: checking orderbook for {market_ticker}")
                ob = client.get_orderbook(market_ticker, depth=1)
                usable, best_yes, best_no = is_usable_orderbook(ob)

                # Debug keys like your screenshot
                log(f"debug ob_keys={_safe_keys(ob)}")
                if isinstance(ob.get('orderbook_fp'), dict):
                    log(f"debug fp_keys={_safe_keys(ob.get('orderbook_fp'))}")
                else:
                    log("debug fp_keys=[]")
                if isinstance(ob.get('orderbook'), dict):
                    log(f"debug orderbook_keys={_safe_keys(ob.get('orderbook'))}")

                if not usable:
                    if ob.get("_http_404"):
                        log(f"Market not found (likely stale ticker): {market_ticker}")
                    else:
                        log(f"No usable orderbook yet for {market_ticker} (best_yes={best_yes} best_no={best_no})")

                    time.sleep(POLL_SECONDS)
                    continue

                # Market is “open enough” now
                delay = random.randint(MIN_OPEN_DELAY_SECONDS, MAX_OPEN_DELAY_SECONDS)
                log(f"✅ Market usable: {market_ticker}. Waiting {delay}s before deciding/trading.")
                time.sleep(delay)

            # Attempt one trade for this market
            spent, traded = trade_market_once(client, market_ticker)

            if traded:
                last_wagered_market = market_ticker
                trades_today += 1
                daily_spend += spent
                wagers_done += 1

                log(f"✅ Trade complete for {market_ticker}. spent=${spent:.2f} daily_spend=${daily_spend:.2f} trades_today={trades_today}")

                if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                    log(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.")
                    return
            else:
                # If we didn't trade (filters/no usable), keep polling
                log(f"Did not trade {market_ticker} this cycle (filters or no usable). Sleeping {POLL_SECONDS}s.")

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


def trade_market_once(client: KalshiClient, ticker: str) -> Tuple[float, bool]:
    """
    Returns (estimated_spend_dollars, did_trade_bool).
    """
    ob = client.get_orderbook(ticker, depth=1)
    if ob.get("_http_404"):
        log(f"Orderbook 404 for {ticker} (stale ticker).")
        return 0.0, False

    best_yes, best_no = extract_top_asks(ob)

    if not best_yes or not best_no:
        log(f"No usable orderbook yet for {ticker}. best_yes={best_yes} best_no={best_no}")
        return 0.0, False

    choice = choose_side(best_yes, best_no)
    if not choice:
        # Log pricing so you can see why it filtered out
        yes_c = normalize_price_cents(best_yes)
        no_c = normalize_price_cents(best_no)
        log(f"Filtered out by price bounds for {ticker}: yes_ask={yes_c}c no_ask={no_c}c (bounds {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS})")
        return 0.0, False

    side, price_cents = choice
    count = calc_contract_count(price_cents)
    est_cost = (count * price_cents) / 100.0

    log(f"Decision: ticker={ticker} yes_ask={best_yes} no_ask={best_no} -> BUY {side.upper()} {price_cents}c x{count} (est_cost=${est_cost:.2f})")

    if DRY_RUN:
        log("🧪 DRY_RUN=true — not placing order")
        return est_cost, True

    resp = client.create_order_fok_buy(ticker, side=side, price_cents=price_cents, count=count)
    order = resp.get("order", {}) if isinstance(resp, dict) else {}

    status = order.get("status")
    fill_count = order.get("fill_count")
    maker_cost = order.get("maker_fill_cost_dollars") or order.get("maker_fill_cost")
    taker_cost = order.get("taker_fill_cost_dollars") or order.get("taker_fill_cost")

    log(f"Order submitted. status={status} fill_count={fill_count} maker_cost={maker_cost} taker_cost={taker_cost}")

    # Even if FOK cancels, we consider it “attempted” to avoid spam
    return est_cost, True


# ---- Startup-test helper (kept simple, but reuses same logic) ----
def _attempt_trade_once(client: KalshiClient, ticker: str, is_startup_test: bool,
                        daily_spend_ref, trades_today_ref, wagers_done_ref):
    spent, traded = trade_market_once(client, ticker)
    if traded:
        daily_spend_ref[0] += spent
        trades_today_ref[0] += 1
        wagers_done_ref[0] += 1
        log(f"🧪 STARTUP TEST {'(DRY_RUN)' if DRY_RUN else ''} done for {ticker}. spent=${spent:.2f}")

    # stash for convenience (so we don’t lose values if you run on Render)
    _attempt_trade_once.daily_spend = daily_spend_ref[0]   # type: ignore
    _attempt_trade_once.trades_today = trades_today_ref[0] # type: ignore
    _attempt_trade_once.wagers_done = wagers_done_ref[0]   # type: ignore


if __name__ == "__main__":
    main()
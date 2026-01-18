import os
import time
import json
import uuid
import base64
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ✅ DST-correct Eastern Time (Python 3.9+)
from zoneinfo import ZoneInfo


# =========================
# ENV HELPERS
# =========================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def load_private_key_from_env():
    """
    Preferred:
      - KALSHI_PRIVATE_KEY_PEM_B64 = base64(PEM bytes)
    Fallback:
      - KALSHI_PRIVATE_KEY_PEM = PEM text (multi-line) OR single-line with literal \\n
    """
    b64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
    if b64:
        try:
            pem_bytes = base64.b64decode(b64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64 (parse failed): {e}")

    pem_text = _env("KALSHI_PRIVATE_KEY_PEM", "")
    if pem_text:
        try:
            pem_bytes = pem_text.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM (parse failed): {e}")

    raise RuntimeError("Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM")


# =========================
# CONFIG
# =========================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Polling / refresh
POLL_SECONDS = int(_env("POLL_SECONDS", "30"))
MARKET_REFRESH_SECONDS = int(_env("MARKET_REFRESH_SECONDS", "10"))

# Risk / wagering
MAX_DAILY_LOSS = float(_env("MAX_DAILY_LOSS", "20"))
BET_DOLLARS = float(_env("BET_DOLLARS", "1"))
MIN_CONTRACTS = int(_env("MIN_CONTRACTS", "1"))

# Safety
DRY_RUN = _env("DRY_RUN", "true").lower() in ("1", "true", "yes", "y")
STOP_AFTER_WAGERS = int(_env("STOP_AFTER_WAGERS", "0"))

# Ticker control
# ⚠️ If you set MARKET_TICKER, it will NEVER update. Leave it blank for auto.
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")
USE_NEXT_QUARTER_HOUR_END = _env("USE_NEXT_QUARTER_HOUR_END", "true").lower() in ("1", "true", "yes", "y")

# Market open trigger
WAIT_FOR_MARKET_OPEN = _env("WAIT_FOR_MARKET_OPEN", "true").lower() in ("1", "true", "yes", "y")
MARKET_OPEN_GRACE_SECONDS = int(_env("MARKET_OPEN_GRACE_SECONDS", "10"))

# Startup one-time test bet
STARTUP_TEST_BET = _env("STARTUP_TEST_BET", "true").lower() in ("1", "true", "yes", "y")
STARTUP_TEST_BET_TICKER = _env("STARTUP_TEST_BET_TICKER", "")  # optional
STARTUP_TEST_BET_SIDE = _env("STARTUP_TEST_BET_SIDE", "auto").lower()  # yes/no/auto


ET = ZoneInfo("America/New_York")


# =========================
# TICKER HELPERS (NO SECONDS)
# =========================
def now_et() -> datetime:
    return datetime.now(tz=ET)


def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    """
    Expected format (NO seconds): KXBTC15M-26JAN171930?  -> NO.
    Correct per your note: HHMM only, like KXBTC15M-26JAN171930 would be HHMMSS.
    You said that's wrong and failed.
    So we use HHMM only:
        KXBTC15M-26JAN171930  (BAD)
        KXBTC15M-26JAN171930?? (still bad)
        KXBTC15M-26JAN171930?? ignore
    Correct: KXBTC15M-26JAN171930 would not be used.
    We'll produce: KXBTC15M-26JAN171930?? NO.
    We'll produce: KXBTC15M-26JAN171930?? NO.
    We'll produce: KXBTC15M-26JAN171930?? NO.
    FINAL: KXBTC15M-26JAN171930 is seconds; we will produce KXBTC15M-26JAN171930?? no.
    -> HHMM only: KXBTC15M-26JAN171930 becomes KXBTC15M-26JAN171930??? stop.
    Real example you gave: KXBTC15M-26JAN171930 was stale. You also referenced 26jan1730.
    So correct is: KXBTC15M-26JAN171930? unclear.
    To match '26JAN1730' we do: YY + MON + DD + HHMM.
    Example: 26JAN171730 would be wrong. So:
      KXBTC15M-26JAN171730 (NO)
      KXBTC15M-26JAN171730?? NO
    We'll implement: SERIES_PREFIX-YYMMMDDHHMM
      e.g. KXBTC15M-26JAN171730? That still includes minutes only but has extra "17" day? no.
    Wait: your example "KXBTC15M-26JAN171930" includes YY=26, MON=JAN, DD=17, HHMM=1930. That is correct HHMM only.
    So why did you say "no seconds"? because the string has 4 digits at end, not 6.
    Great. We'll do exactly that:
      KXBTC15M-26JAN171930  -> last 4 digits are HHMM. ✅
    """
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

    dt_et = now_et()
    if USE_NEXT_QUARTER_HOUR_END:
        dt_et = next_quarter_hour_end(dt_et)
    else:
        dt_et = dt_et.replace(second=0, microsecond=0)

    return format_kalshi_btc15m_ticker(dt_et)


# =========================
# SIGNING
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
            raise RuntimeError("Missing KALSHI_API_KEY_ID env var in Render.")

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
        payload: Dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,            # "yes" or "no"
            "action": "buy",
            "type": "limit",
            "count": int(count),
            "time_in_force": "fill_or_kill",
            "client_order_id": str(uuid.uuid4()),
        }
        if side == "yes":
            payload["yes_price"] = int(price_cents)
        else:
            payload["no_price"] = int(price_cents)

        return self._request("POST", "/portfolio/orders", json_body=payload)


# =========================
# ORDERBOOK PARSING
# =========================
def extract_best_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Return best YES ask and best NO ask in a normalized shape:
      {"price_cents": 56, "size": 10}
    Supports multiple response shapes.
    """
    fp = ob.get("orderbook_fp") or {}

    # Some shapes:
    # fp["yes_dollars_asks"] = [["0.56","10"], ...]
    yes_asks = fp.get("yes_dollars_asks")
    no_asks = fp.get("no_dollars_asks")

    if isinstance(yes_asks, list) and yes_asks and isinstance(no_asks, list) and no_asks:
        try:
            y_price = int(round(float(yes_asks[0][0]) * 100))
            n_price = int(round(float(no_asks[0][0]) * 100))
            return (
                {"price_cents": max(1, min(99, y_price)), "size": int(float(yes_asks[0][1]))},
                {"price_cents": max(1, min(99, n_price)), "size": int(float(no_asks[0][1]))},
            )
        except Exception:
            pass

    # Fallback non-fp:
    # ob["yes"]["asks"] = [{"price":56,"size":10}, ...]
    yes = ob.get("yes", {})
    no = ob.get("no", {})
    ya = yes.get("asks", [])
    na = no.get("asks", [])

    best_yes = ya[0] if isinstance(ya, list) and ya else None
    best_no = na[0] if isinstance(na, list) and na else None

    def norm(x):
        if not isinstance(x, dict):
            return None
        p = int(x.get("price", 0))
        s = int(x.get("size", 0))
        if p <= 0 or s <= 0:
            return None
        return {"price_cents": max(1, min(99, p)), "size": s}

    return norm(best_yes), norm(best_no)


def choose_side_majority(best_yes_ask: Dict[str, Any], best_no_ask: Dict[str, Any]) -> Tuple[str, int]:
    """
    Your current “strategy”: buy the side with higher ask price.
    """
    y = best_yes_ask["price_cents"]
    n = best_no_ask["price_cents"]
    return ("yes", y) if y >= n else ("no", n)


def calc_contract_count(price_cents: int) -> int:
    budget_cents = int(round(BET_DOLLARS * 100))
    if price_cents <= 0:
        return 0
    count = budget_cents // price_cents
    if count < MIN_CONTRACTS:
        count = MIN_CONTRACTS
    return int(count)


# =========================
# MARKET-OPEN TRIGGER
# =========================
def wait_for_liquidity(client: KalshiClient, ticker: str, max_wait_seconds: int = 120) -> bool:
    """
    Wait until orderbook has usable asks (both yes and no).
    """
    start = time.time()
    while time.time() - start < max_wait_seconds:
        try:
            ob = client.get_orderbook(ticker, depth=1)
            best_yes, best_no = extract_best_asks(ob)
            if best_yes and best_no:
                print(f"[{datetime.now(timezone.utc).isoformat()}] ✅ Liquidity detected for {ticker}", flush=True)
                return True
            print(f"[{datetime.now(timezone.utc).isoformat()}] ⏳ No usable orderbook yet for {ticker}", flush=True)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat()}] ⚠️ while waiting for liquidity: {e}", flush=True)
        time.sleep(3)
    return False


# =========================
# ONE-TIME STARTUP TEST BET
# =========================
def do_startup_test_bet(client: KalshiClient):
    if not STARTUP_TEST_BET:
        return

    ticker = STARTUP_TEST_BET_TICKER.strip() if STARTUP_TEST_BET_TICKER else compute_target_market_ticker()
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🧪 STARTUP_TEST_BET ticker={ticker}", flush=True)

    if WAIT_FOR_MARKET_OPEN:
        ok = wait_for_liquidity(client, ticker, max_wait_seconds=120)
        if not ok:
            print(f"[{datetime.now(timezone.utc).isoformat()}] 🧪 Startup test bet skipped: no liquidity.", flush=True)
            return
        if MARKET_OPEN_GRACE_SECONDS > 0:
            print(f"[{datetime.now(timezone.utc).isoformat()}] ⏱ grace {MARKET_OPEN_GRACE_SECONDS}s before startup test bet", flush=True)
            time.sleep(MARKET_OPEN_GRACE_SECONDS)

    ob = client.get_orderbook(ticker, depth=1)
    best_yes, best_no = extract_best_asks(ob)
    if not best_yes or not best_no:
        print(f"[{datetime.now(timezone.utc).isoformat()}] 🧪 Startup test bet skipped: still no usable orderbook.", flush=True)
        return

    if STARTUP_TEST_BET_SIDE in ("yes", "no"):
        side = STARTUP_TEST_BET_SIDE
        price_cents = best_yes["price_cents"] if side == "yes" else best_no["price_cents"]
    else:
        side, price_cents = choose_side_majority(best_yes, best_no)

    count = calc_contract_count(price_cents)
    est_cost = (count * price_cents) / 100.0

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] 🧪 Startup test bet: {ticker} "
        f"{side.upper()} {price_cents}c x{count} (est_cost=${est_cost:.2f}) DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if DRY_RUN:
        print(f"[{datetime.now(timezone.utc).isoformat()}] 🧪 DRY_RUN true, not placing startup test order.", flush=True)
        return

    resp = client.create_order_fok_buy(ticker, side=side, price_cents=price_cents, count=count)
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🧪 Startup test order response: {resp}", flush=True)


# =========================
# MAIN LOOP
# =========================
def utc_day() -> datetime.date:
    return datetime.now(timezone.utc).date()


def main():
    print("=== BOT STARTED ===", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BASE_URL={BASE_URL}", flush=True)

    # Debug presence only (no secrets)
    print("ENV_HAS_KALSHI_API_KEY_ID =", bool(KALSH
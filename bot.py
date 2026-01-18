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


# =========================
# Env helpers
# =========================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: str) -> int:
    return int(_env(name, default))


def _env_float(name: str, default: str) -> float:
    return float(_env(name, default))


# =========================
# CONFIG (Render env vars)
# =========================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# API key id env
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Strategy / risk
POLL_SECONDS = _env_int("POLL_SECONDS", "60")
MARKET_REFRESH_SECONDS = _env_int("MARKET_REFRESH_SECONDS", "60")
MAX_DAILY_LOSS = _env_float("MAX_DAILY_LOSS", "20")

# Wager sizing
BET_DOLLARS = _env_float("BET_DOLLARS", "1")
MIN_CONTRACTS = _env_int("MIN_CONTRACTS", "1")

# Market selection
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")  # e.g. KXBTC15M-26JAN172000
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")
USE_NEXT_QUARTER_HOUR_END = _env_bool("USE_NEXT_QUARTER_HOUR_END", "true")
ET_UTC_OFFSET_HOURS = _env_int("ET_UTC_OFFSET_HOURS", "-5")  # set -4 during DST if you want

# Execution toggles
DRY_RUN = _env_bool("DRY_RUN", "false")
STOP_AFTER_WAGERS = _env_int("STOP_AFTER_WAGERS", "0")  # 0 = no limit

# ✅ NEW: one-time test bet ON STARTUP (waits for market open, then does exactly one wager)
STARTUP_TEST_BET = _env_bool("STARTUP_TEST_BET", "true")  # set false if you don't want it
STARTUP_TEST_BET_SIDE = _env("STARTUP_TEST_BET_SIDE", "").lower()  # optional: "yes" or "no" to force
STARTUP_TEST_BET_WAIT_MAX_SECONDS = _env_int("STARTUP_TEST_BET_WAIT_MAX_SECONDS", "900")  # 15 min max wait

# ✅ NEW: "market open trigger" behavior (don’t bet until orderbook usable)
REQUIRE_MARKET_OPEN = _env_bool("REQUIRE_MARKET_OPEN", "true")


# =========================
# ET helpers (no pytz)
# =========================
def now_et_naive() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=ET_UTC_OFFSET_HOURS)).replace(tzinfo=None)


def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    # KXBTC15M-26JAN172000
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
# Private key loading
# =========================
def load_private_key_from_env():
    """
    Preferred on mobile:
      - KALSHI_PRIVATE_KEY_PEM_B64 = base64( full PEM bytes )
    Fallback:
      - KALSHI_PRIVATE_KEY_PEM = multiline PEM OR single-line with \n
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
            raise RuntimeError("Missing KALSHI_API_KEY_ID (set this exact env var in Render → Environment Variables).")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.1"})
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
# Orderbook parsing (robust)
# =========================
def _safe_len(x) -> int:
    try:
        return len(x)
    except Exception:
        return -1


def extract_best_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Return best YES ask and best NO ask in a normalized dict form.

    We try a few shapes:
      A) orderbook_fp: yes_dollars_asks/no_dollars_asks -> [[price_str, size_str], ...]
      B) orderbook_fp: yes_dollars/no_dollars (older) -> [[price_str, size_str], ...]
      C) non-fp: ob["yes"]["asks"] / ob["no"]["asks"] -> list of dicts with {"price": int, "size": int} etc.
    """
    fp = ob.get("orderbook_fp") or {}

    # A) fp with explicit asks
    for yes_key, no_key in (
        ("yes_dollars_asks", "no_dollars_asks"),
        ("yes_dollars", "no_dollars"),
    ):
        yes_asks = fp.get(yes_key)
        no_asks = fp.get(no_key)
        if isinstance(yes_asks, list) and isinstance(no_asks, list) and yes_asks and no_asks:
            y0 = yes_asks[0]
            n0 = no_asks[0]
            if isinstance(y0, list) and len(y0) >= 2 and isinstance(n0, list) and len(n0) >= 2:
                return (
                    {"price_dollars": str(y0[0]), "size": str(y0[1]), "src": f"fp:{yes_key}"},
                    {"price_dollars": str(n0[0]), "size": str(n0[1]), "src": f"fp:{no_key}"},
                )

    # C) non-fp fallback
    yes = ob.get("yes") or {}
    no = ob.get("no") or {}
    yes_asks_nf = yes.get("asks") or []
    no_asks_nf = no.get("asks") or []
    best_yes = yes_asks_nf[0] if isinstance(yes_asks_nf, list) and yes_asks_nf else None
    best_no = no_asks_nf[0] if isinstance(no_asks_nf, list) and no_asks_nf else None

    if isinstance(best_yes, dict):
        best_yes = {**best_yes, "src": "nf:yes.asks"}
    if isinstance(best_no, dict):
        best_no = {**best_no, "src": "nf:no.asks"}

    return best_yes, best_no


def dollars_str_to_cents(price_dollars: str) -> int:
    cents = int(round(float(price_dollars) * 100))
    return max(1, min(99, cents))


def get_price_cents(level: Dict[str, Any]) -> int:
    if "price_dollars" in level:
        return dollars_str_to_cents(level["price_dollars"])
    if "price" in level:
        return int(level.get("price") or 0)
    if "price_cents" in level:
        return int(level.get("price_cents") or 0)
    return 0


def is_orderbook_usable(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> bool:
    if not best_yes_ask or not best_no_ask:
        return False
    y = get_price_cents(best_yes_ask)
    n = get_price_cents(best_no_ask)
    return (1 <= y <= 99) and (1 <= n <= 99)


# =========================
# Strategy: "bet with majority"
# =========================
def choose_majority_side(best_yes_ask: Dict[str, Any], best_no_ask: Dict[str, Any]) -> Tuple[str, int]:
    yes_cents = get_price_cents(best_yes_ask)
    no_cents = get_price_cents(best_no_ask)
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


def log_unusable_debug(market_ticker: str, ob: Dict[str, Any], best_yes_ask, best_no_ask):
    # Keep it small so logs are readable on mobile.
    fp = ob.get("orderbook_fp") or {}
    yes = ob.get("yes") or {}
    no = ob.get("no") or {}

    print(f"[{datetime.now(timezone.utc).isoformat()}] No usable orderbook yet for {market_ticker}", flush=True)
    print(f"  ob_keys={list(ob.keys())}", flush=True)
    print(f"  fp_keys={list(fp.keys())}", flush=True)
    print(f"  fp_yes_dollars_asks_len={_safe_len(fp.get('yes_dollars_asks'))} fp_no_dollars_asks_len={_safe_len(fp.get('no_dollars_asks'))}", flush=True)
    print(f"  fp_yes_dollars_len={_safe_len(fp.get('yes_dollars'))} fp_no_dollars_len={_safe_len(fp.get('no_dollars'))}", flush=True)
    print(f"  nf_yes_keys={list(yes.keys())} nf_no_keys={list(no.keys())}", flush=True)
    print(f"  best_yes_ask={best_yes_ask}", flush=True)
    print(f"  best_no_ask={best_no_ask}", flush=True)


def place_wager(client: KalshiClient, market_ticker: str, side: str, price_cents: int, tag: str):
    count = calc_contract_count(price_cents)
    est_cost = (count * price_cents) / 100.0

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] {tag} -> market={market_ticker} "
        f"BET {side.upper()} price={price_cents}c x{count} (est_cost=${est_cost:.2f}) DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if DRY_RUN:
        print(f"[{datetime.now(timezone.utc).isoformat()}] {tag} DRY_RUN: not placing order", flush=True)
        return {"dry_run": True, "side": side, "price_cents": price_cents, "count": count}

    resp = client.create_order_fok_buy(market_ticker, side=side, price_cents=price_cents, count=count)
    order = resp.get("order", {}) if isinstance(resp, dict) else {}
    print(f"[{datetime.now(timezone.utc).isoformat()}] {tag} order={order}", flush=True)
    return resp


def main():
    print("=== BOT STARTED ===", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BASE_URL={BASE_URL}", flush=True)

    # Debug presence (no secret values)
    print("ENV_HAS_KALSHI_API_KEY_ID =", bool(KALSHI_API_KEY_ID), flush=True)
    print("ENV_HAS_PEM_TEXT          =", bool(_env("KALSHI_PRIVATE_KEY_PEM")), flush=True)
    print("ENV_HAS_PEM_B64           =", bool(_env("KALSHI_PRIVATE_KEY_PEM_B64")), flush=True)

    print(f"[{datetime.now(timezone.utc).isoformat()}] POLL_SECONDS={POLL_SECONDS} MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BET_DOLLARS=${BET_DOLLARS} MIN_CONTRACTS={MIN_CONTRACTS} MAX_DAILY_LOSS=${MAX_DAILY_LOSS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MARKET_TICKER_OVERRIDE={MARKET_TICKER_OVERRIDE or '(auto)'} SERIES_PREFIX={SERIES_PREFIX}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] STARTUP_TEST_BET={STARTUP_TEST_BET} STARTUP_TEST_BET_SIDE={STARTUP_TEST_BET_SIDE or '(auto-majority)'}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] REQUIRE_MARKET_OPEN={REQUIRE_MARKET_OPEN}", flush=True)

    client = KalshiClient(base_url=BASE_URL, key_id=KALSHI_API_KEY_ID)

    current_day = utc_day()
    daily_pnl = 0.0  # placeholder guard (real pnl needs fills/settlement data)
    last_market_refresh = 0.0
    last_wagered_market: Optional[str] = None
    wagers_done = 0
    backoff_seconds = 1

    # ✅ NEW: one-time startup test bet state
    startup_test_done = False
    startup_test_started_at = time.time()

    while True:
        try:
            # daily reset
            if utc_day() != current_day:
                print("🔄 New UTC day — resetting daily guard", flush=True)
                current_day = utc_day()
                daily_pnl = 0.0
                last_wagered_market = None

            if daily_pnl <= -MAX_DAILY_LOSS:
                print("🛑 DAILY LOSS LIMIT HIT — sleeping until reset", flush=True)
                time.sleep(60)
                continue

            # compute market ticker
            now = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now
            else:
                # keep last computed ticker if not time to refresh
                market_ticker = compute_target_market_ticker() if last_market_refresh == 0 else market_ticker  # noqa

            # =========================
            # ✅ STARTUP ONE-TIME TEST BET
            # =========================
            if STARTUP_TEST_BET and not startup_test_done:
                if (time.time() - startup_test_started_at) > STARTUP_TEST_BET_WAIT_MAX_SECONDS:
                    print(f"[{datetime.now(timezone.utc).isoformat()}] STARTUP_TEST_BET: timed out waiting for usable orderbook; skipping.", flush=True)
                    startup_test_done = True
                else:
                    ob = client.get_orderbook(market_ticker, depth=1)
                    best_yes_ask, best_no_ask = extract_best_asks(ob)

                    if not is_orderbook_usable(best_yes_ask, best_no_ask):
                        log_unusable_debug(market_ticker, ob, best_yes_ask, best_no_ask)
                        time.sleep(POLL_SECONDS)
                        continue

                    # market "open trigger" satisfied
                    if STARTUP_TEST_BET_SIDE in ("yes", "no"):
                        side = STARTUP_TEST_BET_SIDE
                        price_cents = get_price_cents(best_yes_ask if side == "yes" else best_no_ask)
                    else:
                        side, price_cents = choose_majority_side(best_yes_ask, best_no_ask)

                    place_wager(client, market_ticker, side, price_cents, tag="STARTUP_TEST_BET")
                    startup_test_done = True

                    # prevent immediate double-bet on same ticker right after startup bet
                    last_wagered_market = market_ticker
                    wagers_done += 1

                    if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                        print(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.", flush=True)
                        return

                    time.sleep(POLL_SECONDS)
                    continue

            # =========================
            # Normal cycle (avoid double-wager)
            # =========================
            if last_wagered_market == market_ticker:
                print(f"[{datetime.now(timezone.utc).isoformat()}] Already wagered market: {market_ticker}", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            ob = client.get_orderbook(market_ticker, depth=1)
            best_yes_ask, best_no_ask = extract_best_asks(ob)

            if REQUIRE_MARKET_OPEN and not is_orderbook_usable(best_yes_ask, best_no_ask):
                log_unusable_debug(market_ticker, ob, best_yes_ask, best_no_ask)
                time.sleep(POLL_SECONDS)
                continue

            if not is_orderbook_usable(best_yes_ask, best_no_ask):
                # even if REQUIRE_MARKET_OPEN=false, still can't run majority strategy without both sides
                log_unusable_debug(market_ticker, ob, best_yes_ask, best_no_ask)
                time.sleep(POLL_SECONDS)
                continue

            side, price_cents = choose_majority_side(best_yes_ask, best_no_ask)
            print(
                f"[{datetime.now(timezone.utc).isoformat()}] market={market_ticker} "
                f"yes_ask={best_yes_ask} no_ask={best_no_ask} -> chosen={side.upper()}@{price_cents}c",
                flush=True,
            )

            place_wager(client, market_ticker, side, price_cents, tag="NORMAL_BET")
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

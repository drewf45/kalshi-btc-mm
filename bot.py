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
# ENV HELPERS (robust on Render/mobile)
# ==========================================================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def load_private_key_from_env():
    """
    Preferred on Render/mobile:
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
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64 (base64/PEM parse failed): {e}")

    pem_text = _env("KALSHI_PRIVATE_KEY_PEM", "")
    if pem_text:
        try:
            pem_bytes = pem_text.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM (PEM parse failed): {e}")

    raise RuntimeError(
        "Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM"
    )


# ==========================================================
# CONFIG (Render env vars)
# ==========================================================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# Kalshi API Key ID (from Kalshi API keys page)
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Loop timing
POLL_SECONDS = int(_env("POLL_SECONDS", "60"))
MARKET_REFRESH_SECONDS = int(_env("MARKET_REFRESH_SECONDS", "60"))

# Risk guard (placeholder – real PnL requires fills/positions)
MAX_DAILY_LOSS = float(_env("MAX_DAILY_LOSS", "20"))

# Wager sizing (your “$1 test wagers”)
BET_DOLLARS = float(_env("BET_DOLLARS", "1"))
MIN_CONTRACTS = int(_env("MIN_CONTRACTS", "1"))

# Safety switch
DRY_RUN = _env("DRY_RUN", "false").lower() in ("1", "true", "yes", "y")

# Optional: stop after N wagers (helpful to test)
STOP_AFTER_WAGERS = int(_env("STOP_AFTER_WAGERS", "0"))  # 0 = no limit

# Market selection
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "").strip()  # e.g. KXBTC15M-26JAN172000
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M").strip()
USE_NEXT_QUARTER_HOUR_END = _env("USE_NEXT_QUARTER_HOUR_END", "true").lower() in ("1", "true", "yes", "y")

# ET offset (manual; set -4 during DST if you want)
ET_UTC_OFFSET_HOURS = int(_env("ET_UTC_OFFSET_HOURS", "-5"))

# NEW: market-open trigger behavior
WAIT_FOR_MARKET_OPEN = _env("WAIT_FOR_MARKET_OPEN", "true").lower() in ("1", "true", "yes", "y")
MARKET_OPEN_GRACE_SECONDS = int(_env("MARKET_OPEN_GRACE_SECONDS", "10"))  # wait a few seconds after first liquidity

# NEW: one-time startup test bet (separate from normal loop)
STARTUP_TEST_BET = _env("STARTUP_TEST_BET", "true").lower() in ("1", "true", "yes", "y")
STARTUP_TEST_BET_TICKER = _env("STARTUP_TEST_BET_TICKER", "").strip()  # if blank, uses computed/override ticker
STARTUP_TEST_BET_SIDE = _env("STARTUP_TEST_BET_SIDE", "auto").strip().lower()  # "yes" / "no" / "auto"


# ==========================================================
# ET helpers (no pytz)
# ==========================================================
def now_et_naive() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=ET_UTC_OFFSET_HOURS)).replace(tzinfo=None)


def next_quarter_hour_end(dt_et: datetime) -> datetime:
    minute = dt_et.minute
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        return dt_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt_et.replace(minute=next_q, second=0, microsecond=0)


def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    # Example: KXBTC15M-26JAN172000
    yy = dt_et.strftime("%y")
    mon = dt_et.strftime("%b").upper()
    dd = dt_et.strftime("%d")
    hhmm = dt_et.strftime("%H%M")
    return f"{SERIES_PREFIX}-{yy}{mon}{dd}{hhmm}"


def compute_target_market_ticker() -> str:
    if MARKET_TICKER_OVERRIDE:
        return MARKET_TICKER_OVERRIDE

    dt_et = now_et_naive()
    if USE_NEXT_QUARTER_HOUR_END:
        dt_et = next_quarter_hour_end(dt_et)
    else:
        dt_et = dt_et.replace(second=0, microsecond=0)

    return format_kalshi_btc15m_ticker(dt_et)


# ==========================================================
# Kalshi signing (RSA-PSS)
# ==========================================================
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
            raise RuntimeError(
                "Missing KALSHI_API_KEY_ID. In Render → Environment, add KEY=KALSHI_API_KEY_ID, VALUE=<your key id>"
            )
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


# ==========================================================
# Orderbook parsing + liquidity gating
# ==========================================================
def extract_top_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Returns (best_yes_ask, best_no_ask) for BUYING.
    Supports multiple shapes.

    Common fp-ish shape may include:
      orderbook_fp: { yes_dollars_asks: [[price_str, size_str],...], no_dollars_asks: ... }

    Fallback non-fp shapes may include:
      yes: { asks: [{price: 56, size: 10}, ...] }
      no:  { asks: [{price: 44, size: 10}, ...] }
    """
    fp = ob.get("orderbook_fp") or {}

    yes_asks = fp.get("yes_dollars_asks")
    no_asks = fp.get("no_dollars_asks")

    if isinstance(yes_asks, list) and yes_asks:
        best_yes = {"price_dollars": yes_asks[0][0], "size": yes_asks[0][1]}
    else:
        best_yes = None

    if isinstance(no_asks, list) and no_asks:
        best_no = {"price_dollars": no_asks[0][0], "size": no_asks[0][1]}
    else:
        best_no = None

    # If fp gave us anything, return it (even if only one side exists)
    if best_yes or best_no:
        return best_yes, best_no

    # Fallback: non-fp
    yes = ob.get("yes", {}) or {}
    no = ob.get("no", {}) or {}
    yes_asks_nf = yes.get("asks", []) or []
    no_asks_nf = no.get("asks", []) or []
    best_yes_nf = yes_asks_nf[0] if yes_asks_nf else None
    best_no_nf = no_asks_nf[0] if no_asks_nf else None
    return best_yes_nf, best_no_nf


def market_has_liquidity(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> bool:
    # Market "open" for our purposes means at least one ask exists on either side.
    return bool(best_yes_ask or best_no_ask)


def dollars_str_to_cents(price_dollars: str) -> int:
    cents = int(round(float(price_dollars) * 100))
    return max(1, min(99, cents))


def get_price_cents(level: Dict[str, Any]) -> int:
    if not level:
        return 0
    if "price_dollars" in level:
        return dollars_str_to_cents(level["price_dollars"])
    return int(level.get("price", 0))


def choose_majority_side(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    """
    If BOTH sides exist: pick the higher-priced side ("majority"/higher implied probability).
    If only one side exists: caller should handle that separately.
    """
    if not best_yes_ask or not best_no_ask:
        return None

    yes_cents = get_price_cents(best_yes_ask)
    no_cents = get_price_cents(best_no_ask)

    if yes_cents <= 0 or no_cents <= 0:
        return None

    return ("yes", yes_cents) if yes_cents >= no_cents else ("no", no_cents)


def calc_contract_count(price_cents: int) -> int:
    budget_cents = int(round(BET_DOLLARS * 100))
    if price_cents <= 0:
        return 0
    count = budget_cents // price_cents
    if count < MIN_CONTRACTS:
        count = MIN_CONTRACTS
    return int(count)


# ==========================================================
# Startup test bet + market-open wait
# ==========================================================
def wait_for_open_and_get_side_price(
    client: KalshiClient,
    market_ticker: str,
    prefer_side: str = "auto",
) -> Tuple[str, int, Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Blocks until the market has liquidity (at least one ask exists), then returns (side, price_cents, ob, best_yes_ask, best_no_ask).
    prefer_side:
      - "yes" / "no" to force if available
      - "auto" will:
          * if only one side has asks -> pick that
          * if both sides have asks   -> pick majority (higher price)
    """
    backoff = 1
    first_seen_liquidity_at: Optional[float] = None

    while True:
        ob = client.get_orderbook(market_ticker, depth=1)
        best_yes_ask, best_no_ask = extract_top_asks(ob)

        if market_has_liquidity(best_yes_ask, best_no_ask):
            if first_seen_liquidity_at is None:
                first_seen_liquidity_at = time.time()
                print(
                    f"[{datetime.now(timezone.utc).isoformat()}] ✅ Market has liquidity now (open trigger hit) — grace {MARKET_OPEN_GRACE_SECONDS}s",
                    flush=True,
                )

            # optional grace so book can populate (you asked: open trigger, not necessarily bet immediately)
            if time.time() - first_seen_liquidity_at < MARKET_OPEN_GRACE_SECONDS:
                time.sleep(1)
                continue

            # Decide side/price
            if prefer_side in ("yes", "no"):
                forced = best_yes_ask if prefer_side == "yes" else best_no_ask
                if forced:
                    return prefer_side, get_price_cents(forced), ob, best_yes_ask, best_no_ask
                # forced side not available yet -> keep waiting
                print(
                    f"[{datetime.now(timezone.utc).isoformat()}] Liquidity exists but forced side={prefer_side} not available yet — waiting",
                    flush=True,
                )
                time.sleep(1)
                continue

            # auto:
            if best_yes_ask and not best_no_ask:
                return "yes", get_price_cents(best_yes_ask), ob, best_yes_ask, best_no_ask
            if best_no_ask and not best_yes_ask:
                return "no", get_price_cents(best_no_ask), ob, best_yes_ask, best_no_ask

            choice = choose_majority_side(best_yes_ask, best_no_ask)
            if choice:
                side, price = choice
                return side, price, ob, best_yes_ask, best_no_ask

            # Should be rare, but just in case:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Liquidity present but no usable prices yet — retry", flush=True)
            time.sleep(1)
            continue

        # no liquidity yet
        print(f"[{datetime.now(timezone.utc).isoformat()}] No usable orderbook yet for {market_ticker} — waiting", flush=True)
        time.sleep(backoff)
        backoff = min(backoff + 1, 10)


def do_one_test_bet(client: KalshiClient):
    if not STARTUP_TEST_BET:
        return

    ticker = STARTUP_TEST_BET_TICKER or compute_target_market_ticker()
    print(f"[{datetime.now(timezone.utc).isoformat()}] STARTUP_TEST_BET enabled. target={ticker}", flush=True)

    side, price_cents, _ob, yask, nask = wait_for_open_and_get_side_price(
        client,
        ticker,
        prefer_side=STARTUP_TEST_BET_SIDE,
    )

    count = calc_contract_count(price_cents)
    est_cost = (count * price_cents) / 100.0

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] STARTUP TEST -> market={ticker} yes_ask={yask} no_ask={nask} "
        f"BET {side.upper()} price={price_cents}c x{count} (est_cost=${est_cost:.2f}) DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if DRY_RUN:
        print("🧪 DRY_RUN=true (startup test bet not placed)", flush=True)
        return

    resp = client.create_order_fok_buy(ticker, side=side, price_cents=price_cents, count=count)
    order = resp.get("order", {}) if isinstance(resp, dict) else {}
    print(f"✅ STARTUP TEST order response: {order}", flush=True)


# ==========================================================
# MAIN LOOP
# ==========================================================
def utc_day() -> datetime.date:
    return datetime.now(timezone.utc).date()


def main():
    print("=== BOT STARTED ===", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BASE_URL={BASE_URL}", flush=True)

    # Debug presence (no secret leakage)
    print("ENV_HAS_KALSHI_API_KEY_ID =", bool(_env("KALSHI_API_KEY_ID") or _env("KALSHI_API_KEYID")), flush=True)
    print("ENV_HAS_PEM_TEXT          =", bool(_env("KALSHI_PRIVATE_KEY_PEM")), flush=True)
    print("ENV_HAS_PEM_B64           =", bool(_env("KALSHI_PRIVATE_KEY_PEM_B64")), flush=True)

    print(f"[{datetime.now(timezone.utc).isoformat()}] POLL_SECONDS={POLL_SECONDS} MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MAX_DAILY_LOSS=${MAX_DAILY_LOSS} BET_DOLLARS=${BET_DOLLARS} DRY_RUN={DRY_RUN}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MARKET_TICKER_OVERRIDE={MARKET_TICKER_OVERRIDE or '(auto)'} SERIES_PREFIX={SERIES_PREFIX}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] ET_UTC_OFFSET_HOURS={ET_UTC_OFFSET_HOURS}", flush=True)

    print(f"[{datetime.now(timezone.utc).isoformat()}] WAIT_FOR_MARKET_OPEN={WAIT_FOR_MARKET_OPEN} MARKET_OPEN_GRACE_SECONDS={MARKET_OPEN_GRACE_SECONDS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] STARTUP_TEST_BET={STARTUP_TEST_BET} STARTUP_TEST_BET_SIDE={STARTUP_TEST_BET_SIDE} STARTUP_TEST_BET_TICKER={STARTUP_TEST_BET_TICKER or '(auto)'}", flush=True)

    client = KalshiClient(base_url=BASE_URL, key_id=KALSHI_API_KEY_ID)

    # --- One-time startup test bet (optional) ---
    try:
        do_one_test_bet(client)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).isoformat()}] ⚠️ STARTUP TEST skipped/failed: {e}", flush=True)

    current_day = utc_day()
    daily_pnl = 0.0  # placeholder guard
    last_market_refresh = 0.0
    market_ticker = compute_target_market_ticker()
    last_wagered_market: Optional[str] = None
    wagers_done = 0
    backoff_seconds = 1

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

            # compute / refresh ticker
            now = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now

            # avoid double-wagering same market
            if last_wagered_market == market_ticker:
                print(f"[{datetime.now(timezone.utc).isoformat()}] Already wagered market: {market_ticker}", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            # --- Market open trigger (liquidity gate) ---
            if WAIT_FOR_MARKET_OPEN:
                side, price_cents, _ob, best_yes_ask, best_no_ask = wait_for_open_and_get_side_price(
                    client,
                    market_ticker,
                    prefer_side="auto",
                )
            else:
                ob = client.get_orderbook(market_ticker, depth=1)
                best_yes_ask, best_no_ask = extract_top_asks(ob)

                if not market_has_liquidity(best_yes_ask, best_no_ask):
                    print(f"[{datetime.now(timezone.utc).isoformat()}] No usable orderbook yet for {market_ticker}", flush=True)
                    time.sleep(POLL_SECONDS)
                    continue

                if best_yes_ask and not best_no_ask:
                    side, price_cents = "yes", get_price_cents(best_yes_ask)
                elif best_no_ask and not best_yes_ask:
                    side, price_cents = "no", get_price_cents(best_no_ask)
                else:
                    choice = choose_majority_side(best_yes_ask, best_no_ask)
                    if not choice:
                        print(f"[{datetime.now(timezone.utc).isoformat()}] Liquidity present but no usable prices yet for {market_ticker}", flush=True)
                        time.sleep(POLL_SECONDS)
                        continue
                    side, price_cents = choice

            # size bet
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
                status = order.get("status")
                fill_count = order.get("fill_count")
                maker_cost = order.get("maker_fill_cost_dollars") or order.get("maker_fill_cost")
                taker_cost = order.get("taker_fill_cost_dollars") or order.get("taker_fill_cost")

                print(f"✅ Order submitted. status={status} fill_count={fill_count} maker_cost={maker_cost} taker_cost={taker_cost}", flush=True)

                # mark as wagered even if FOK cancels to avoid spam
                last_wagered_market = market_ticker
                wagers_done += 1

            if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                print(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.", flush=True)
                return

            # normal sleep
            backoff_seconds = 1
            time.sleep(POLL_SECONDS)

        except Exception as
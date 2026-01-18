import os
import time
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import requests

# --- Requires cryptography ---
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# =========================
# CONFIG (Render env vars)
# =========================
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# You create an API key on Kalshi (API Keys page). The docs call this "Your API key ID".
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()

# This is your PRIVATE KEY PEM (multi-line). In Render, store it as a Secret and preserve newlines.
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "").strip()

# Strategy / risk
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MARKET_REFRESH_SECONDS = int(os.getenv("MARKET_REFRESH_SECONDS", "60"))  # how often to re-compute the 15m ticker
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))

# Wagering
BET_DOLLARS = float(os.getenv("BET_DOLLARS", "1"))  # "1 dollar test wagers"
MIN_CONTRACTS = int(os.getenv("MIN_CONTRACTS", "1"))  # always buy at least 1 contract if possible

# If provided, bot will ONLY trade this exact market ticker (example: KXBTC15M-26JAN172000).
MARKET_TICKER_OVERRIDE = os.getenv("MARKET_TICKER", "").strip()

# Otherwise bot will generate tickers using this prefix (Kalshi BTC 15m uses KXBTC15M-YYMMMDDHHMM).
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()

# When to target: END time of the 15-min window in ET (matches Kalshi UI tickers you pasted).
# Example: at 7:57 PM ET, next quarter-hour end is 8:00 PM -> KXBTC15M-26JAN172000
USE_NEXT_QUARTER_HOUR_END = os.getenv("USE_NEXT_QUARTER_HOUR_END", "true").lower() in ("1", "true", "yes", "y")

# Safety: if true, prints what it WOULD do but does not place orders.
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes", "y")

# Optional: stop after N successful wagers (useful for testing)
STOP_AFTER_WAGERS = int(os.getenv("STOP_AFTER_WAGERS", "0"))  # 0 = no limit


# =========================
# Simple ET helpers (no pytz needed)
# =========================
# Eastern time is UTC-5 or UTC-4 depending on DST; to avoid extra deps, we approximate using US DST rules is messy.
# For this bot, simplest reliable approach: use Kalshi's ticker you paste as override.
# If you want auto-ticker generation without dependencies, we assume ET = UTC-5.
# If you want DST-correct, set MARKET_TICKER override (recommended).
ET_UTC_OFFSET_HOURS = int(os.getenv("ET_UTC_OFFSET_HOURS", "-5"))  # set to -4 during DST if you want


def now_et_naive() -> datetime:
    """Naive datetime representing 'ET' using a fixed offset you control via env var."""
    return (datetime.now(timezone.utc) + timedelta(hours=ET_UTC_OFFSET_HOURS)).replace(tzinfo=None)


def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    """
    Kalshi format you showed: KXBTC15M-26JAN172000
    -> prefix - YY + MON(uppercase) + DD + HHMM
    """
    yy = dt_et.strftime("%y")
    mon = dt_et.strftime("%b").upper()
    dd = dt_et.strftime("%d")
    hhmm = dt_et.strftime("%H%M")
    return f"{SERIES_PREFIX}-{yy}{mon}{dd}{hhmm}"


def next_quarter_hour_end(dt_et: datetime) -> datetime:
    """Return next quarter-hour time (minute in {00,15,30,45}) as the END timestamp."""
    minute = dt_et.minute
    # next quarter boundary
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        # roll to next hour
        dt2 = dt_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        dt2 = dt_et.replace(minute=next_q, second=0, microsecond=0)
    return dt2


def compute_target_market_ticker() -> str:
    if MARKET_TICKER_OVERRIDE:
        return MARKET_TICKER_OVERRIDE

    dt_et = now_et_naive()
    if USE_NEXT_QUARTER_HOUR_END:
        dt_et = next_quarter_hour_end(dt_et)
    else:
        # current quarter end (not recommended)
        dt_et = dt_et.replace(second=0, microsecond=0)

    return format_kalshi_btc15m_ticker(dt_et)


# =========================
# Kalshi signing (RSA-PSS)
# =========================
def load_private_key(pem_text: str):
    if not pem_text:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM env var.")
    # Render often stores multiline secrets correctly; but if pasted with literal \n, convert.
    pem_text = pem_text.replace("\\n", "\n").encode("utf-8")
    return serialization.load_pem_private_key(pem_text, password=None)


def make_signature(private_key, timestamp_ms: str, method: str, path_with_query: str, body: str) -> str:
    """
    Kalshi signature signs: timestamp + method + path + body
    (Kalshi docs: authenticated requests use KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP)
    """
    payload = (timestamp_ms + method.upper() + path_with_query + body).encode("utf-8")
    sig = private_key.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    # base64
    import base64
    return base64.b64encode(sig).decode("utf-8")


@dataclass
class KalshiClient:
    base_url: str
    key_id: str
    private_key_pem: str

    def __post_init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})
        self.private_key = load_private_key(self.private_key_pem)

        if not self.key_id:
            raise RuntimeError("Missing KALSHI_API_KEY_ID env var.")

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"

        body = ""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

        # include query in signature if present
        path_with_query = path
        if params:
            # requests will encode; we must build the same canonical query string
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
        # Docs: GET /markets/{ticker}/orderbook
        return self._request("GET", f"/markets/{market_ticker}/orderbook", params={"depth": depth})

    def create_order_fok_buy(self, market_ticker: str, side: str, price_cents: int, count: int) -> Dict[str, Any]:
        """
        Docs: POST /portfolio/orders
        Required: ticker, side (yes/no), action (buy), type (limit or market), plus price fields.
        We use limit + fill_or_kill for "test wager now".
        """
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
def extract_top_of_book(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Kalshi orderbook returns both fp and non-fp depending on endpoint version.
    We'll support:
      - orderbook_fp.yes_dollars / no_dollars : [[price_str, size_str], ...]
      - yes.bids / yes.asks etc (fallback)
    We need best ASK to buy.
    """
    fp = ob.get("orderbook_fp") or {}

    # Prefer fp if present
    yes_asks = fp.get("yes_dollars_asks") or fp.get("yes_dollars", None)
    no_asks = fp.get("no_dollars_asks") or fp.get("no_dollars", None)

    # Some payloads have separate asks/bids under fp; some don't. If missing, try non-fp structure.
    # Non-fp sometimes: ob["yes"]["asks"] = [{"price": 52, "size": 10}, ...]
    if not yes_asks or not no_asks:
        yes = ob.get("yes", {})
        no = ob.get("no", {})
        yes_asks_nf = yes.get("asks", [])
        no_asks_nf = no.get("asks", [])
        best_yes_ask = yes_asks_nf[0] if yes_asks_nf else None
        best_no_ask = no_asks_nf[0] if no_asks_nf else None
        return best_yes_ask, best_no_ask

    # fp format often looks like list of [price_str, size_str]
    # We take first element as best level.
    best_yes = yes_asks[0] if isinstance(yes_asks, list) and yes_asks else None
    best_no = no_asks[0] if isinstance(no_asks, list) and no_asks else None

    def normalize(level):
        if level is None:
            return None
        if isinstance(level, list) and len(level) >= 2:
            return {"price_dollars": level[0], "size": level[1]}
        if isinstance(level, dict):
            return level
        return None

    return normalize(best_yes), normalize(best_no)


def dollars_str_to_cents(price_dollars: str) -> int:
    # "0.5600" -> 56
    val = float(price_dollars)
    cents = int(round(val * 100))
    # clamp to 1..99
    return max(1, min(99, cents))


# =========================
# Strategy: "bet with majority"
# =========================
def choose_majority_side(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    """
    Pick the side with higher implied probability (more expensive to buy).
    We need the ASK to buy.
    Returns (side, price_cents_to_buy) or None if no book.
    """
    if not best_yes_ask or not best_no_ask:
        return None

    # fp path: {"price_dollars":"0.56"} ; non-fp path: {"price":56}
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

    # "Majority" = higher price (market thinks more likely)
    if yes_cents >= no_cents:
        return ("yes", yes_cents)
    else:
        return ("no", no_cents)


def calc_contract_count(price_cents: int) -> int:
    """
    Spend up to BET_DOLLARS, in whole contracts.
    Each contract costs price_cents (max loss per contract on buy).
    """
    budget_cents = int(round(BET_DOLLARS * 100))
    if price_cents <= 0:
        return 0
    count = budget_cents // price_cents
    if count < MIN_CONTRACTS:
        count = MIN_CONTRACTS
    # Don't exceed budget (if MIN_CONTRACTS forces spend > budget, still allow 1 contract)
    return int(count)


# =========================
# MAIN LOOP
# =========================
def utc_day() -> datetime.date:
    return datetime.now(timezone.utc).date()


def main():
    print("=== BOT STARTED ===", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] BASE_URL={BASE_URL}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] POLL_SECONDS={POLL_SECONDS} MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MAX_DAILY_LOSS=${MAX_DAILY_LOSS} BET_DOLLARS=${BET_DOLLARS} DRY_RUN={DRY_RUN}", flush=True)
    print(f"[{datetime.now(timezone.utc).isoformat()}] MARKET_TICKER_OVERRIDE={MARKET_TICKER_OVERRIDE or '(auto)'} SERIES_PREFIX={SERIES_PREFIX}", flush=True)

    client = KalshiClient(
        base_url=BASE_URL,
        key_id=KALSHI_API_KEY_ID,
        private_key_pem=KALSHI_PRIVATE_KEY_PEM,
    )

    current_day = utc_day()
    daily_pnl = 0.0  # NOTE: simple placeholder; true PnL should come from fills/settlements
    last_market_refresh = 0.0
    market_ticker = compute_target_market_ticker()
    last_wagered_market: Optional[str] = None
    wagers_done = 0

    backoff_seconds = 1

    while True:
        try:
            # Reset daily stats at UTC midnight
            if utc_day() != current_day:
                print("🔄 New UTC day — resetting daily PnL guard", flush=True)
                current_day = utc_day()
                daily_pnl = 0.0
                last_wagered_market = None

            # Hard stop if loss limit hit (guard only; real PnL needs fills)
            if daily_pnl <= -MAX_DAILY_LOSS:
                print("🛑 DAILY LOSS LIMIT HIT — sleeping until reset", flush=True)
                time.sleep(60)
                continue

            # Refresh which 15-min market we should trade
            now = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now

            # Avoid double-wagering same market
            if last_wagered_market == market_ticker:
                print(f"[{datetime.now(timezone.utc).isoformat()}] Already wagered this market: {market_ticker}", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            # Pull top-of-book
            ob = client.get_orderbook(market_ticker, depth=1)
            best_yes_ask, best_no_ask = extract_top_of_book(ob)

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
                f"top_yes_ask={best_yes_ask} top_no_ask={best_no_ask} -> "
                f"BET {side.upper()} price={price_cents}c x{count} (est_cost=${est_cost:.2f})",
                flush=True,
            )

            if DRY_RUN:
                last_wagered_market = market_ticker
                wagers_done += 1
                print("🧪 DRY_RUN=true (not placing order)", flush=True)
            else:
                # Place the test wager (FOK limit buy)
                resp = client.create_order_fok_buy(market_ticker, side=side, price_cents=price_cents, count=count)
                order = resp.get("order", {})
                status = order.get("status")
                fill_count = order.get("fill_count")
                maker_cost = order.get("maker_fill_cost_dollars") or order.get("maker_fill_cost")
                taker_cost = order.get("taker_fill_cost_dollars") or order.get("taker_fill_cost")

                print(f"✅ Order submitted. status={status} fill_count={fill_count} maker_cost={maker_cost} taker_cost={taker_cost}", flush=True)

                # Mark as wagered even if FOK cancels; prevents spamming
                last_wagered_market = market_ticker
                wagers_done += 1

            # Optional stop for testing
            if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                print(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.", flush=True)
                return

            # Reset backoff after a good cycle
            backoff_seconds = 1

            time.sleep(POLL_SECONDS)

        except Exception as e:
            # Rate limit handling
            if str(e) == "429_RATE_LIMIT":
                print(f"[{datetime.now(timezone.utc).isoformat()}] ⚠️ 429 rate limit. Backing off {backoff_seconds}s", flush=True)
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 120)
                continue

            print(f"[{datetime.now(timezone.utc).isoformat()}] ❌ ERROR: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
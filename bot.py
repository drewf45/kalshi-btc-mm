# bot.py
"""
Kalshi 15-minute BTC bot (Render-friendly)

Fixes:
- Uses KALSHI_API_BASE=https://api.elections.kalshi.com (per your logs)
- Robust RSA signature auth (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PEM_BASE64)
- Fetches quotes from ORDERBOOK (fixes yes/no None/None)
- Avoids instant email spam; sends once per day after DAILY_EMAIL_HOUR_ET (default 20 = 8pm ET)
- Adds CONFIRM_LIVE_TRADING gate so you can't accidentally go live
- "Farm wins" style starter market-maker scaffold: places 1 small bid on YES and 1 on NO per 15-min market
  (you can expand sizing later)

ENV VARS (Render -> Environment):
Required:
  KALSHI_API_BASE=https://api.elections.kalshi.com
  KALSHI_API_KEY_ID=...                (from Kalshi API key page)
  KALSHI_PRIVATE_KEY_PEM_BASE64=...    (BASE64 of the *PEM private key* used for signing)
  SERIES_PREFIX=KXBTC15M               (your market series)
Optional strategy:
  ENABLE_TRADING=true|false            (default false)
  CONFIRM_LIVE_TRADING=true|false      (default false)  <-- MUST be true to place real orders
  POLL_SECONDS=60
  ORDER_USD_PER_SIDE=1.00              (default 1.00)
  IMPROVE_TICKS=1                      (default 1) how many cents to improve the bid
  MAX_SPREAD_CENTS=20                  (default 20) skip if spread too wide
  MIN_LIQUIDITY_CENTS=1                (default 1) skip if no bid/ask exists
Email (optional):
  EMAIL_ENABLED=true|false
  DAILY_EMAIL_HOUR_ET=20
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD
  EMAIL_TO, EMAIL_FROM
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any, Dict, Optional, Tuple, List

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc-mm")


# -------------------------
# Time helpers
# -------------------------
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def et_now() -> dt.datetime:
    # Simple ET approximation: UTC-5 (no DST handling).
    # Good enough for daily email + choosing "next market" for your use case.
    return utc_now().astimezone(dt.timezone(dt.timedelta(hours=-5)))

def iso_date_et() -> str:
    return et_now().date().isoformat()


# -------------------------
# Config
# -------------------------
@dataclasses.dataclass
class Config:
    api_base: str
    api_key_id: str
    private_key_pem_b64: str

    series_prefix: str
    poll_seconds: int

    enable_trading: bool
    confirm_live_trading: bool

    order_usd_per_side: float
    improve_ticks: int
    max_spread_cents: int
    min_liquidity_cents: int

    email_enabled: bool
    daily_email_hour_et: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_to: str
    email_from: str


def getenv_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def load_config() -> Config:
    api_base = os.getenv("KALSHI_API_BASE", "").strip()
    if not api_base:
        raise RuntimeError("Missing KALSHI_API_BASE")
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    if not api_key_id:
        raise RuntimeError("Missing KALSHI_API_KEY_ID")
    pk_b64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()
    if not pk_b64:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")

    series_prefix = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
    poll_seconds = int(os.getenv("POLL_SECONDS", "60"))

    enable_trading = getenv_bool("ENABLE_TRADING", False)
    confirm_live_trading = getenv_bool("CONFIRM_LIVE_TRADING", False)

    order_usd_per_side = float(os.getenv("ORDER_USD_PER_SIDE", "1.00"))
    improve_ticks = int(os.getenv("IMPROVE_TICKS", "1"))
    max_spread_cents = int(os.getenv("MAX_SPREAD_CENTS", "20"))
    min_liquidity_cents = int(os.getenv("MIN_LIQUIDITY_CENTS", "1"))

    email_enabled = getenv_bool("EMAIL_ENABLED", False)
    daily_email_hour_et = int(os.getenv("DAILY_EMAIL_HOUR_ET", "20"))
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    email_to = os.getenv("EMAIL_TO", "").strip()
    email_from = os.getenv("EMAIL_FROM", "").strip()

    return Config(
        api_base=api_base.rstrip("/"),
        api_key_id=api_key_id,
        private_key_pem_b64=pk_b64,
        series_prefix=series_prefix,
        poll_seconds=poll_seconds,
        enable_trading=enable_trading,
        confirm_live_trading=confirm_live_trading,
        order_usd_per_side=order_usd_per_side,
        improve_ticks=improve_ticks,
        max_spread_cents=max_spread_cents,
        min_liquidity_cents=min_liquidity_cents,
        email_enabled=email_enabled,
        daily_email_hour_et=daily_email_hour_et,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        email_to=email_to,
        email_from=email_from,
    )


# -------------------------
# State (simple JSON file)
# -------------------------
STATE_PATH = "state.json"

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {
            "last_email_date": None,
            "daily": {
                "date": iso_date_et(),
                "orders_placed": 0,
                "orders_failed": 0,
                "realized_pnl_cents": 0,  # placeholder; true realized pnl requires fills endpoint parsing
            },
            "seen_markets": {},
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_email_date": None,
            "daily": {
                "date": iso_date_et(),
                "orders_placed": 0,
                "orders_failed": 0,
                "realized_pnl_cents": 0,
            },
            "seen_markets": {},
        }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def rollover_daily(state: Dict[str, Any]) -> None:
    today = iso_date_et()
    if state.get("daily", {}).get("date") != today:
        state["daily"] = {
            "date": today,
            "orders_placed": 0,
            "orders_failed": 0,
            "realized_pnl_cents": 0,
        }


# -------------------------
# Email
# -------------------------
def send_email(cfg: Config, subject: str, body: str) -> None:
    if not cfg.email_enabled:
        return
    # Require complete SMTP config
    needed = [cfg.smtp_host, cfg.smtp_username, cfg.smtp_password, cfg.email_to, cfg.email_from]
    if any(not x for x in needed):
        log.warning("EMAIL_ENABLED=true but SMTP/EMAIL_* vars incomplete. Skipping email.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg.email_from
    msg["To"] = cfg.email_to

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as server:
        server.starttls()
        server.login(cfg.smtp_username, cfg.smtp_password)
        server.sendmail(cfg.email_from, [cfg.email_to], msg.as_string())

def maybe_send_daily_email(cfg: Config, state: Dict[str, Any]) -> None:
    if not cfg.email_enabled:
        return

    now = et_now()
    today = iso_date_et()
    if now.hour < cfg.daily_email_hour_et:
        return
    if state.get("last_email_date") == today:
        return

    pnl = (state["daily"]["realized_pnl_cents"] / 100.0) if state.get("daily") else 0.0
    body = f"""Kalshi Bot Daily Report ({today})

Orders placed: {state["daily"]["orders_placed"]}
Orders failed : {state["daily"]["orders_failed"]}
Realized P&L  : ${pnl:,.2f}

Note:
- Realized P&L is a placeholder unless we wire fills/trades parsing.
"""
    send_email(cfg, f"Kalshi Bot Daily P&L — {today}", body)
    state["last_email_date"] = today
    save_state(state)
    log.info("Daily email sent.")


# -------------------------
# Kalshi API Client (RSA signature)
# -------------------------
class KalshiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self._private_key = self._load_private_key(cfg.private_key_pem_b64)

    @staticmethod
    def _load_private_key(pem_b64: str):
        pem_bytes = base64.b64decode(pem_b64.encode("utf-8"))
        return serialization.load_pem_private_key(pem_bytes, password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str, body: str) -> str:
        """
        Signature scheme used in earlier working version:
        message = timestamp_ms + method + path + body
        RSA-SHA256, then base64
        """
        msg = (timestamp_ms + method.upper() + path + body).encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        sig = self._sign(ts_ms, method, path, body)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Header names matching the working approach used in your earlier runs:
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    def request(self, method: str, path: str, json_body: Optional[dict] = None) -> Any:
        url = f"{self.cfg.api_base}{path}"
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))
        headers = self._headers(method, path, body)

        resp = self.session.request(method, url, data=None if json_body is None else body, headers=headers, timeout=20)

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")

        if resp.text.strip() == "":
            return None
        return resp.json()

    def get(self, path: str) -> Any:
        return self.request("GET", path, None)

    def post(self, path: str, json_body: dict) -> Any:
        return self.request("POST", path, json_body)


# -------------------------
# Market utilities
# -------------------------
def list_open_markets(kc: KalshiClient, series_prefix: str, limit: int = 100) -> List[dict]:
    # Try common params; tolerate API shape differences.
    # Many Kalshi endpoints are under /trade-api/v2
    path = f"/trade-api/v2/markets?series_ticker={series_prefix}&status=active&limit={limit}"
    data = kc.get(path)
    # Typical: {"markets":[...]}
    if isinstance(data, dict) and "markets" in data:
        return data["markets"] or []
    # Fallback: sometimes returns list
    if isinstance(data, list):
        return data
    return []

def pick_next_market(markets: List[dict]) -> Optional[dict]:
    """
    Pick the next market by earliest close time in the future if available,
    otherwise just the first active.
    """
    now = utc_now()
    def parse_time(s: str) -> Optional[dt.datetime]:
        try:
            # accept ISO
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return t.astimezone(dt.timezone.utc)
        except Exception:
            return None

    candidates = []
    for m in markets:
        ct = m.get("close_time") or m.get("close_ts") or m.get("closeTime")
        t = parse_time(ct) if isinstance(ct, str) else None
        candidates.append((t, m))

    # prefer future close times
    future = [(t, m) for (t, m) in candidates if t is not None and t > now]
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]

    # fallback stable order
    return markets[0] if markets else None

def fetch_market(kc: KalshiClient, ticker: str) -> dict:
    return kc.get(f"/trade-api/v2/markets/{ticker}")

def fetch_orderbook(kc: KalshiClient, ticker: str) -> dict:
    """
    Robust: try a few known orderbook paths (Kalshi has changed these before).
    """
    paths = [
        f"/trade-api/v2/markets/{ticker}/orderbook",
        f"/trade-api/v2/markets/{ticker}/order-book",
        f"/trade-api/v2/markets/{ticker}/book",
    ]
    last_err = None
    for p in paths:
        try:
            return kc.get(p)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not fetch orderbook for {ticker}: {last_err}")

def _best_prices(side_book: dict) -> Tuple[Optional[int], Optional[int]]:
    bids = side_book.get("bids", []) or []
    asks = side_book.get("asks", []) or []
    best_bid = max((int(x["price"]) for x in bids if isinstance(x, dict) and "price" in x), default=None)
    best_ask = min((int(x["price"]) for x in asks if isinstance(x, dict) and "price" in x), default=None)
    return best_bid, best_ask

def get_quotes(kc: KalshiClient, ticker: str) -> Dict[str, Any]:
    ob = fetch_orderbook(kc, ticker)

    # Common shapes:
    # 1) {"yes": {...}, "no": {...}}
    # 2) {"orderbook": {"yes": {...}, "no": {...}}}
    if isinstance(ob, dict) and "orderbook" in ob and isinstance(ob["orderbook"], dict):
        ob = ob["orderbook"]

    yes_book = (ob.get("yes") or {}) if isinstance(ob, dict) else {}
    no_book = (ob.get("no") or {}) if isinstance(ob, dict) else {}

    yes_bid, yes_ask = _best_prices(yes_book)
    no_bid, no_ask = _best_prices(no_book)

    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask, "raw": ob}


# -------------------------
# Trading primitives
# -------------------------
def cents_from_usd(usd: float) -> int:
    return int(round(usd * 100))

def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))

def compute_size_for_budget(budget_cents: int, price_cents: int) -> int:
    """
    Kalshi prices are in cents per contract (0..100).
    Buying 1 contract costs price_cents.
    With budget_cents, size = floor(budget / price).
    If price is 0 (shouldn't happen), return 0.
    """
    if price_cents <= 0:
        return 0
    return budget_cents // price_cents

def place_limit_buy(
    kc: KalshiClient,
    ticker: str,
    side: str,             # "yes" or "no"
    price_cents: int,      # 1..99
    count: int,            # contracts
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Order endpoint is commonly /trade-api/v2/orders.
    Payload shape can vary slightly; this matches typical v2 style.

    NOTE: If your account uses a slightly different field name (e.g., "action", "side", "type"),
    the error will show in logs and we’ll adjust once—no more goose chase.
    """
    payload = {
        "market_ticker": ticker,
        "side": side,            # yes/no
        "type": "limit",
        "action": "buy",
        "price": int(price_cents),
        "count": int(count),
        "time_in_force": "gtc",
    }

    if dry_run:
        return {"dry_run": True, "payload": payload}

    return kc.post("/trade-api/v2/orders", payload)


# -------------------------
# Strategy: simple "farm wins" starter MM
# -------------------------
def should_trade_market(cfg: Config, q: Dict[str, Any]) -> bool:
    # Need at least some liquidity
    for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        if q.get(k) is None:
            return False

    yes_bid, yes_ask = int(q["yes_bid"]), int(q["yes_ask"])
    no_bid, no_ask = int(q["no_bid"]), int(q["no_ask"])

    # Basic sanity
    if yes_bid < cfg.min_liquidity_cents or yes_ask < cfg.min_liquidity_cents:
        return False
    if no_bid < cfg.min_liquidity_cents or no_ask < cfg.min_liquidity_cents:
        return False

    # Skip if spreads too wide
    if (yes_ask - yes_bid) > cfg.max_spread_cents:
        return False
    if (no_ask - no_bid) > cfg.max_spread_cents:
        return False

    return True

def compute_entry_prices(cfg: Config, q: Dict[str, Any]) -> Dict[str, int]:
    """
    We "improve" the bid by IMPROVE_TICKS but don't cross the ask.
    This is market-maker-ish: try to get filled at slightly better than current bid.
    """
    yes_bid, yes_ask = int(q["yes_bid"]), int(q["yes_ask"])
    no_bid, no_ask = int(q["no_bid"]), int(q["no_ask"])

    yes_px = clamp_int(yes_bid + cfg.improve_ticks, 1, 99)
    no_px = clamp_int(no_bid + cfg.improve_ticks, 1, 99)

    # Don't cross
    if yes_px >= yes_ask:
        yes_px = max(1, yes_ask - 1)
    if no_px >= no_ask:
        no_px = max(1, no_ask - 1)

    return {"yes_px": yes_px, "no_px": no_px}


# -------------------------
# Auth check / balance
# -------------------------
def auth_check(kc: KalshiClient) -> Optional[dict]:
    """
    Your logs show "Auth check OK (portfolio/balance)."
    We'll call a common balance endpoint:
    """
    # Try a few. First that works is used.
    paths = [
        "/trade-api/v2/portfolio/balance",
        "/trade-api/v2/portfolio",
        "/trade-api/v2/balance",
    ]
    last_err = None
    for p in paths:
        try:
            return kc.get(p)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Auth check failed: {last_err}")

def extract_cash_balance_cents(balance_obj: Any) -> Optional[int]:
    """
    Best-effort extraction across possible JSON shapes.
    """
    if not isinstance(balance_obj, dict):
        return None

    # Common fields
    for key in ("cash_balance", "cash", "available_cash", "availableCash"):
        v = balance_obj.get(key)
        if isinstance(v, (int, float)):
            # assume dollars if float >= 1 and not cents
            if isinstance(v, float):
                return cents_from_usd(v)
            return int(v)

    # Nested patterns
    if "portfolio" in balance_obj and isinstance(balance_obj["portfolio"], dict):
        p = balance_obj["portfolio"]
        for key in ("cash_balance", "cash", "available_cash"):
            v = p.get(key)
            if isinstance(v, (int, float)):
                if isinstance(v, float):
                    return cents_from_usd(v)
                return int(v)

    return None


# -------------------------
# Main loop
# -------------------------
def main() -> None:
    cfg = load_config()

    if "trading-api.kalshi.com" in cfg.api_base:
        log.warning("KALSHI_API_BASE looks unusual: %s (you want https://api.elections.kalshi.com)", cfg.api_base)

    state = load_state()
    rollover_daily(state)
    save_state(state)

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", cfg.enable_trading)
    log.info("CONFIRM_LIVE_TRADING=%s", cfg.confirm_live_trading)
    log.info("POLL_SECONDS=%s", cfg.poll_seconds)
    log.info("SERIES_PREFIX=%s", cfg.series_prefix)
    log.info("API_BASE=%s", cfg.api_base)

    kc = KalshiClient(cfg)

    # Auth check
    bal = auth_check(kc)
    cash_cents = extract_cash_balance_cents(bal)
    log.info("Auth check OK (portfolio/balance). Cash=%s", f"${cash_cents/100:.2f}" if cash_cents is not None else "unknown")

    while True:
        start = time.time()
        rollover_daily(state)

        try:
            maybe_send_daily_email(cfg, state)

            markets = list_open_markets(kc, cfg.series_prefix, limit=100)
            log.info("Open markets returned: %s", len(markets))
            m = pick_next_market(markets)
            if not m:
                log.warning("No active markets found for series=%s", cfg.series_prefix)
                time.sleep(cfg.poll_seconds)
                continue

            ticker = m.get("ticker") or m.get("market_ticker") or m.get("id")
            if not ticker:
                log.warning("Market missing ticker field: %s", str(m)[:500])
                time.sleep(cfg.poll_seconds)
                continue

            q = get_quotes(kc, ticker)
            yes_bid, yes_ask = q["yes_bid"], q["yes_ask"]
            no_bid, no_ask = q["no_bid"], q["no_ask"]

            log.info(
                "Heartbeat ET now=%s | market=%s | yes %s/%s no %s/%s",
                et_now().strftime("%Y-%m-%d %H:%M:%S"),
                ticker,
                str(yes_bid), str(yes_ask),
                str(no_bid), str(no_ask),
            )

            if any(v is None for v in (yes_bid, yes_ask, no_bid, no_ask)):
                log.warning("No quotes available for one or both sides; skipping this tick.")
                time.sleep(cfg.poll_seconds)
                continue

            if not should_trade_market(cfg, q):
                log.info("Skipping market due to liquidity/spread constraints.")
                time.sleep(cfg.poll_seconds)
                continue

            # Strategy: one small limit buy on BOTH sides per new market window (or per market change)
            prices = compute_entry_prices(cfg, q)
            yes_px, no_px = prices["yes_px"], prices["no_px"]

            # Only place once per market per day unless you change it
            # (prevents spam while you're testing)
            today = iso_date_et()
            seen = state.setdefault("seen_markets", {})
            seen_key = f"{today}:{ticker}"
            already = seen.get(seen_key, {}).get("placed", False)

            if cfg.enable_trading and not already:
                if not cfg.confirm_live_trading:
                    log.warning("ENABLE_TRADING=True but CONFIRM_LIVE_TRADING=False. Dry-run only.")
                    dry_run = True
                else:
                    dry_run = False

                budget_cents = cents_from_usd(cfg.order_usd_per_side)

                yes_size = compute_size_for_budget(budget_cents, yes_px)
                no_size = compute_size_for_budget(budget_cents, no_px)

                # Ensure at least 1 contract if budget allows
                if yes_size <= 0 or no_size <= 0:
                    log.info("Budget too small for current prices. yes_size=%s no_size=%s", yes_size, no_size)
                else:
                    # Place YES buy
                    try:
                        r1 = place_limit_buy(kc, ticker, "yes", yes_px, yes_size, dry_run=dry_run)
                        state["daily"]["orders_placed"] += 1
                        log.info("Placed YES buy: price=%sc size=%s dry_run=%s", yes_px, yes_size, dry_run)
                    except Exception as e:
                        state["daily"]["orders_failed"] += 1
                        log.warning("YES order failed: %s", e)

                    # Place NO buy
                    try:
                        r2 = place_limit_buy(kc, ticker, "no", no_px, no_size, dry_run=dry_run)
                        state["daily"]["orders_placed"] += 1
                        log.info("Placed NO buy: price=%sc size=%s dry_run=%s", no_px, no_size, dry_run)
                    except Exception as e:
                        state["daily"]["orders_failed"] += 1
                        log.warning("NO order failed: %s", e)

                    seen[seen_key] = {"placed": True, "yes_px": yes_px, "no_px": no_px, "ts": int(time.time())}
                    save_state(state)

            # sleep
            elapsed = time.time() - start
            log.info("Loop complete in %.2fs; sleeping %ss", elapsed, cfg.poll_seconds)
            time.sleep(cfg.poll_seconds)

        except Exception as e:
            log.error("LOOP ERROR: %r", e)
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
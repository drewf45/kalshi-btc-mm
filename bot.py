# bot.py
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-btc-mm")


# -------------------------
# Time helpers
# -------------------------
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def et_now() -> dt.datetime:
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
    api_base = os.getenv("KALSHI_API_BASE", "").strip().rstrip("/")
    if not api_base:
        raise RuntimeError("Missing KALSHI_API_BASE")

    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    if not api_key_id:
        raise RuntimeError("Missing KALSHI_API_KEY_ID")

    pk_b64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()
    if not pk_b64:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")

    return Config(
        api_base=api_base,
        api_key_id=api_key_id,
        private_key_pem_b64=pk_b64,
        series_prefix=os.getenv("SERIES_PREFIX", "KXBTC15M").strip(),
        poll_seconds=int(os.getenv("POLL_SECONDS", "60")),
        enable_trading=getenv_bool("ENABLE_TRADING", False),
        confirm_live_trading=getenv_bool("CONFIRM_LIVE_TRADING", False),
        order_usd_per_side=float(os.getenv("ORDER_USD_PER_SIDE", "1.00")),
        improve_ticks=int(os.getenv("IMPROVE_TICKS", "1")),
        max_spread_cents=int(os.getenv("MAX_SPREAD_CENTS", "20")),
        min_liquidity_cents=int(os.getenv("MIN_LIQUIDITY_CENTS", "1")),
        email_enabled=getenv_bool("EMAIL_ENABLED", False),
        daily_email_hour_et=int(os.getenv("DAILY_EMAIL_HOUR_ET", "20")),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        email_to=os.getenv("EMAIL_TO", "").strip(),
        email_from=os.getenv("EMAIL_FROM", "").strip(),
    )


# -------------------------
# Simple state
# -------------------------
STATE_PATH = "state.json"

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {
            "last_email_date": None,
            "daily": {"date": iso_date_et(), "orders_placed": 0, "orders_failed": 0, "realized_pnl_cents": 0},
            "seen_markets": {},
            "api_prefix": None,  # discovered prefix stored here
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_email_date": None,
            "daily": {"date": iso_date_et(), "orders_placed": 0, "orders_failed": 0, "realized_pnl_cents": 0},
            "seen_markets": {},
            "api_prefix": None,
        }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def rollover_daily(state: Dict[str, Any]) -> None:
    today = iso_date_et()
    if state.get("daily", {}).get("date") != today:
        state["daily"] = {"date": today, "orders_placed": 0, "orders_failed": 0, "realized_pnl_cents": 0}


# -------------------------
# Email
# -------------------------
def send_email(cfg: Config, subject: str, body: str) -> None:
    if not cfg.email_enabled:
        return
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

    pnl = (state["daily"]["realized_pnl_cents"] / 100.0)
    body = f"""Kalshi Bot Daily Report ({today})

Orders placed: {state["daily"]["orders_placed"]}
Orders failed : {state["daily"]["orders_failed"]}
Realized P&L  : ${pnl:,.2f}

Note: Realized P&L is placeholder unless fills are parsed.
"""
    send_email(cfg, f"Kalshi Bot Daily P&L — {today}", body)
    state["last_email_date"] = today
    save_state(state)
    log.info("Daily email sent.")


# -------------------------
# Kalshi client (RSA signature)
# -------------------------
class KalshiClient:
    def __init__(self, cfg: Config, api_prefix: str):
        self.cfg = cfg
        self.api_prefix = api_prefix  # e.g. "/trade-api/v2"
        self.session = requests.Session()
        self._private_key = self._load_private_key(cfg.private_key_pem_b64)

    @staticmethod
    def _load_private_key(pem_b64: str):
        pem_bytes = base64.b64decode(pem_b64.encode("utf-8"))
        return serialization.load_pem_private_key(pem_bytes, password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str, body: str) -> str:
        # Signature message = ts + METHOD + path + body
        msg = (timestamp_ms + method.upper() + path + body).encode("utf-8")
        sig = self._private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        sig = self._sign(ts_ms, method, path, body)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
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
        if not resp.text.strip():
            return None
        return resp.json()

    def get(self, path: str) -> Any:
        return self.request("GET", path, None)

    def post(self, path: str, json_body: dict) -> Any:
        return self.request("POST", path, json_body)


# -------------------------
# Prefix discovery (FIX for your 404)
# -------------------------
def discover_api_prefix(cfg: Config) -> str:
    """
    The 'api.elections.kalshi.com' host can expose different path prefixes.
    Your 404 indicates we're calling a prefix that doesn't exist.
    We'll probe a small set and pick the first that doesn't 404.
    """
    # Known-ish candidates (we only need ONE that works)
    candidates = [
        "/trade-api/v2",
        "/trade-api/v1",
        "/trade-api",
        "/v2",
        "",  # sometimes direct
    ]

    s = requests.Session()

    # We'll probe with a super-safe public endpoint that usually exists:
    # markets list is often public.
    for pref in candidates:
        path = f"{pref}/markets?limit=1"
        url = f"{cfg.api_base}{path}"
        try:
            r = s.get(url, timeout=10)
            if r.status_code != 404:
                log.info("Discovered API prefix: %s (probe %s -> %s)", pref or "(none)", path, r.status_code)
                return pref
        except Exception:
            pass

    # If everything 404s, default to v2 and let auth error show.
    log.warning("Could not discover API prefix; defaulting to /trade-api/v2")
    return "/trade-api/v2"


# -------------------------
# API wrappers using discovered prefix
# -------------------------
def list_open_markets(kc: KalshiClient, series_prefix: str, limit: int = 100) -> List[dict]:
    path = f"{kc.api_prefix}/markets?series_ticker={series_prefix}&status=active&limit={limit}"
    data = kc.get(path)
    if isinstance(data, dict) and "markets" in data:
        return data["markets"] or []
    if isinstance(data, list):
        return data
    return []

def pick_next_market(markets: List[dict]) -> Optional[dict]:
    if not markets:
        return None
    now = utc_now()

    def parse_time(s: str) -> Optional[dt.datetime]:
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return t.astimezone(dt.timezone.utc)
        except Exception:
            return None

    scored = []
    for m in markets:
        ct = m.get("close_time") or m.get("close_ts") or m.get("closeTime")
        t = parse_time(ct) if isinstance(ct, str) else None
        scored.append((t, m))

    future = [(t, m) for (t, m) in scored if t is not None and t > now]
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]

    return markets[0]

def fetch_orderbook(kc: KalshiClient, ticker: str) -> dict:
    paths = [
        f"{kc.api_prefix}/markets/{ticker}/orderbook",
        f"{kc.api_prefix}/markets/{ticker}/order-book",
        f"{kc.api_prefix}/markets/{ticker}/book",
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
    if isinstance(ob, dict) and "orderbook" in ob and isinstance(ob["orderbook"], dict):
        ob = ob["orderbook"]

    yes_book = (ob.get("yes") or {}) if isinstance(ob, dict) else {}
    no_book = (ob.get("no") or {}) if isinstance(ob, dict) else {}

    yes_bid, yes_ask = _best_prices(yes_book)
    no_bid, no_ask = _best_prices(no_book)

    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}


def auth_check(kc: KalshiClient) -> Any:
    """
    We avoid hardcoding a single balance endpoint. Probe a few under the discovered prefix.
    """
    paths = [
        f"{kc.api_prefix}/portfolio/balance",
        f"{kc.api_prefix}/portfolio",
        f"{kc.api_prefix}/balance",
        f"{kc.api_prefix}/account/balance",
    ]
    last_err = None
    for p in paths:
        try:
            return kc.get(p)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Auth check failed: {last_err}")

def extract_cash_balance_cents(balance_obj: Any) -> Optional[int]:
    if not isinstance(balance_obj, dict):
        return None
    for key in ("cash_balance", "cash", "available_cash", "availableCash"):
        v = balance_obj.get(key)
        if isinstance(v, (int, float)):
            return int(round(v * 100)) if isinstance(v, float) else int(v)
    if "portfolio" in balance_obj and isinstance(balance_obj["portfolio"], dict):
        p = balance_obj["portfolio"]
        for key in ("cash_balance", "cash", "available_cash"):
            v = p.get(key)
            if isinstance(v, (int, float)):
                return int(round(v * 100)) if isinstance(v, float) else int(v)
    return None


# -------------------------
# Trading
# -------------------------
def cents_from_usd(usd: float) -> int:
    return int(round(usd * 100))

def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))

def compute_size_for_budget(budget_cents: int, price_cents: int) -> int:
    if price_cents <= 0:
        return 0
    return budget_cents // price_cents

def should_trade_market(cfg: Config, q: Dict[str, Any]) -> bool:
    for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        if q.get(k) is None:
            return False
    yes_bid, yes_ask = int(q["yes_bid"]), int(q["yes_ask"])
    no_bid, no_ask = int(q["no_bid"]), int(q["no_ask"])

    if min(yes_bid, yes_ask, no_bid, no_ask) < cfg.min_liquidity_cents:
        return False
    if (yes_ask - yes_bid) > cfg.max_spread_cents:
        return False
    if (no_ask - no_bid) > cfg.max_spread_cents:
        return False
    return True

def compute_entry_prices(cfg: Config, q: Dict[str, Any]) -> Dict[str, int]:
    yes_bid, yes_ask = int(q["yes_bid"]), int(q["yes_ask"])
    no_bid, no_ask = int(q["no_bid"]), int(q["no_ask"])

    yes_px = clamp_int(yes_bid + cfg.improve_ticks, 1, 99)
    no_px = clamp_int(no_bid + cfg.improve_ticks, 1, 99)

    if yes_px >= yes_ask:
        yes_px = max(1, yes_ask - 1)
    if no_px >= no_ask:
        no_px = max(1, no_ask - 1)

    return {"yes_px": yes_px, "no_px": no_px}

def place_limit_buy(kc: KalshiClient, ticker: str, side: str, price_cents: int, count: int) -> Dict[str, Any]:
    payload = {
        "market_ticker": ticker,
        "side": side,
        "type": "limit",
        "action": "buy",
        "price": int(price_cents),
        "count": int(count),
        "time_in_force": "gtc",
    }
    return kc.post(f"{kc.api_prefix}/orders", payload)


# -------------------------
# Main
# -------------------------
def main() -> None:
    cfg = load_config()
    state = load_state()
    rollover_daily(state)

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", cfg.enable_trading)
    log.info("CONFIRM_LIVE_TRADING=%s", cfg.confirm_live_trading)
    log.info("POLL_SECONDS=%s", cfg.poll_seconds)
    log.info("SERIES_PREFIX=%s", cfg.series_prefix)
    log.info("API_BASE=%s", cfg.api_base)

    # Discover prefix if missing
    if not state.get("api_prefix"):
        state["api_prefix"] = discover_api_prefix(cfg)
        save_state(state)

    kc = KalshiClient(cfg, api_prefix=state["api_prefix"])

    # Auth check
    bal = auth_check(kc)
    cash_cents = extract_cash_balance_cents(bal)
    log.info("Auth check OK. Cash=%s", f"${cash_cents/100:.2f}" if cash_cents is not None else "unknown")

    while True:
        start = time.time()
        rollover_daily(state)
        try:
            maybe_send_daily_email(cfg, state)

            markets = list_open_markets(kc, cfg.series_prefix, limit=100)
            log.info("Open markets returned: %s", len(markets))
            m = pick_next_market(markets)
            if not m:
                log.warning("No active markets found.")
                time.sleep(cfg.poll_seconds)
                continue

            ticker = m.get("ticker") or m.get("market_ticker") or m.get("id")
            if not ticker:
                log.warning("Market missing ticker field.")
                time.sleep(cfg.poll_seconds)
                continue

            q = get_quotes(kc, ticker)
            log.info(
                "Heartbeat ET now=%s | market=%s | yes %s/%s no %s/%s",
                et_now().strftime("%Y-%m-%d %H:%M:%S"),
                ticker,
                q["yes_bid"], q["yes_ask"],
                q["no_bid"], q["no_ask"],
            )

            if not should_trade_market(cfg, q):
                log.info("Skipping market (liquidity/spread/quotes).")
                time.sleep(cfg.poll_seconds)
                continue

            today = iso_date_et()
            seen_key = f"{today}:{ticker}"
            already = state.setdefault("seen_markets", {}).get(seen_key, {}).get("placed", False)

            if cfg.enable_trading and not already:
                if not cfg.confirm_live_trading:
                    log.warning("ENABLE_TRADING=True but CONFIRM_LIVE_TRADING=False. NOT placing orders.")
                else:
                    prices = compute_entry_prices(cfg, q)
                    yes_px, no_px = prices["yes_px"], prices["no_px"]
                    budget_cents = cents_from_usd(cfg.order_usd_per_side)

                    yes_size = compute_size_for_budget(budget_cents, yes_px)
                    no_size = compute_size_for_budget(budget_cents, no_px)

                    if yes_size <= 0 or no_size <= 0:
                        log.info("Budget too small for current prices. yes_size=%s no_size=%s", yes_size, no_size)
                    else:
                        try:
                            place_limit_buy(kc, ticker, "yes", yes_px, yes_size)
                            state["daily"]["orders_placed"] += 1
                            log.info("Placed YES buy: price=%sc size=%s", yes_px, yes_size)
                        except Exception as e:
                            state["daily"]["orders_failed"] += 1
                            log.warning("YES order failed: %s", e)

                        try:
                            place_limit_buy(kc, ticker, "no", no_px, no_size)
                            state["daily"]["orders_placed"] += 1
                            log.info("Placed NO buy: price=%sc size=%s", no_px, no_size)
                        except Exception as e:
                            state["daily"]["orders_failed"] += 1
                            log.warning("NO order failed: %s", e)

                        state["seen_markets"][seen_key] = {"placed": True, "ts": int(time.time())}
                        save_state(state)

            elapsed = time.time() - start
            log.info("Loop complete in %.2fs; sleeping %ss", elapsed, cfg.poll_seconds)
            time.sleep(cfg.poll_seconds)

        except Exception as e:
            log.error("LOOP ERROR: %r", e)
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
import os
import json
import time
import base64
import hmac
import hashlib
import logging
import smtplib
import datetime as dt
from dataclasses import dataclass, asdict
from email.mime.text import MIMEText
from typing import Optional, Dict, Any, List, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc-mm")


# =========================
# Time helpers (ET)
# =========================
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None  # fallback, will behave like UTC if missing

def now_et() -> dt.datetime:
    if ET:
        return dt.datetime.now(ET)
    return dt.datetime.utcnow()

def utc_now() -> dt.datetime:
    # Python 3.13 warns on utcnow(); use timezone-aware
    return dt.datetime.now(dt.timezone.utc)

def iso_ts_utc() -> str:
    return utc_now().isoformat()

def sleep_s(seconds: int):
    time.sleep(max(1, int(seconds)))


# =========================
# Config
# =========================
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return float(v)
    except:
        return default

def env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return int(v)
    except:
        return default

API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()

# Hard-fix the earlier Render DNS / moved API situation:
# - "api.kalshi.com" failed DNS on Render
# - "trading-api.kalshi.com" got 401 "moved"
# We force to elections API unless you override intentionally.
if "api.kalshi.com" in API_BASE or "trading-api.kalshi.com" in API_BASE:
    log.warning("KALSHI_API_BASE looked wrong for Render/migration: %s. Forcing https://api.elections.kalshi.com", API_BASE)
    API_BASE = "https://api.elections.kalshi.com"

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
POLL_SECONDS = env_int("POLL_SECONDS", 60)

ENABLE_TRADING = env_bool("ENABLE_TRADING", False)

# Win-farming / scalping knobs
EXTREME_THRESHOLD = env_float("EXTREME_THRESHOLD", 0.80)  # 0.80 => 80¢
MIN_EDGE_CENTS = env_int("MIN_EDGE_CENTS", 2)            # require at least this much perceived edge to enter
MAX_OPEN_POSITIONS = env_int("MAX_OPEN_POSITIONS", 1)
ORDER_QTY = env_int("ORDER_QTY", 1)                      # start 1 contract
MAX_DAILY_LOSS_PCT = env_float("MAX_DAILY_LOSS_PCT", 0.20)  # 20% of acct
BET_PCT_OF_LIQUIDITY = env_float("BET_PCT_OF_LIQUIDITY", 0.01)  # 1% default cap
MAX_HOLD_SECONDS = env_int("MAX_HOLD_SECONDS", 8 * 60)   # exit if stale (8 minutes)

# Email
EMAIL_ENABLED = env_bool("EMAIL_ENABLED", False)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = env_int("SMTP_PORT", 587)
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_TLS = env_bool("SMTP_TLS", True)
EMAIL_TO = os.getenv("EMAIL_TO", SMTP_USERNAME).strip()  # default to your own gmail

# Daily report timing (ET)
REPORT_HOUR = env_int("REPORT_HOUR_ET", 23)  # 11pm ET
REPORT_MINUTE = env_int("REPORT_MINUTE_ET", 55)

# Kalshi auth
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_PEM_BASE64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()

# Persist minimal state (Render disk is ephemeral unless you add a disk; /tmp is fine for runtime)
STATE_PATH = os.getenv("STATE_PATH", "/tmp/kalshi_bot_state.json")


# =========================
# Data structures
# =========================
@dataclass
class Trade:
    opened_at_utc: str
    market_ticker: str
    side: str                 # "YES" or "NO" (what we bought)
    entry_price_cents: int
    qty: int
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    closed_at_utc: Optional[str] = None
    exit_price_cents: Optional[int] = None
    realized_pnl_cents: Optional[int] = None
    status: str = "OPEN"      # OPEN/CLOSED/FAILED


@dataclass
class BotState:
    day_et: str
    starting_cash_cents: int = 0
    realized_pnl_cents: int = 0
    trades: List[Dict[str, Any]] = None
    last_report_day_et: Optional[str] = None
    last_mid_cents: Optional[int] = None
    open_trade: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        d = asdict(self)
        if d["trades"] is None:
            d["trades"] = []
        return json.dumps(d, indent=2)

    @staticmethod
    def from_disk() -> "BotState":
        if not os.path.exists(STATE_PATH):
            return BotState(day_et=now_et().date().isoformat(), trades=[])
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            return BotState(**d)
        except Exception:
            return BotState(day_et=now_et().date().isoformat(), trades=[])

    def save(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                f.write(self.to_json())
        except Exception as e:
            log.warning("Could not persist state: %s", e)


# =========================
# Email
# =========================
def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        return
    if not (SMTP_USERNAME and SMTP_PASSWORD and EMAIL_TO):
        log.warning("EMAIL_ENABLED=true but SMTP_USERNAME/SMTP_PASSWORD/EMAIL_TO not fully set.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = EMAIL_TO

    try:
        if SMTP_TLS:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
            server.starttls()
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, [EMAIL_TO], msg.as_string())
        server.quit()
        log.info("Daily email sent to %s", EMAIL_TO)
    except Exception as e:
        log.error("Email send failed: %s", e)


# =========================
# Kalshi auth + client
# =========================
class KalshiClient:
    """
    Implements Kalshi RSA request signing (typical pattern).
    If your existing code already worked fetching markets, your env vars are correct.
    """
    def __init__(self, api_base: str, key_id: str, private_key_pem_b64: str):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.private_key = None

        if private_key_pem_b64:
            try:
                pem_bytes = base64.b64decode(private_key_pem_b64)
                self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            except Exception as e:
                log.error("Failed to load private key from KALSHI_PRIVATE_KEY_PEM_BASE64: %s", e)
                self.private_key = None

    def _sign(self, method: str, path: str, timestamp: str, body: str) -> str:
        if not self.private_key:
            raise RuntimeError("No private key loaded. Set KALSHI_PRIVATE_KEY_PEM_BASE64.")
        # Common canonical string format:
        # {timestamp}{method}{path}{body}
        payload = (timestamp + method.upper() + path + body).encode("utf-8")
        signature = self.private_key.sign(
            payload,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        method_u = method.upper()

        body_str = ""
        data = None
        headers = {"Content-Type": "application/json"}

        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))
            data = body_str.encode("utf-8")

        ts = str(int(time.time() * 1000))  # ms timestamp is common
        if self.key_id and self.private_key:
            sig = self._sign(method_u, path, ts, body_str)
            # Header names used by many Kalshi examples
            headers.update({
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sig,
            })

        r = requests.request(method_u, url, params=params, data=data, headers=headers, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {r.text}")
        return r.json()

    # ---- Convenience wrappers ----
    def list_open_markets_for_series(self, series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
        # Works with /trade-api/v2/markets?status=open&series_ticker=KXBTC15M
        resp = self.request(
            "GET",
            "/trade-api/v2/markets",
            params={"limit": limit, "status": "open", "series_ticker": series_ticker},
        )
        # response shape can be {"markets":[...]} or {"markets": {"markets":[...]}}
        if "markets" in resp and isinstance(resp["markets"], list):
            return resp["markets"]
        if "markets" in resp and isinstance(resp["markets"], dict) and "markets" in resp["markets"]:
            return resp["markets"]["markets"]
        return resp.get("markets", [])

    def get_market(self, ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/trade-api/v2/markets/{ticker}")

    def get_portfolio(self) -> Dict[str, Any]:
        return self.request("GET", "/trade-api/v2/portfolio/balance")

    def create_order(self, market_ticker: str, side: str, action: str, price_cents: int, qty: int) -> Dict[str, Any]:
        """
        side: "yes" or "no" (contract)
        action: "buy" or "sell"
        price_cents: 1..99
        """
        payload = {
            "market_ticker": market_ticker,
            "side": side.lower(),
            "action": action.lower(),
            "type": "limit",
            "yes_price": price_cents if side.upper() == "YES" else None,
            "no_price": price_cents if side.upper() == "NO" else None,
            "count": qty,
        }
        # remove Nones to avoid API complaints
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.request("POST", "/trade-api/v2/orders", json_body=payload)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/trade-api/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self.request("POST", f"/trade-api/v2/orders/{order_id}/cancel")


# =========================
# Market math helpers
# =========================
def extract_book_prices(market: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """
    Returns yes_bid, yes_ask, no_bid, no_ask in cents (ints) or None.
    Works with common Kalshi response fields.
    """
    # Many responses include "yes_bid", "yes_ask", etc already as ints.
    def to_int(x):
        try:
            if x is None:
                return None
            return int(x)
        except:
            return None

    yes_bid = to_int(market.get("yes_bid"))
    yes_ask = to_int(market.get("yes_ask"))
    no_bid = to_int(market.get("no_bid"))
    no_ask = to_int(market.get("no_ask"))

    # Sometimes nested
    if yes_bid is None and isinstance(market.get("market"), dict):
        m = market["market"]
        yes_bid = to_int(m.get("yes_bid"))
        yes_ask = to_int(m.get("yes_ask"))
        no_bid = to_int(m.get("no_bid"))
        no_ask = to_int(m.get("no_ask"))

    return yes_bid, yes_ask, no_bid, no_ask

def mid_from_book(yes_bid: Optional[int], yes_ask: Optional[int]) -> Optional[int]:
    if yes_bid is None or yes_ask is None:
        return None
    return int(round((yes_bid + yes_ask) / 2))

def spread_cents(bid: Optional[int], ask: Optional[int]) -> Optional[int]:
    if bid is None or ask is None:
        return None
    return max(0, ask - bid)

def clamp_price(p: int) -> int:
    return max(1, min(99, int(p)))

def dynamic_takeprofit_stop(spread: int, momentum_cents: int) -> Tuple[int, int]:
    """
    DYNAMIC exits:
    - take profit grows slightly with spread and with favorable momentum
    - stop loss grows with spread, but is always larger than TP
    Returns (tp_cents, sl_cents) as deltas from entry.
    """
    # Base: want small consistent wins, but not 1-cent always.
    # Typical: spread 2-6 cents. Aim for TP 1-4 cents.
    tp = 1 + int(0.6 * spread)
    tp = min(6, max(1, tp))

    # If momentum is already favorable, allow slightly bigger take profit
    if momentum_cents >= 2:
        tp = min(8, tp + 1)

    # Stop larger than TP; allow room but cap risk
    sl = max(tp + 2, int(1.7 * spread) + 2)
    sl = min(15, max(4, sl))
    return tp, sl


# =========================
# Strategy logic
# =========================
def resolve_next_open_market(k: KalshiClient) -> Optional[str]:
    markets = k.list_open_markets_for_series(SERIES_PREFIX, limit=200)
    log.info("Open markets returned: %s", len(markets))
    if not markets:
        return None

    # Prefer earliest closing / nearest in time.
    # Many markets have "close_time" or "expiration_time" etc.
    def get_close_ts(m):
        for key in ("close_time", "expiration_time", "end_time", "settlement_time"):
            if key in m:
                return m.get(key)
        return None

    # If close_time is ISO, sort lexicographically is usually OK.
    markets_sorted = sorted(markets, key=lambda m: str(get_close_ts(m)))
    ticker = markets_sorted[0].get("ticker") or markets_sorted[0].get("market_ticker")
    return ticker

def should_enter_trade(state: BotState, cash_cents: int) -> bool:
    # Daily loss stop
    if state.starting_cash_cents > 0:
        max_loss = int(state.starting_cash_cents * MAX_DAILY_LOSS_PCT)
        if state.realized_pnl_cents <= -max_loss:
            log.warning("DAILY STOP HIT: realized_pnl=%sc <= -%sc max_loss. Trading disabled for rest of day.",
                        state.realized_pnl_cents, max_loss)
            return False

    # Single position only
    if state.open_trade is not None:
        return False

    # Liquidity sizing check: 1% cap
    max_risk = int(cash_cents * BET_PCT_OF_LIQUIDITY)
    # In Kalshi, max loss per contract roughly price paid (for the side you buy).
    # With 1 contract, price likely 20-80 cents; ensure it fits.
    # We'll compute per trade at entry time; this is just a quick gate.
    if max_risk < 25:  # if 1% of cash < 25 cents, still allow 1 contract but it’s tiny anyway
        return True
    return True

def pick_entry(market: Dict[str, Any], last_mid: Optional[int]) -> Optional[Dict[str, Any]]:
    yes_bid, yes_ask, no_bid, no_ask = extract_book_prices(market)
    if None in (yes_bid, yes_ask, no_bid, no_ask):
        return None

    # Convert to "YES mid" view for momentum
    mid = mid_from_book(yes_bid, yes_ask)
    momentum = 0
    if mid is not None and last_mid is not None:
        momentum = mid - last_mid  # + means YES drifting up

    y_spread = spread_cents(yes_bid, yes_ask) or 0
    n_spread = spread_cents(no_bid, no_ask) or 0
    avg_spread = int(round((y_spread + n_spread) / 2))

    # We want to "farm wins" = trade reversion after extremes (crowd overshoot).
    # If YES is extremely expensive (high probability priced), we buy NO (fade).
    # If NO is extremely expensive, we buy YES (fade).
    entry = None

    # Example: YES bid >= 80 means YES crowded.
    if yes_bid >= int(EXTREME_THRESHOLD * 100):
        # Buy NO at no_ask (we are fading YES)
        edge = (yes_bid - (100 - no_ask))  # rough “mispricing” proxy
        if edge >= MIN_EDGE_CENTS:
            entry = {
                "buy_side": "NO",
                "buy_price": clamp_price(no_ask),
                "momentum": momentum,
                "avg_spread": avg_spread,
                "reason": f"YES crowded (yes_bid={yes_bid}); fade by buying NO@{no_ask}",
            }

    # If NO bid >= 80 means NO crowded.
    if entry is None and no_bid >= int(EXTREME_THRESHOLD * 100):
        edge = (no_bid - (100 - yes_ask))
        if edge >= MIN_EDGE_CENTS:
            entry = {
                "buy_side": "YES",
                "buy_price": clamp_price(yes_ask),
                "momentum": -momentum,  # momentum relevant to bought side
                "avg_spread": avg_spread,
                "reason": f"NO crowded (no_bid={no_bid}); fade by buying YES@{yes_ask}",
            }

    return entry

def compute_exit_targets(entry_price: int, side: str, yes_bid: int, yes_ask: int, no_bid: int, no_ask: int,
                         avg_spread: int, momentum: int) -> Dict[str, int]:
    tp, sl = dynamic_takeprofit_stop(avg_spread, momentum)

    # For profit on bought side:
    # - If we bought YES, we exit by SELL YES at yes_bid.
    # - If we bought NO, we exit by SELL NO at no_bid.
    # Targets are expressed as price deltas relative to entry.
    target_price = clamp_price(entry_price + tp)
    stop_price = clamp_price(entry_price - sl)

    return {"tp_delta": tp, "sl_delta": sl, "target_price": target_price, "stop_price": stop_price}

def mark_to_market(side: str, entry_price: int, yes_bid: int, no_bid: int) -> int:
    # Unrealized PnL in cents for 1 contract:
    # If bought YES at entry, can sell at yes_bid; pnl = yes_bid - entry
    if side.upper() == "YES":
        return yes_bid - entry_price
    return no_bid - entry_price


# =========================
# Main loop
# =========================
def new_day_rollover(state: BotState, cash_cents: int):
    today = now_et().date().isoformat()
    if state.day_et != today:
        # Send final report for previous day if not already sent
        if state.last_report_day_et != state.day_et:
            send_daily_report(state, cash_cents, forcing_day=state.day_et)

        # reset for new day
        state.day_et = today
        state.starting_cash_cents = cash_cents
        state.realized_pnl_cents = 0
        state.trades = []
        state.open_trade = None
        state.last_mid_cents = None
        state.save()
        log.info("New ET day rollover: %s", today)

def send_daily_report(state: BotState, cash_cents: int, forcing_day: Optional[str] = None):
    day = forcing_day or state.day_et
    trades = state.trades or []
    wins = 0
    losses = 0
    total = 0
    pnl = state.realized_pnl_cents
    for t in trades:
        if t.get("status") == "CLOSED" and t.get("realized_pnl_cents") is not None:
            total += 1
            if t["realized_pnl_cents"] > 0:
                wins += 1
            elif t["realized_pnl_cents"] < 0:
                losses += 1

    body = []
    body.append(f"Kalshi BTC 15m Bot — Daily P&L ({day} ET)")
    body.append("")
    body.append(f"Starting cash (approx): ${state.starting_cash_cents/100:.2f}")
    body.append(f"Realized P&L: ${pnl/100:.2f}")
    body.append(f"Trades closed: {total} | Wins: {wins} | Losses: {losses}")
    if total > 0:
        body.append(f"Win rate: {wins/total*100:.1f}%")
    body.append("")
    body.append("Recent trades (last 10):")
    for t in (trades[-10:] if len(trades) > 10 else trades):
        body.append(
            f"- {t.get('market_ticker')} {t.get('side')} "
            f"entry {t.get('entry_price_cents')}c -> exit {t.get('exit_price_cents')}c "
            f"pnl {t.get('realized_pnl_cents')}c status={t.get('status')}"
        )

    send_email(f"Kalshi Bot Daily P&L — {day}", "\n".join(body))
    state.last_report_day_et = day
    state.save()

def report_due(state: BotState) -> bool:
    if not EMAIL_ENABLED:
        return False
    now = now_et()
    day = now.date().isoformat()
    if state.last_report_day_et == day:
        return False
    return (now.hour == REPORT_HOUR and now.minute >= REPORT_MINUTE)

def safe_get_cash_cents(k: KalshiClient) -> int:
    try:
        bal = k.get_portfolio()
        # Try common shapes
        # e.g. {"balance":{"cash":4980}} or {"cash":4980}
        if "cash" in bal:
            return int(round(float(bal["cash"])))
        if "balance" in bal and isinstance(bal["balance"], dict) and "cash" in bal["balance"]:
            return int(round(float(bal["balance"]["cash"])))
    except Exception as e:
        log.warning("Could not fetch cash balance: %s", e)
    # fallback: unknown
    return 0

def main():
    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", ENABLE_TRADING)
    log.info("POLL_SECONDS=%s", POLL_SECONDS)
    log.info("SERIES_PREFIX=%s", SERIES_PREFIX)
    log.info("API_BASE=%s", API_BASE)

    if not KALSHI_API_KEY_ID:
        log.error("Missing KALSHI_API_KEY_ID in environment.")
        return

    if not KALSHI_PRIVATE_KEY_PEM_BASE64:
        log.error("Missing KALSHI_PRIVATE_KEY_PEM_BASE64 in environment. "
                  "Kalshi trading requires a private key for signing.")
        return

    k = KalshiClient(API_BASE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PEM_BASE64)

    state = BotState.from_disk()
    if state.trades is None:
        state.trades = []

    # Initialize starting cash once if empty
    cash_cents = safe_get_cash_cents(k)
    if state.starting_cash_cents == 0 and cash_cents > 0:
        state.starting_cash_cents = cash_cents
        state.day_et = now_et().date().isoformat()
        state.save()

    while True:
        try:
            cash_cents = safe_get_cash_cents(k)
            new_day_rollover(state, cash_cents)

            if report_due(state):
                send_daily_report(state, cash_cents)

            # Resolve current/next open market
            ticker = resolve_next_open_market(k)
            if not ticker:
                log.warning("No open market found for %s", SERIES_PREFIX)
                sleep_s(POLL_SECONDS)
                continue

            market = k.get_market(ticker)
            yes_bid, yes_ask, no_bid, no_ask = extract_book_prices(market)
            if None in (yes_bid, yes_ask, no_bid, no_ask):
                log.warning("Market missing book fields: %s", ticker)
                sleep_s(POLL_SECONDS)
                continue

            mid = mid_from_book(yes_bid, yes_ask)
            if mid is not None:
                state.last_mid_cents = mid

            log.info(
                "Heartbeat ET now=%s | market=%s | yes %s/%s no %s/%s",
                now_et().strftime("%Y-%m-%d %H:%M:%S %Z"),
                ticker, yes_bid, yes_ask, no_bid, no_ask
            )

            # If an open trade exists, manage dynamic exit
            if state.open_trade is not None:
                ot = Trade(**state.open_trade)
                age = (utc_now() - dt.datetime.fromisoformat(ot.opened_at_utc)).total_seconds()
                unreal = mark_to_market(ot.side, ot.entry_price_cents, yes_bid, no_bid)

                # Dynamic targets (based on current spread and momentum)
                y_sp = spread_cents(yes_bid, yes_ask) or 0
                n_sp = spread_cents(no_bid, no_ask) or 0
                avg_sp = int(round((y_sp + n_sp) / 2))
                momentum = 0
                if mid is not None and state.last_mid_cents is not None:
                    # momentum in "YES mid"; translate for bought side below:
                    yes_mom = mid - state.last_mid_cents
                    momentum = yes_mom if ot.side.upper() == "YES" else -yes_mom

                targets = compute_exit_targets(
                    ot.entry_price_cents, ot.side, yes_bid, yes_ask, no_bid, no_ask, avg_sp, momentum
                )
                tp = targets["tp_delta"]
                sl = targets["sl_delta"]
                target_price = targets["target_price"]
                stop_price = targets["stop_price"]

                log.info(
                    "OPEN TRADE %s qty=%s entry=%sc unreal=%sc age=%.0fs | TP=%sc (target=%sc) SL=%sc (stop=%sc)",
                    ot.side, ot.qty, ot.entry_price_cents, unreal, age, tp, target_price, sl, stop_price
                )

                # Exit rules:
                # 1) Take profit if bid >= entry + tp
                # 2) Stop if bid <= entry - sl
                # 3) Time exit if too old
                exit_now = False
                exit_reason = ""

                if ot.side.upper() == "YES":
                    current_exit_bid = yes_bid
                else:
                    current_exit_bid = no_bid

                if current_exit_bid >= target_price:
                    exit_now = True
                    exit_reason = f"TP hit: bid {current_exit_bid} >= target {target_price}"
                elif current_exit_bid <= stop_price:
                    exit_now = True
                    exit_reason = f"SL hit: bid {current_exit_bid} <= stop {stop_price}"
                elif age >= MAX_HOLD_SECONDS:
                    exit_now = True
                    exit_reason = f"TIME exit: age {int(age)}s >= {MAX_HOLD_SECONDS}s"

                if exit_now:
                    if not ENABLE_TRADING:
                        log.info("Would exit but ENABLE_TRADING=False. Reason: %s", exit_reason)
                        # If paper mode, just close it as simulated
                        ot.closed_at_utc = iso_ts_utc()
                        ot.exit_price_cents = int(current_exit_bid)
                        ot.realized_pnl_cents = int(current_exit_bid - ot.entry_price_cents)
                        ot.status = "CLOSED"
                        state.realized_pnl_cents += ot.realized_pnl_cents
                        state.trades.append(asdict(ot))
                        state.open_trade = None
                        state.save()
                    else:
                        log.info("Exiting trade: %s", exit_reason)
                        # Place limit sell at current bid (aggressive enough to fill if book holds)
                        sell_price = int(current_exit_bid)
                        resp = k.create_order(
                            market_ticker=ot.market_ticker,
                            side=ot.side,
                            action="sell",
                            price_cents=clamp_price(sell_price),
                            qty=ot.qty,
                        )
                        order_id = resp.get("order", {}).get("order_id") or resp.get("order_id")
                        ot.exit_order_id = order_id

                        # Attempt to confirm fill quickly
                        filled = False
                        for _ in range(5):
                            time.sleep(2)
                            if not order_id:
                                break
                            try:
                                od = k.get_order(order_id)
                                status = (od.get("order", {}) or od).get("status", "")
                                filled_count = (od.get("order", {}) or od).get("filled_count") or 0
                                if str(status).lower() in ("filled", "executed") or int(filled_count) >= ot.qty:
                                    filled = True
                                    break
                            except Exception:
                                pass

                        # If not filled, do NOT chase forever. Cancel and close using current bid as mark.
                        if not filled and order_id:
                            try:
                                k.cancel_order(order_id)
                            except Exception:
                                pass

                        # Finalize trade record using current bid as realized proxy
                        ot.closed_at_utc = iso_ts_utc()
                        ot.exit_price_cents = int(current_exit_bid)
                        ot.realized_pnl_cents = int(current_exit_bid - ot.entry_price_cents)
                        ot.status = "CLOSED"
                        state.realized_pnl_cents += ot.realized_pnl_cents
                        state.trades.append(asdict(ot))
                        state.open_trade = None
                        state.save()

                sleep_s(POLL_SECONDS)
                continue

            # No open trade -> consider entry
            if not should_enter_trade(state, cash_cents):
                sleep_s(POLL_SECONDS)
                continue

            entry = pick_entry(market, state.last_mid_cents)
            if not entry:
                sleep_s(POLL_SECONDS)
                continue

            # Position sizing: 1 contract, but enforce BET_PCT_OF_LIQUIDITY cap if cash known
            buy_price = int(entry["buy_price"])
            qty = ORDER_QTY

            if cash_cents > 0:
                max_risk = int(cash_cents * BET_PCT_OF_LIQUIDITY)
                # max loss per contract approx buy_price
                if buy_price * qty > max_risk and max_risk > 0:
                    # scale down but keep at least 1
                    qty = max(1, max_risk // max(1, buy_price))
                    qty = max(1, min(qty, ORDER_QTY))

            if qty < 1:
                qty = 1

            log.info("ENTRY SIGNAL: %s | side=%s price=%sc qty=%s",
                     entry["reason"], entry["buy_side"], buy_price, qty)

            if not ENABLE_TRADING:
                log.info("ENABLE_TRADING=False, simulating entry only.")
                t = Trade(
                    opened_at_utc=iso_ts_utc(),
                    market_ticker=ticker,
                    side=entry["buy_side"],
                    entry_price_cents=buy_price,
                    qty=qty,
                    status="OPEN",
                )
                state.open_trade = asdict(t)
                state.save()
                sleep_s(POLL_SECONDS)
                continue

            # Place real buy order
            resp = k.create_order(
                market_ticker=ticker,
                side=entry["buy_side"],
                action="buy",
                price_cents=clamp_price(buy_price),
                qty=qty,
            )
            order_id = resp.get("order", {}).get("order_id") or resp.get("order_id")

            # Confirm fill briefly; if not filled, cancel and do nothing
            filled = False
            for _ in range(5):
                time.sleep(2)
                if not order_id:
                    break
                try:
                    od = k.get_order(order_id)
                    status = (od.get("order", {}) or od).get("status", "")
                    filled_count = (od.get("order", {}) or od).get("filled_count") or 0
                    if str(status).lower() in ("filled", "executed") or int(filled_count) >= qty:
                        filled = True
                        break
                except Exception:
                    pass

            if not filled:
                log.warning("Entry order not filled quickly; canceling. order_id=%s", order_id)
                if order_id:
                    try:
                        k.cancel_order(order_id)
                    except Exception:
                        pass
                sleep_s(POLL_SECONDS)
                continue

            t = Trade(
                opened_at_utc=iso_ts_utc(),
                market_ticker=ticker,
                side=entry["buy_side"],
                entry_price_cents=buy_price,
                qty=qty,
                entry_order_id=order_id,
                status="OPEN",
            )
            state.open_trade = asdict(t)
            state.save()

            sleep_s(POLL_SECONDS)

        except Exception as e:
            log.error("LOOP ERROR: %s", e)
            sleep_s(POLL_SECONDS)


if __name__ == "__main__":
    main()
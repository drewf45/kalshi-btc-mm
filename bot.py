import os
import time
import json
import uuid
import base64
import random
import smtplib
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from typing import Any, Dict, Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ==========================================================
# Helpers
# ==========================================================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "y")

def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)

def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)

def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(msg: str) -> None:
    print(f"[{utc_iso()}] {msg}", flush=True)


# ==========================================================
# Email Notifier (SMTP)
# ==========================================================
class EmailNotifier:
    """
    Uses SMTP. Recommended providers:
    - SendGrid SMTP
    - Gmail SMTP with an App Password (not your regular password)
    """
    def __init__(self):
        self.enabled = _env_bool("EMAIL_ENABLED", "false")
        self.smtp_host = _env("SMTP_HOST", "")
        self.smtp_port = _env_int("SMTP_PORT", "587")
        self.smtp_user = _env("SMTP_USERNAME", "")
        self.smtp_pass = _env("SMTP_PASSWORD", "")
        self.mail_from = _env("EMAIL_FROM", self.smtp_user)
        self.mail_to = _env("EMAIL_TO", "")
        self.use_tls = _env_bool("SMTP_TLS", "true")

        # prevent spam: per-key cooldown window
        self.cooldown_seconds = _env_int("EMAIL_COOLDOWN_SECONDS", "900")  # 15 min
        self._last_sent: Dict[str, float] = {}

    def _can_send(self, key: str) -> bool:
        now = time.time()
        last = self._last_sent.get(key, 0.0)
        if now - last >= self.cooldown_seconds:
            self._last_sent[key] = now
            return True
        return False

    def send(self, subject: str, body: str, dedupe_key: str = "generic") -> None:
        if not self.enabled:
            return
        if not self.mail_to or not self.smtp_host:
            # misconfigured -> don't crash the bot
            log("EMAIL_ENABLED=true but SMTP/EMAIL_TO not configured; skipping email.")
            return
        if not self._can_send(dedupe_key):
            return

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.mail_from
        msg["To"] = self.mail_to

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as server:
                if self.use_tls:
                    server.starttls()
                if self.smtp_user and self.smtp_pass:
                    server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.mail_from, [self.mail_to], msg.as_string())
            log(f"✅ Email sent: {subject}")
        except Exception as e:
            log(f"⚠️ Email send failed (ignored): {e}")


# ==========================================================
# Kalshi Key Loading
# ==========================================================
def load_private_key_from_env():
    """
    Preferred: KALSHI_PRIVATE_KEY_PEM_B64 = base64(PEM bytes)
    Fallback : KALSHI_PRIVATE_KEY_PEM     = PEM text (multiline or with \\n)
    """
    b64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
    if b64:
        try:
            pem_bytes = base64.b64decode(b64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64: {e}")

    pem_text = _env("KALSHI_PRIVATE_KEY_PEM", "")
    if pem_text:
        try:
            pem_bytes = pem_text.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM: {e}")

    raise RuntimeError("Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM")


# ==========================================================
# CONFIG
# ==========================================================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")
if not KALSHI_API_KEY_ID:
    # keep the error loud and early
    raise RuntimeError("Missing KALSHI_API_KEY_ID in environment variables.")

POLL_SECONDS = _env_int("POLL_SECONDS", "60")
MARKET_REFRESH_SECONDS = _env_int("MARKET_REFRESH_SECONDS", "30")

BET_DOLLARS = _env_float("BET_DOLLARS", "1")
MIN_CONTRACTS = _env_int("MIN_CONTRACTS", "1")

MIN_PRICE_CENTS = _env_int("MIN_PRICE_CENTS", "5")
MAX_PRICE_CENTS = _env_int("MAX_PRICE_CENTS", "95")

MAX_DAILY_LOSS = _env_float("MAX_DAILY_LOSS", "20")  # guard only (placeholder)

SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")
USE_NEXT_QUARTER_HOUR_END = _env_bool("USE_NEXT_QUARTER_HOUR_END", "true")

# IMPORTANT: If you set MARKET_TICKER in Render, it WILL OVERRIDE auto selection.
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")

# ET offset: set -5 for EST, -4 for EDT
ET_UTC_OFFSET_HOURS = _env_int("ET_UTC_OFFSET_HOURS", "-5")

DRY_RUN = _env_bool("DRY_RUN", "true")  # flip to false to trade

# Market open gating + delay
WAIT_FOR_OPEN = _env_bool("WAIT_FOR_OPEN", "true")
OPEN_DELAY_RANGE = _env("OPEN_DELAY_RANGE", "3-15")  # seconds, e.g. "3-15"
STUCK_AFTER_OPEN_MINUTES = _env_int("STUCK_AFTER_OPEN_MINUTES", "10")  # email if open but no book after this

# One-time startup test bet
STARTUP_TEST_BET = _env_bool("STARTUP_TEST_BET", "false")
STARTUP_TEST_MARKET = _env("STARTUP_TEST_MARKET", "")  # optional explicit ticker for startup test
STOP_AFTER_WAGERS = _env_int("STOP_AFTER_WAGERS", "0")  # 0 = no limit


# ==========================================================
# Time + Ticker helpers (NO seconds)
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
    # Example: KXBTC15M-26JAN171930 (NO seconds)
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
    return format_kalshi_btc15m_ticker(dt_et)

def parse_delay_range(rng: str) -> Tuple[int, int]:
    try:
        a, b = rng.split("-", 1)
        lo = max(0, int(a.strip()))
        hi = max(lo, int(b.strip()))
        return lo, hi
    except Exception:
        return (3, 15)


# ==========================================================
# Kalshi signing
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
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.2"})
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

        # Treat auth/order errors as "real failures"
        if resp.status_code in (401, 403):
            raise RuntimeError(f"AUTH_ERROR_{resp.status_code}: {resp.text[:200]}")
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
        return self._request("POST", "/portfolio/orders", json_body=payload)


# ==========================================================
# Orderbook parsing (supports multiple shapes)
# ==========================================================
def _norm_fp_level(level):
    # fp often: ["0.5600","12"] or [price,size]
    if level is None:
        return None
    if isinstance(level, list) and len(level) >= 2:
        return {"price_dollars": str(level[0]), "size": str(level[1])}
    if isinstance(level, dict):
        return level
    return None

def extract_best_asks(ob: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Returns (best_yes_ask, best_no_ask) in a normalized dict format.
    Handles:
      - ob["orderbook_fp"]["yes_dollars_asks"]
      - ob["orderbook_fp"]["yes_dollars"] (older)
      - ob["orderbook"]["yes_dollars"] etc
      - ob["yes"]["asks"] etc
    """

    # Sometimes returned root has key "orderbook"
    ob_root = ob.get("orderbook") if isinstance(ob, dict) else None
    if isinstance(ob_root, dict):
        # Many responses have: orderbook_keys=['no','no_dollars','yes','yes_dollars']
        # Those are commonly bids/asks arrays; we want asks-to-buy. Some endpoints provide *_asks explicitly, others not.
        # If only "yes_dollars" exists, it may already represent asks for buy at the top.
        # We'll treat list[0] as best ask if shape matches [price,size].
        yes_list = ob_root.get("yes_dollars_asks") or ob_root.get("yes_dollars")
        no_list = ob_root.get("no_dollars_asks") or ob_root.get("no_dollars")
        if isinstance(yes_list, list) and yes_list and isinstance(no_list, list) and no_list:
            best_yes = _norm_fp_level(yes_list[0])
            best_no = _norm_fp_level(no_list[0])
            if best_yes and best_no:
                return best_yes, best_no

    fp = ob.get("orderbook_fp") if isinstance(ob, dict) else None
    if isinstance(fp, dict):
        yes_asks = fp.get("yes_dollars_asks") or fp.get("yes_dollars")
        no_asks = fp.get("no_dollars_asks") or fp.get("no_dollars")
        if isinstance(yes_asks, list) and yes_asks and isinstance(no_asks, list) and no_asks:
            best_yes = _norm_fp_level(yes_asks[0])
            best_no = _norm_fp_level(no_asks[0])
            if best_yes and best_no:
                return best_yes, best_no

    # Non-fp fallback
    yes = ob.get("yes", {}) if isinstance(ob, dict) else {}
    no = ob.get("no", {}) if isinstance(ob, dict) else {}
    yes_asks_nf = yes.get("asks", []) if isinstance(yes, dict) else []
    no_asks_nf = no.get("asks", []) if isinstance(no, dict) else []
    best_yes_nf = yes_asks_nf[0] if yes_asks_nf else None
    best_no_nf = no_asks_nf[0] if no_asks_nf else None
    return best_yes_nf, best_no_nf

def dollars_str_to_cents(price_dollars: str) -> int:
    cents = int(round(float(price_dollars) * 100))
    return max(1, min(99, cents))

def level_price_cents(level: Dict[str, Any]) -> int:
    if not level:
        return 0
    if "price_dollars" in level:
        return dollars_str_to_cents(level["price_dollars"])
    if "price" in level:
        return int(level.get("price", 0))
    return 0

def choose_majority_side(best_yes_ask: Optional[Dict[str, Any]], best_no_ask: Optional[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    if not best_yes_ask or not best_no_ask:
        return None
    yes_cents = level_price_cents(best_yes_ask)
    no_cents = level_price_cents(best_no_ask)
    if yes_cents <= 0 or no_cents <= 0:
        return None

    # Price guard rails (avoid super illiquid extremes)
    if not (MIN_PRICE_CENTS <= yes_cents <= MAX_PRICE_CENTS and MIN_PRICE_CENTS <= no_cents <= MAX_PRICE_CENTS):
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
# Main
# ==========================================================
def utc_day() -> datetime.date:
    return datetime.now(timezone.utc).date()

def main():
    notifier = EmailNotifier()

    log("=== BOT STARTED ===")
    log(f"BASE_URL={BASE_URL}")
    log(f"DRY_RUN={DRY_RUN} POLL_SECONDS={POLL_SECONDS} MARKET_REFRESH_SECONDS={MARKET_REFRESH_SECONDS}")
    log(f"SERIES_PREFIX={SERIES_PREFIX} MARKET_TICKER_OVERRIDE={'(set)' if MARKET_TICKER_OVERRIDE else '(auto)'} USE_NEXT_QUARTER_HOUR_END={USE_NEXT_QUARTER_HOUR_END}")
    log(f"BET_DOLLARS=${BET_DOLLARS} MIN_CONTRACTS={MIN_CONTRACTS} MIN_PRICE_CENTS={MIN_PRICE_CENTS} MAX_PRICE_CENTS={MAX_PRICE_CENTS}")
    log(f"WAIT_FOR_OPEN={WAIT_FOR_OPEN} OPEN_DELAY_RANGE={OPEN_DELAY_RANGE} STUCK_AFTER_OPEN_MINUTES={STUCK_AFTER_OPEN_MINUTES}")
    log(f"STARTUP_TEST_BET={STARTUP_TEST_BET} STOP_AFTER_WAGERS={STOP_AFTER_WAGERS}")
    log(f"ENV_HAS_KALSHI_API_KEY_ID={bool(KALSHI_API_KEY_ID)} ENV_HAS_PEM_B64={bool(_env('KALSHI_PRIVATE_KEY_PEM_B64'))} ENV_HAS_PEM_TEXT={bool(_env('KALSHI_PRIVATE_KEY_PEM'))}")
    log(f"EMAIL_ENABLED={notifier.enabled}")

    client = KalshiClient(base_url=BASE_URL, key_id=KALSHI_API_KEY_ID)

    current_day = utc_day()
    daily_pnl = 0.0  # placeholder
    last_market_refresh = 0.0
    market_ticker = compute_target_market_ticker()

    wagers_done = 0
    first_real_order_email_sent = False

    backoff_seconds = 1

    # open detection + stuck timer
    market_open_detected_at: Optional[float] = None
    delayed_open_sleep_done = False

    # one-time startup test bet state
    startup_test_done = False

    while True:
        try:
            # Daily reset (guard only)
            if utc_day() != current_day:
                log("🔄 New UTC day — resetting daily guard")
                current_day = utc_day()
                daily_pnl = 0.0
                market_open_detected_at = None
                delayed_open_sleep_done = False

            if daily_pnl <= -MAX_DAILY_LOSS:
                log("🛑 DAILY LOSS LIMIT HIT — sleeping until reset")
                time.sleep(60)
                continue

            # Determine ticker
            now_ts = time.time()
            if MARKET_TICKER_OVERRIDE:
                market_ticker = MARKET_TICKER_OVERRIDE
            elif now_ts - last_market_refresh >= MARKET_REFRESH_SECONDS:
                market_ticker = compute_target_market_ticker()
                last_market_refresh = now_ts

            # Pull orderbook
            ob = client.get_orderbook(market_ticker, depth=1)

            best_yes_ask, best_no_ask = extract_best_asks(ob)
            choice = choose_majority_side(best_yes_ask, best_no_ask)

            has_usable_book = choice is not None

            # Market open detection: we consider it "open" when both asks exist and are parseable
            if has_usable_book:
                if market_open_detected_at is None:
                    market_open_detected_at = time.time()
                    delayed_open_sleep_done = False
                    log(f"✅ MARKET OPEN detected for {market_ticker} (yes_ask={best_yes_ask} no_ask={best_no_ask})")

            if WAIT_FOR_OPEN and market_open_detected_at is None:
                log(f"WAIT_FOR_OPEN: checking orderbook for {market_ticker} -> No usable orderbook yet")
                time.sleep(POLL_SECONDS)
                continue

            # If open detected, apply a small random delay once
            if market_open_detected_at is not None and not delayed_open_sleep_done:
                lo, hi = parse_delay_range(OPEN_DELAY_RANGE)
                delay = random.randint(lo, hi)
                log(f"OPEN_DELAY: sleeping {delay}s before trading (not required to trade exactly at open)")
                time.sleep(delay)
                delayed_open_sleep_done = True

            # Stuck-after-open email: open detected (or WAIT_FOR_OPEN false) but still no usable book long enough
            if market_open_detected_at is not None and not has_usable_book:
                minutes_open = (time.time() - market_open_detected_at) / 60.0
                if minutes_open >= STUCK_AFTER_OPEN_MINUTES:
                    subject = "⚠️ Kalshi Bot Stuck After Open"
                    body = (
                        f"Time: {utc_iso()}\n"
                        f"Market: {market_ticker}\n"
                        f"State: open_detected_but_no_usable_book\n"
                        f"Minutes since open: {minutes_open:.1f}\n"
                        f"best_yes_ask: {best_yes_ask}\n"
                        f"best_no_ask : {best_no_ask}\n"
                    )
                    notifier.send(subject, body, dedupe_key=f"stuck:{market_ticker}")
                    # Don't spam: reset the timer window by pushing open_detected_at forward a bit
                    market_open_detected_at = time.time()
                log(f"No usable orderbook yet for {market_ticker} (best_yes={best_yes_ask} best_no={best_no_ask})")
                time.sleep(POLL_SECONDS)
                continue

            if not has_usable_book:
                # Normal: liquidity not there yet
                log(f"No usable orderbook yet for {market_ticker} (best_yes={best_yes_ask} best_no={best_no_ask})")
                time.sleep(POLL_SECONDS)
                continue

            # One-time startup test bet (optional)
            if STARTUP_TEST_BET and not startup_test_done:
                test_ticker = STARTUP_TEST_MARKET.strip() or market_ticker
                side, price_cents = choice
                count = calc_contract_count(price_cents)
                log(f"STARTUP_TEST_BET: attempting one-time test bet on {test_ticker} side={side} price={price_cents}c count={count}")

                if DRY_RUN:
                    log("🧪 DRY_RUN=true (startup test bet simulated)")
                else:
                    resp = client.create_order_fok_buy(test_ticker, side=side, price_cents=price_cents, count=count)
                    order = resp.get("order", {}) if isinstance(resp, dict) else {}
                    log(f"✅ STARTUP TEST order response: {order}")

                    notifier.send(
                        "✅ Kalshi Bot Startup Test Order Placed",
                        f"Time: {utc_iso()}\nMarket: {test_ticker}\nSide: {side}\nPrice: {price_cents}c\nCount: {count}\nOrder: {json.dumps(order)[:1200]}",
                        dedupe_key="startup_test_success"
                    )

                    if not first_real_order_email_sent:
                        first_real_order_email_sent = True
                        notifier.send(
                            "✅ Kalshi Bot Live Trading Confirmed",
                            f"Time: {utc_iso()}\nFirst real order placed.\nMarket: {test_ticker}\nSide: {side}\nPrice: {price_cents}c\nCount: {count}",
                            dedupe_key="first_live_order"
                        )

                startup_test_done = True
                # Continue loop (don’t immediately place another bet)
                time.sleep(POLL_SECONDS)
                continue

            # Main bet decision
            side, price_cents = choice
            count = calc_contract_count(price_cents)
            est_cost = (count * price_cents) / 100.0

            log(f"TRADE DECISION: market={market_ticker} yes_ask={best_yes_ask} no_ask={best_no_ask} -> BUY {side.upper()} {price_cents}c x{count} (est_cost=${est_cost:.2f})")

            if DRY_RUN:
                log("🧪 DRY_RUN=true (not placing order)")
            else:
                resp = client.create_order_fok_buy(market_ticker, side=side, price_cents=price_cents, count=count)
                order = resp.get("order", {}) if isinstance(resp, dict) else {}
                log(f"✅ Order response: {order}")

                # Email once on first real order
                if not first_real_order_email_sent:
                    first_real_order_email_sent = True
                    notifier.send(
                        "✅ Kalshi Bot Live Trading Confirmed",
                        f"Time: {utc_iso()}\nFirst real order placed.\nMarket: {market_ticker}\nSide: {side}\nPrice: {price_cents}c\nCount: {count}",
                        dedupe_key="first_live_order"
                    )

            wagers_done += 1
            if STOP_AFTER_WAGERS and wagers_done >= STOP_AFTER_WAGERS:
                log(f"🛑 STOP_AFTER_WAGERS reached ({wagers_done}). Exiting.")
                return

            # Reset rate limit backoff after successful cycle
            backoff_seconds = 1

            time.sleep(POLL_SECONDS)

        except Exception as e:
            # 429: backoff but DON'T email unless it persists a lot
            if str(e) == "429_RATE_LIMIT":
                log(f"⚠️ 429 rate limit. Backing off {backoff_seconds}s")
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 120)
                continue

            # Real failure -> email
            err_text = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()

            log(f"❌ ERROR: {err_text}")
            subject = "⚠️ Kalshi Bot Error"
            body = (
                f"Time: {utc_iso()}\n"
                f"Market: {compute_target_market_ticker()}\n"
                f"DRY_RUN: {DRY_RUN}\n"
                f"Error: {err_text}\n\n"
                f"Traceback:\n{tb[:4000]}\n"
            )
            notifier.send(subject, body, dedupe_key=f"err:{err_text[:80]}")

            # small sleep so it doesn't spin
            time.sleep(5)


if __name__ == "__main__":
    main()
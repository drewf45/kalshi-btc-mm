# bot.py
import os
import time
import json
import base64
import logging
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc-mm")


# ----------------------------
# Env helpers (supports your backwards env-var drift)
# ----------------------------
def getenv_any(*names: str, default=None):
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return default


def to_bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def to_int(v, default=0):
    try:
        return int(str(v).strip())
    except Exception:
        return default


@dataclass
class Config:
    # Kalshi auth
    api_base: str
    api_key_id: str | None
    private_key_pem_base64: str | None

    # Strategy / runtime
    enable_trading: bool
    poll_seconds: int
    series_prefix: str

    # Risk
    max_daily_loss_pct: float           # 0.20 means stop at -20%
    bet_pct_of_liquidity: float         # 0.01 means 1% per trade
    extreme_threshold: float            # 0.95 means 95%

    # Email
    email_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_tls: bool
    smtp_username: str | None
    smtp_password: str | None
    email_to: str | None


def load_config() -> Config:
    api_base = getenv_any("KALSHI_API_BASE", default="https://api.elections.kalshi.com").rstrip("/")
    api_key_id = getenv_any("KALSHI_API_KEY_ID", "KALSHI_KEY_ID", default=None)

    # IMPORTANT: allow several historical names you used
    private_key_b64 = getenv_any(
        "KALSHI_PRIVATE_KEY_PEM_BASE64",
        "KALSHI_PRIVATE_KEY_PEM_Base64",
        "KALSHI_PRIVATE_KEY_BASE64",
        default=None
    )

    enable_trading = to_bool(getenv_any("ENABLE_TRADING", default="false"))
    poll_seconds = to_int(getenv_any("POLL_SECONDS", "POLL_SECONDS=60", default="60"), default=60)

    # You had both SERIES_PREFIX and a misspelled Series_PREFIC / Series_PREFIC.
    series_prefix = getenv_any("SERIES_PREFIX", "Series_PREFIC", "SERIES_PREFIC", default="KXBTC15M")
    series_prefix = str(series_prefix).strip().upper()

    max_daily_loss_pct = float(getenv_any("MAX_DAILY_LOSS_PCT", "MAX_DAILY_LOSS", default="0.20"))
    # If you set MAX_DAILY_LOSS=20 meaning "20%", convert it
    if max_daily_loss_pct > 1.0:
        max_daily_loss_pct = max_daily_loss_pct / 100.0

    bet_pct_of_liquidity = float(getenv_any("BET_PCT_OF_LIQUIDITY", default="0.01"))
    if bet_pct_of_liquidity > 1.0:
        bet_pct_of_liquidity = bet_pct_of_liquidity / 100.0

    extreme_threshold = float(getenv_any("EXTREME_THRESHOLD", default="0.95"))
    if extreme_threshold > 1.0:
        extreme_threshold = extreme_threshold / 100.0

    email_enabled = to_bool(getenv_any("EMAIL_ENABLED", default="false"))
    smtp_host = getenv_any("SMTP_HOST", default="smtp.gmail.com")
    smtp_port = to_int(getenv_any("SMTP_PORT", default="587"), default=587)
    smtp_tls = to_bool(getenv_any("SMTP_TLS", "STP_TLS", default="true"))  # you had STP_TLS typo once
    smtp_username = getenv_any("SMTP_USERNAME", default=None)
    smtp_password = getenv_any("SMTP_PASSWORD", default=None)
    email_to = getenv_any("EMAIL_TO", default=smtp_username)

    return Config(
        api_base=api_base,
        api_key_id=api_key_id,
        private_key_pem_base64=private_key_b64,
        enable_trading=enable_trading,
        poll_seconds=poll_seconds,
        series_prefix=series_prefix,
        max_daily_loss_pct=max_daily_loss_pct,
        bet_pct_of_liquidity=bet_pct_of_liquidity,
        extreme_threshold=extreme_threshold,
        email_enabled=email_enabled,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_tls=smtp_tls,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        email_to=email_to,
    )


# ----------------------------
# Email (Gmail STARTTLS correctly)
# ----------------------------
def send_email(cfg: Config, subject: str, body: str):
    if not cfg.email_enabled:
        return
    if not cfg.smtp_username or not cfg.smtp_password or not cfg.email_to:
        log.warning("EMAIL enabled but SMTP_USERNAME / SMTP_PASSWORD / EMAIL_TO missing")
        return

    try:
        msg = EmailMessage()
        msg["From"] = cfg.smtp_username
        msg["To"] = cfg.email_to
        msg["Subject"] = subject
        msg.set_content(body)

        import smtplib
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
            s.ehlo()
            if cfg.smtp_tls:
                context = ssl.create_default_context()
                s.starttls(context=context)
                s.ehlo()
            s.login(cfg.smtp_username, cfg.smtp_password)
            s.send_message(msg)
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.error("EMAIL FAILED: %s", repr(e))


# ----------------------------
# Kalshi client (signed requests)
# ----------------------------
class KalshiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        self._private_key = None
        self._has_creds = False
        self._ticker_format = None  # discovered format for KXBTC15M tickers

        self._load_creds()

    def _load_creds(self):
        if not self.cfg.api_key_id or not self.cfg.private_key_pem_base64:
            self._has_creds = False
            return

        try:
            # Base64 decode safely (strip whitespace/newlines)
            b64 = "".join(str(self.cfg.private_key_pem_base64).split())
            pem_bytes = base64.b64decode(b64)

            self._private_key = serialization.load_pem_private_key(
                pem_bytes, password=None
            )
            self._has_creds = True
        except Exception as e:
            self._has_creds = False
            log.error("Failed to load Kalshi private key: %s", repr(e))

    @property
    def can_auth(self) -> bool:
        return self._has_creds

    def _sign(self, ts_ms: str, method: str, path: str, query: dict | None, body: str) -> str:
        """
        Signature scheme: sign (timestamp + METHOD + path + ?query + body)
        - timestamp is milliseconds string
        - body must be exactly the string sent ('' for GET)
        """
        method = method.upper()
        q = ""
        if query:
            q = "?" + urlencode(query)
        msg = (ts_ms + method + path + q + body).encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, query: dict | None = None, json_body: dict | None = None):
        if not self.can_auth:
            raise RuntimeError("Kalshi credentials not loaded")

        url = self.cfg.api_base + path
        ts_ms = str(int(time.time() * 1000))
        body_str = ""
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

        sig = self._sign(ts_ms, method, path, query, body_str)

        headers = {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

        try:
            resp = self.session.request(
                method=method.upper(),
                url=url,
                params=query,
                data=(body_str if json_body is not None else None),
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            raise RuntimeError(f"HTTP request failed: {repr(e)}")

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:800]}")

        # Defensive JSON parsing
        if resp.text and resp.text.strip().startswith("{"):
            return resp.json()
        return resp.text

    # ---- Endpoints we use (defensive)
    def get_market(self, ticker: str) -> dict | None:
        try:
            return self.request("GET", f"/trade-api/v2/markets/{ticker}")
        except Exception as e:
            log.warning("Market fetch failed for %s: %s", ticker, str(e)[:300])
            return None

    def list_markets(self, series_ticker: str, limit: int = 100) -> dict | None:
        """
        We avoid filtering status=open because it caused 'no open markets found' failures.
        We list by series_ticker and then filter locally if needed.
        """
        try:
            return self.request("GET", "/trade-api/v2/markets", query={"series_ticker": series_ticker, "limit": limit})
        except Exception as e:
            log.warning("Markets list failed: %s", str(e)[:300])
            return None

    def get_balance(self) -> float | None:
        """
        Return available cash/liquidity if possible.
        This endpoint/shape varies; we parse defensively.
        """
        candidates = [
            "/trade-api/v2/portfolio/balance",
            "/trade-api/v2/portfolio",
            "/trade-api/v2/balance",
        ]
        for path in candidates:
            try:
                j = self.request("GET", path)
                # common patterns
                if isinstance(j, dict):
                    # Try nested
                    for keypath in [
                        ("balance", "available_cash"),
                        ("balance", "cash"),
                        ("balance", "available"),
                        ("portfolio", "cash"),
                        ("cash",),
                        ("available_cash",),
                        ("available",),
                    ]:
                        cur = j
                        ok = True
                        for k in keypath:
                            if not isinstance(cur, dict) or k not in cur:
                                ok = False
                                break
                            cur = cur[k]
                        if ok:
                            try:
                                return float(cur)
                            except Exception:
                                pass
                # If nothing worked, continue
            except Exception:
                continue
        return None

    def place_order(self, ticker: str, side: str, action: str, count: int, price: int):
        """
        side: "yes" or "no"
        action: "buy" or "sell"
        price: integer cents 1..99 (Kalshi commonly uses cents)
        """
        payload = {
            "ticker": ticker,
            "side": side.lower(),
            "action": action.lower(),
            "count": int(count),
            "type": "limit",
            "price": int(price),
        }
        return self.request("POST", "/trade-api/v2/orders", json_body=payload)


# ----------------------------
# Time/ticker logic (THE BIG FIX)
# ----------------------------
MONTH_ABBR = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

def ceil_to_next_15m(dt: datetime) -> datetime:
    """
    Always returns a time STRICTLY in the future aligned to 15-minute boundary.
    """
    dt = dt.replace(second=0, microsecond=0)
    minute = dt.minute
    # next multiple of 15 strictly ahead
    next_min = ((minute // 15) + 1) * 15
    if next_min >= 60:
        dt = dt.replace(minute=0) + timedelta(hours=1)
    else:
        dt = dt.replace(minute=next_min)
    return dt

def fmt_ticker_variant(series_prefix: str, target_utc: datetime, variant: int) -> str:
    """
    Kalshi has shown BOTH in your logs:
      Variant A: KXBTC15M-26JAN181700  (YYMMMDDHHMM)
      Variant B: KXBTC15M-18JAN261700  (DDMMMYYHHMM)
    We try both and pick the one that exists.
    """
    yy = target_utc.strftime("%y")          # 26
    dd = target_utc.strftime("%d")          # 18
    mon = MONTH_ABBR[target_utc.month - 1]  # JAN
    hhmm = target_utc.strftime("%H%M")      # 1700

    if variant == 1:
        # YYMMMDDHHMM  -> 26JAN181700
        suffix = f"{yy}{mon}{dd}{hhmm}"
    else:
        # DDMMMYYHHMM  -> 18JAN261700
        suffix = f"{dd}{mon}{yy}{hhmm}"

    return f"{series_prefix}-{suffix}"


def discover_ticker_format(client: KalshiClient, series_prefix: str, target_utc: datetime) -> tuple[str | None, int | None]:
    """
    Try both ticker suffix orders; return the first that exists.
    """
    for variant in (1, 2):
        t = fmt_ticker_variant(series_prefix, target_utc, variant)
        j = client.get_market(t)
        if isinstance(j, dict) and j:
            return t, variant
    return None, None


# ----------------------------
# Strategy
# ----------------------------
def extract_yes_prices(market_json: dict) -> tuple[int | None, int | None]:
    """
    We try to pull yes_bid/yes_ask in CENTS, defensively.
    Different Kalshi responses can use different keys.
    """
    if not isinstance(market_json, dict):
        return None, None

    # Try typical keys
    candidates = [
        ("market", "yes_bid"),
        ("market", "yes_ask"),
        ("yes_bid",),
        ("yes_ask",),
        ("best_yes_bid",),
        ("best_yes_ask",),
        ("yes_bid",),
        ("yes_ask",),
    ]

    # If nested object exists under "market"
    m = market_json.get("market") if isinstance(market_json.get("market"), dict) else market_json

    yes_bid = None
    yes_ask = None

    for key in ("yes_bid", "best_yes_bid", "bid_yes", "yesBid"):
        if key in m:
            try:
                yes_bid = int(m[key])
                break
            except Exception:
                pass

    for key in ("yes_ask", "best_yes_ask", "ask_yes", "yesAsk"):
        if key in m:
            try:
                yes_ask = int(m[key])
                break
            except Exception:
                pass

    return yes_bid, yes_ask


def decide_trade(cfg: Config, yes_bid: int | None, yes_ask: int | None):
    """
    Trigger when market is extremely one-sided:
      - YES ask >= 95 => buy NO (cheap) for micro-wins
      - YES ask <= 5  => buy YES (cheap)
    """
    if yes_ask is None:
        return None

    thr_hi = int(round(cfg.extreme_threshold * 100))      # 95
    thr_lo = 100 - thr_hi                                # 5

    if yes_ask >= thr_hi:
        # Crowd very YES -> we buy NO around <= 5c
        target_side = "no"
        # Price for NO in cents ~ 100 - YES_ask (approx)
        limit_price = max(1, min(thr_lo, 100 - yes_ask))
        return ("buy", target_side, limit_price)

    if yes_ask <= thr_lo:
        # Crowd very NO -> we buy YES around <= 5c
        target_side = "yes"
        limit_price = max(1, min(thr_lo, yes_ask))
        return ("buy", target_side, limit_price)

    return None


def calc_contract_count(cfg: Config, liquidity: float, limit_price_cents: int) -> int:
    """
    Max spend per trade = 1% of liquidity.
    Each contract costs limit_price/100 dollars.
    """
    if liquidity <= 0:
        return 0
    max_spend = liquidity * cfg.bet_pct_of_liquidity
    price_dollars = max(limit_price_cents, 1) / 100.0
    count = int(max_spend // price_dollars)
    return max(1, count) if count > 0 else 0


# ----------------------------
# Main loop
# ----------------------------
def main():
    cfg = load_config()

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", cfg.enable_trading)
    log.info("POLL_SECONDS=%s", cfg.poll_seconds)
    log.info("SERIES_PREFIX=%s", cfg.series_prefix)
    log.info("API_BASE=%s", cfg.api_base)
    log.info("EMAIL_ENABLED=%s", cfg.email_enabled)

    client = KalshiClient(cfg)

    if not client.can_auth:
        log.warning("Kalshi credentials missing/invalid — running in READ-ONLY mode")
        cfg.enable_trading = False

    # Daily risk tracking (simple local tracker; resets at UTC day boundary)
    current_utc_day = datetime.now(timezone.utc).date()
    day_start_liquidity = None
    max_loss_dollars = None
    stopped_for_day = False

    ticker_variant = None  # discovered format (1 or 2)

    while True:
        loop_start = time.time()
        now_utc = datetime.now(timezone.utc)

        # New UTC day reset
        if now_utc.date() != current_utc_day:
            current_utc_day = now_utc.date()
            day_start_liquidity = None
            max_loss_dollars = None
            stopped_for_day = False
            log.info("New UTC trading day, resetting daily stop.")

        # Always target NEXT 15m boundary (the key fix you asked for)
        target_utc = ceil_to_next_15m(now_utc)

        # Build ticker (try discovered variant first, else discover)
        ticker = None
        if ticker_variant in (1, 2):
            ticker = fmt_ticker_variant(cfg.series_prefix, target_utc, ticker_variant)
            m = client.get_market(ticker) if client.can_auth else None
            if not m:
                ticker_variant = None  # force rediscovery

        if ticker_variant is None:
            t, v = discover_ticker_format(client, cfg.series_prefix, target_utc) if client.can_auth else (None, None)
            ticker = t
            ticker_variant = v

        if not ticker:
            log.error("Could not resolve a valid ticker for next 15m boundary (%s).", target_utc.isoformat())
            time.sleep(cfg.poll_seconds)
            continue

        log.info("CHECKING MARKET (next 15m): %s (UTC=%s)", ticker, target_utc.strftime("%Y-%m-%d %H:%M"))

        market = client.get_market(ticker) if client.can_auth else None
        if not market:
            log.warning("Market not found yet (might not be listed yet). Will retry.")
            time.sleep(cfg.poll_seconds)
            continue

        # Liquidity
        liquidity = client.get_balance() if client.can_auth else None
        if liquidity is None:
            log.warning("No liquidity yet (balance unavailable).")
        else:
            if day_start_liquidity is None:
                day_start_liquidity = liquidity
                max_loss_dollars = day_start_liquidity * cfg.max_daily_loss_pct
                log.info("Day start liquidity: %.2f | Daily stop loss ($): %.2f", day_start_liquidity, max_loss_dollars)

            # crude daily stop (if liquidity drop exceeds max_loss)
            if max_loss_dollars is not None and (day_start_liquidity - liquidity) >= max_loss_dollars:
                if not stopped_for_day:
                    stopped_for_day = True
                    msg = f"Daily stop triggered. Start={day_start_liquidity:.2f} Now={liquidity:.2f} Loss={(day_start_liquidity-liquidity):.2f}"
                    log.error(msg)
                    send_email(cfg, "Kalshi bot: DAILY STOP TRIGGERED", msg)

        # Extract prices & decide
        yes_bid, yes_ask = extract_yes_prices(market)
        log.info("Prices (cents): yes_bid=%s yes_ask=%s", yes_bid, yes_ask)

        trade_plan = decide_trade(cfg, yes_bid, yes_ask)

        if not trade_plan:
            log.info("No signal (not extreme enough).")
        else:
            action, side, limit_price = trade_plan
            log.info("Signal: %s %s @ %sc (extreme threshold=%.0f%%)",
                     action.upper(), side.upper(), limit_price, cfg.extreme_threshold * 100)

            if stopped_for_day:
                log.warning("Stopped for day — not trading.")
            elif not cfg.enable_trading:
                log.warning("Trading disabled — would have placed: %s %s %s @ %sc", action, side, ticker, limit_price)
            else:
                if liquidity is None:
                    log.warning("Cannot size trade without liquidity. Skipping trade.")
                else:
                    count = calc_contract_count(cfg, liquidity, limit_price)
                    if count <= 0:
                        log.warning("Calculated contract count <= 0. Skipping.")
                    else:
                        try:
                            resp = client.place_order(
                                ticker=ticker,
                                side=side,
                                action=action,
                                count=count,
                                price=limit_price,
                            )
                            log.info("ORDER PLACED: %s", json.dumps(resp)[:800])
                            send_email(
                                cfg,
                                f"Kalshi bot: order placed ({ticker})",
                                f"Placed {action} {side} x{count} @ {limit_price}c\n\nResponse:\n{json.dumps(resp, indent=2)[:4000]}",
                            )
                        except Exception as e:
                            err = str(e)
                            log.error("ORDER FAILED: %s", err[:800])
                            send_email(cfg, "Kalshi bot: ORDER FAILED", err[:4000])

        # Heartbeat timing (never silent)
        elapsed = time.time() - loop_start
        sleep_for = max(1, cfg.poll_seconds - int(elapsed))
        log.info("Heartbeat: loop=%.2fs sleeping=%ss", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
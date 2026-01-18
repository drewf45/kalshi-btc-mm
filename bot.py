import os
import time
import json
import base64
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

# Crypto deps (installed via cryptography)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc-mm")


# ---------------------------
# Config helpers
# ---------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return int(str(v).strip())


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return float(str(v).strip())


def first_env(*names: str) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return None


def mask(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


# ---------------------------
# Email (Gmail App Password via SMTP)
# ---------------------------
def send_email(subject: str, body: str) -> None:
    """
    Uses STARTTLS on port 587 by default (correct for smtp.gmail.com:587).
    If you want SSL on 465, set SMTP_PORT=465 and SMTP_USE_SSL=true.
    """
    import smtplib
    import ssl
    from email.mime.text import MIMEText

    email_enabled = env_bool("EMAIL_ENABLED", False)
    if not email_enabled:
        return

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = env_int("SMTP_PORT", 587)
    smtp_user = (os.getenv("SMTP_USERNAME") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASSWORD") or "").strip()
    smtp_to = (os.getenv("EMAIL_TO") or smtp_user).strip()
    use_ssl = env_bool("SMTP_USE_SSL", False)  # for port 465 typically
    use_tls = env_bool("SMTP_TLS", True)       # for port 587 typically

    if not smtp_user or not smtp_pass or not smtp_to:
        raise RuntimeError("Email enabled but SMTP_USERNAME/SMTP_PASSWORD/EMAIL_TO missing")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = smtp_to

    ctx = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [smtp_to], msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=ctx)
                server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [smtp_to], msg.as_string())


def safe_email(subject: str, body: str) -> None:
    try:
        send_email(subject, body)
        log.info("EMAIL SENT: %s", subject)
    except Exception as e:
        log.error("EMAIL FAILED: %r", e)


# ---------------------------
# Kalshi Auth + Client (RSA-PSS)
# ---------------------------
@dataclass
class KalshiCreds:
    key_id: str
    private_key_pem: bytes
    api_base: str


class KalshiClient:
    def __init__(self, creds: KalshiCreds):
        self.creds = creds
        self.session = requests.Session()
        self._priv = serialization.load_pem_private_key(creds.private_key_pem, password=None)

    def _timestamp_ms(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, message: bytes) -> str:
        sig = self._priv.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    def request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
        method_u = method.upper().strip()

        # Expect full paths like /trade-api/v2/...
        if not path.startswith("/"):
            path = "/" + path

        url = self.creds.api_base.rstrip("/") + path
        ts = self._timestamp_ms()

        body_bytes = b""
        if json_body is not None:
            body_bytes = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        # Message format for signing varies by Kalshi version.
        # This follows: timestamp + method + path + body (common in their v2 docs/examples)
        msg = (ts + method_u + path).encode("utf-8") + body_bytes
        sig = self._sign(msg)

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.creds.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

        resp = self.session.request(
            method=method_u,
            url=url,
            params=params,
            data=body_bytes if json_body is not None else None,
            headers=headers,
            timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:500]}")

        if resp.text.strip() == "":
            return {}
        return resp.json()

    def get_markets(self, status: str = "open", limit: int = 200, cursor: Optional[str] = None) -> dict:
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/trade-api/v2/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/trade-api/v2/markets/{ticker}")

    def get_balance(self) -> dict:
        return self.request("GET", "/trade-api/v2/portfolio/balance")

    def create_order(self, ticker: str, side: str, count: int, price: int) -> dict:
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": int(count),
            "type": "limit",
            "price": int(price),
        }
        return self.request("POST", "/trade-api/v2/portfolio/orders", json_body=body)


def load_kalshi_creds() -> Optional[KalshiCreds]:
    key_id = first_env("KALSHI_API_KEY_ID", "KALSHI_KEY_ID")
    key_b64 = first_env(
        "KALSHI_PRIVATE_KEY_PEM_BASE64",
        "KALSHI_PRIVATE_KEY_PEM_Base64",
        "KALSHI_PRIVATE_KEY_BASE64",
        "KALSHI_PRIVATE_KEY",
    )
    api_base = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()

    if not key_id or not key_b64:
        return None

    cleaned = "".join(str(key_b64).strip().split())

    pem_bytes = base64.b64decode(cleaned)
    if b"BEGIN" not in pem_bytes:
        raise RuntimeError("Decoded private key does not look like PEM. Re-check the base64.")

    return KalshiCreds(key_id=str(key_id).strip(), private_key_pem=pem_bytes, api_base=api_base)


# ---------------------------
# Strategy helpers
# ---------------------------
def cents(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if 0.0 <= v <= 1.0:
            return int(round(v * 100))
        return int(round(v))
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        f = float(s)
        if 0.0 <= f <= 1.0:
            return int(round(f * 100))
        return int(round(f))
    if isinstance(v, dict) and "price" in v:
        return cents(v["price"])
    return None


def pick_current_market(k: KalshiClient, prefix: str) -> Tuple[str, dict]:
    best = None
    best_close = None

    cursor = None
    for _ in range(5):
        data = k.get_markets(status="open", limit=200, cursor=cursor)
        markets = data.get("markets", []) or data.get("data", []) or []
        cursor = data.get("cursor")

        now_utc = dt.datetime.now(dt.timezone.utc)

        for m in markets:
            t = m.get("ticker")
            if not isinstance(t, str) or not t.startswith(prefix + "-"):
                continue

            ct = m.get("close_time") or m.get("close_ts") or m.get("closeTime")
            close_dt = None
            if isinstance(ct, str):
                try:
                    close_dt = dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                except Exception:
                    close_dt = None

            if close_dt is None:
                if best is None:
                    best = t
                    best_close = now_utc + dt.timedelta(hours=1)
                continue

            if close_dt <= now_utc:
                continue

            if best_close is None or close_dt < best_close:
                best = t
                best_close = close_dt

        if not cursor:
            break

    if not best:
        raise RuntimeError(f"No open markets found for prefix={prefix}")

    return best, k.get_market(best)


def get_available_cash_cents(balance: dict) -> int:
    # Try a few likely shapes
    if not isinstance(balance, dict):
        return 0
    for key in ("available_cash", "balance", "cash_balance"):
        v = balance.get(key)
        if isinstance(v, int):
            return v
    cash = balance.get("cash")
    if isinstance(cash, dict):
        for key in ("available", "available_cash", "balance", "value"):
            v = cash.get(key)
            if isinstance(v, int):
                return v
    return 0


# ---------------------------
# Main
# ---------------------------
@dataclass
class DayRisk:
    day: str
    start_cash_cents: int
    max_loss_cents: int
    stopped: bool = False


def utc_day() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def main():
    enable_trading = env_bool("ENABLE_TRADING", False)
    poll_seconds = env_int("POLL_SECONDS", 60)
    prefix = (os.getenv("SERIES_PREFIX") or "KXBTC15M").strip()

    extreme_pct = env_int("EXTREME_PCT", 95)          # trigger at 95%/5%
    bet_pct = env_float("BET_PCT", 0.01)              # 1% of liquidity per bet
    max_daily_loss_pct = env_float("MAX_DAILY_LOSS_PCT", 0.20)  # 20% of account per day

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", enable_trading)
    log.info("POLL_SECONDS=%s", poll_seconds)
    log.info("SERIES_PREFIX=%s", prefix)

    creds = load_kalshi_creds()
    if not creds:
        log.warning("READ-ONLY: Missing Kalshi credentials. Need KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM_BASE64.")
        enable_trading = False
        k = None
    else:
        log.info("Kalshi key id detected: %s", mask(creds.key_id))
        k = KalshiClient(creds)

    risk: Optional[DayRisk] = None

    while True:
        try:
            if not k:
                log.info("READ-ONLY: no client. sleeping...")
                time.sleep(poll_seconds)
                continue

            # Reset risk each UTC day
            day = utc_day()
            if risk is None or risk.day != day:
                bal = k.get_balance()
                cash_cents = get_available_cash_cents(bal)
                max_loss = int(cash_cents * max_daily_loss_pct)
                risk = DayRisk(day=day, start_cash_cents=cash_cents, max_loss_cents=max_loss, stopped=False)
                log.info("New UTC day: start_cash_cents=%s max_loss_cents=%s", cash_cents, max_loss)

            # Current cash
            bal = k.get_balance()
            cash_cents = get_available_cash_cents(bal)
            if cash_cents <= 0:
                log.info("No available cash yet.")
                time.sleep(poll_seconds)
                continue

            # Daily stop
            if cash_cents < (risk.start_cash_cents - risk.max_loss_cents):
                if not risk.stopped:
                    risk.stopped = True
                    msg = f"DAILY STOP HIT: start={risk.start_cash_cents} now={cash_cents} max_loss={risk.max_loss_cents}"
                    log.error(msg)
                    safe_email("Kalshi BTC bot: DAILY STOP HIT", msg)
                time.sleep(poll_seconds)
                continue

            # Pick the correct CURRENT open BTC 15m market
            ticker, market = pick_current_market(k, prefix)
            log.info("CHECKING MARKET: %s", ticker)

            # Pull prices (try common fields)
            yes_ask = cents(market.get("yes_ask") or market.get("yesAsk"))
            no_ask = cents(market.get("no_ask") or market.get("noAsk"))
            yes_bid = cents(market.get("yes_bid") or market.get("yesBid"))
            no_bid = cents(market.get("no_bid") or market.get("noBid"))

            if yes_ask is None or no_ask is None:
                log.info("No usable prices yet. yes_ask=%s no_ask=%s", yes_ask, no_ask)
                time.sleep(poll_seconds)
                continue

            # Extreme trigger logic:
            # If YES is 95c+ -> market thinks YES is 95% likely -> bet NO
            # If YES is 5c-  -> market thinks YES is 5% likely  -> bet YES
            trigger_hi = extreme_pct
            trigger_lo = 100 - extreme_pct

            side = None
            price = None

            if yes_ask >= trigger_hi:
                side = "no"
                price = no_ask
            elif yes_ask <= trigger_lo:
                side = "yes"
                price = yes_ask
            else:
                log.info("No signal. yes_ask=%sc yes_bid=%s no_ask=%s no_bid=%s", yes_ask, yes_bid, no_ask, no_bid)
                time.sleep(poll_seconds)
                continue

            # Bet sizing: 1% of cash
            bet_cents = int(cash_cents * bet_pct)
            if bet_cents < 25:
                bet_cents = 25  # minimum tiny bet

            cost_per_contract = max(1, int(price))
            count = bet_cents // cost_per_contract
            if count <= 0:
                log.info("Bet too small for price. cash=%s bet=%s price=%s", cash_cents, bet_cents, price)
                time.sleep(poll_seconds)
                continue

            log.info("SIGNAL: BUY %s | price=%sc | count=%s | bet_cents=%s | cash=%s", side.upper(), price, count, bet_cents, cash_cents)

            if not enable_trading:
                log.info("READ-ONLY: would place order now.")
                time.sleep(poll_seconds)
                continue

            # Place order
            try:
                resp = k.create_order(ticker=ticker, side=side, count=count, price=price)
                log.info("ORDER OK: %s", json.dumps(resp)[:800])
                safe_email(
                    "Kalshi BTC bot: ORDER PLACED",
                    f"ticker={ticker}\nside={side}\nprice={price}\ncount={count}\nbet_cents={bet_cents}\ncash_cents={cash_cents}\n\nresp={json.dumps(resp)[:1500]}",
                )
            except Exception as e:
                err = f"ORDER FAILED: {e!r}"
                log.error(err)
                safe_email("Kalshi BTC bot: ORDER FAILED", err)

            time.sleep(poll_seconds)

        except Exception as loop_err:
            log.error("LOOP ERROR: %r", loop_err)
            safe_email("Kalshi BTC bot: LOOP ERROR", repr(loop_err))
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
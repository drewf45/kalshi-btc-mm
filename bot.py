import os
import time
import json
import base64
import logging
import datetime
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("kalshi-btc-mm")


# ----------------------------
# Config helpers
# ----------------------------
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default

def pick_env(*keys: str) -> Optional[str]:
    """Return the first non-empty env var value from keys."""
    for k in keys:
        v = os.getenv(k)
        if v and v.strip():
            return v.strip()
    return None


# ----------------------------
# Kalshi Auth (per docs)
# Signature = base64( RSA-PSS-SHA256( timestamp + METHOD + path_without_query ) )
# ----------------------------
class KalshiClient:
    def __init__(self, api_base: str, api_key_id: str, private_key_pem_b64: Optional[str]):
        self.api_base = api_base.rstrip("/")
        self.api_key_id = api_key_id

        self.private_key = None
        if private_key_pem_b64:
            try:
                pem_bytes = base64.b64decode(private_key_pem_b64)
                self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            except Exception as e:
                log.error("Failed to load private key from base64 PEM: %s", e)
                self.private_key = None

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if not self.private_key:
            raise RuntimeError("Missing private key (KALSHI_PRIVATE_KEY_PEM_BASE64)")
        path_wo_query = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_wo_query}".encode("utf-8")
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Dict[str, Any]:
        url = self.api_base + path
        timestamp_ms = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))

        headers = {"KALSHI-ACCESS-KEY": self.api_key_id, "KALSHI-ACCESS-TIMESTAMP": timestamp_ms}
        if self.private_key:
            headers["KALSHI-ACCESS-SIGNATURE"] = self._sign(timestamp_ms, method.upper(), path)

        resp = self.session.request(method=method.upper(), url=url, params=params, json=json_body, headers=headers, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:500]}")
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def get_balance_cents(self) -> Optional[int]:
        # /trade-api/v2/portfolio/balance returns {"balance": <cents>, ...}
        data = self.request("GET", "/trade-api/v2/portfolio/balance")
        bal = data.get("balance")
        if isinstance(bal, int):
            return bal
        return None

    def get_market(self, ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/trade-api/v2/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> Dict[str, Any]:
        # Kalshi docs: /trade-api/v2/markets/{ticker}/orderbook
        return self.request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")

    def list_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> Dict[str, Any]:
        # If API supports filters, this will work; if not, it will still return markets.
        params = {"limit": limit, "status": status, "series_ticker": series_ticker}
        return self.request("GET", "/trade-api/v2/markets", params=params)


# ----------------------------
# Email (safe TLS/SSL handling)
# ----------------------------
def send_email(subject: str, body: str) -> None:
    import smtplib
    from email.mime.text import MIMEText

    enabled = env_bool("EMAIL_ENABLED", False)
    if not enabled:
        return

    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = env_int("SMTP_PORT", 587)
    user = os.getenv("SMTP_USERNAME", "").strip()
    pwd = os.getenv("SMTP_PASSWORD", "").strip()
    use_tls = env_bool("SMTP_TLS", True)

    if not (host and port and user and pwd):
        log.warning("EMAIL_ENABLED=true but SMTP env vars missing; skipping email.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = user

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, pwd)
                s.sendmail(user, [user], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                if use_tls:
                    s.starttls()
                s.login(user, pwd)
                s.sendmail(user, [user], msg.as_string())
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.error("EMAIL FAILED: %r", e)


# ----------------------------
# Time / ticker logic
# Format from your link:
# KXBTC15M-26JAN181700  -> YY MON DD HHMM (ET)
# ----------------------------
ET = ZoneInfo("America/New_York")

def next_15m_boundary_et(now_et: datetime.datetime) -> datetime.datetime:
    # round UP to next 15m boundary
    minute = now_et.minute
    add = (15 - (minute % 15)) % 15
    if add == 0:
        add = 15
    target = (now_et.replace(second=0, microsecond=0) + datetime.timedelta(minutes=add))
    return target

def format_market_ticker(series_prefix: str, boundary_et: datetime.datetime) -> str:
    yy = f"{boundary_et.year % 100:02d}"
    mon = boundary_et.strftime("%b").upper()  # JAN, FEB, ...
    dd = f"{boundary_et.day:02d}"
    hhmm = boundary_et.strftime("%H%M")       # 1700
    return f"{series_prefix}-{yy}{mon}{dd}{hhmm}"


# ----------------------------
# Market bias detection ("95% on one side")
# We interpret this as orderbook depth dominance.
# If YES shares / total shares >= 0.95 => market is heavily YES
# If NO shares / total shares >= 0.95 => market is heavily NO
# ----------------------------
def orderbook_side_shares(orderbook: Dict[str, Any]) -> Tuple[int, int]:
    # orderbook shape varies; handle common shapes:
    # {
    #   "orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}
    # }
    ob = orderbook.get("orderbook") if isinstance(orderbook, dict) else None
    if not isinstance(ob, dict):
        return (0, 0)
    yes_levels = ob.get("yes") or []
    no_levels = ob.get("no") or []

    def sum_qty(levels: Any) -> int:
        total = 0
        if isinstance(levels, list):
            for lvl in levels:
                if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    qty = lvl[1]
                    if isinstance(qty, int):
                        total += qty
        return total

    return (sum_qty(yes_levels), sum_qty(no_levels))


# ----------------------------
# Main loop
# ----------------------------
def main():
    # FIX 1: correct base URL defaults to official production base.
    api_base = os.getenv("KALSHI_API_BASE", "https://api.kalshi.com").strip()

    # FIX 2: credentials and env var naming
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()

    # Accept both the correct and the incorrect-case env name so you stop backsliding.
    private_key_b64 = pick_env(
        "KALSHI_PRIVATE_KEY_PEM_BASE64",   # correct
        "KALSHI_PRIVATE_KEY_PEM_Base64",   # your current wrong one
        "KALSHI_PRIVATE_KEY_PEM_b64",      # fallback
    )

    # FIX 3: series prefix default should include the M for 15m BTC series
    series_prefix = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()

    poll_seconds = env_int("POLL_SECONDS", 60)
    enable_trading = env_bool("ENABLE_TRADING", False)

    max_daily_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "20").strip() or "20")  # % of total liquidity
    extreme_threshold = float(os.getenv("EXTREME_THRESHOLD", "0.95").strip() or "0.95")  # 95%

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", enable_trading)
    log.info("POLL_SECONDS=%s", poll_seconds)
    log.info("SERIES_PREFIX=%s", series_prefix)
    log.info("API_BASE=%s", api_base)
    log.info("EMAIL_ENABLED=%s", env_bool("EMAIL_ENABLED", False))

    if not api_key_id:
        raise RuntimeError("Missing KALSHI_API_KEY_ID")

    client = KalshiClient(api_base=api_base, api_key_id=api_key_id, private_key_pem_b64=private_key_b64)

    if not client.private_key:
        log.warning("Kalshi credentials missing/invalid private key -> running in READ-ONLY mode")

    # Daily loss tracking (simple)
    day_start_utc = datetime.datetime.now(datetime.timezone.utc).date()
    start_balance_cents = None

    while True:
        try:
            # reset daily balance baseline at UTC day boundary
            today_utc = datetime.datetime.now(datetime.timezone.utc).date()
            if today_utc != day_start_utc:
                day_start_utc = today_utc
                start_balance_cents = None
                log.info("New UTC trading day; resetting daily loss baseline.")

            # heartbeat time
            now_et = datetime.datetime.now(ET)
            boundary_et = next_15m_boundary_et(now_et)
            target_ticker = format_market_ticker(series_prefix, boundary_et)

            log.info("Heartbeat ET now=%s | next15=%s | ticker=%s",
                     now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                     boundary_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                     target_ticker)

            # Balance
            balance_cents = None
            if client.private_key:
                try:
                    balance_cents = client.get_balance_cents()
                    if isinstance(balance_cents, int):
                        if start_balance_cents is None:
                            start_balance_cents = balance_cents
                            log.info("Daily baseline balance: $%.2f", start_balance_cents / 100.0)
                        else:
                            pnl_cents = balance_cents - start_balance_cents
                            log.info("Balance: $%.2f | Daily PnL: $%.2f", balance_cents / 100.0, pnl_cents / 100.0)

                            # Hard daily loss limit
                            if start_balance_cents > 0:
                                drawdown_pct = (-pnl_cents) / start_balance_cents * 100.0 if pnl_cents < 0 else 0.0
                                if drawdown_pct >= max_daily_loss_pct:
                                    log.error("DAILY LOSS LIMIT HIT: %.2f%% >= %.2f%% -> DISABLING TRADING", drawdown_pct, max_daily_loss_pct)
                                    enable_trading = False
                except Exception as e:
                    log.warning("Balance fetch failed (continuing): %r", e)

            # Try direct market ticker first (fast)
            market = None
            try:
                market = client.get_market(target_ticker)
                log.info("Market resolved (direct): %s", target_ticker)
            except Exception as e:
                log.warning("Direct market fetch failed for %s: %r", target_ticker, e)

                # Fallback: list markets & try to find the next valid one
                try:
                    listing = client.list_markets(series_ticker=series_prefix, status="open", limit=200)
                    markets = listing.get("markets") or listing.get("data") or listing.get("results") or []
                    candidate = None

                    # Find something that starts at/after boundary by parsing ticker suffix (YYMONDDHHMM)
                    def parse_suffix(t: str) -> Optional[datetime.datetime]:
                        try:
                            suffix = t.split("-", 1)[1]  # 26JAN181700
                            yy = int(suffix[0:2]) + 2000
                            mon = suffix[2:5]
                            dd = int(suffix[5:7])
                            hh = int(suffix[7:9])
                            mm = int(suffix[9:11])
                            # parse month
                            mnum = datetime.datetime.strptime(mon, "%b").month
                            return datetime.datetime(yy, mnum, dd, hh, mm, tzinfo=ET)
                        except Exception:
                            return None

                    # Flatten markets list shapes
                    tickers: List[str] = []
                    if isinstance(markets, list):
                        for m in markets:
                            if isinstance(m, dict) and isinstance(m.get("ticker"), str):
                                tickers.append(m["ticker"])

                    # pick earliest ticker >= boundary
                    best_dt = None
                    for t in tickers:
                        dt = parse_suffix(t)
                        if not dt:
                            continue
                        if dt >= boundary_et:
                            if best_dt is None or dt < best_dt:
                                best_dt = dt
                                candidate = t

                    if candidate:
                        market = client.get_market(candidate)
                        log.info("Market resolved (fallback): %s", candidate)
                    else:
                        log.error("Could not resolve a valid ticker for next 15m boundary (%s).", boundary_et.isoformat())
                        market = None

                except Exception as e2:
                    log.error("Fallback market search failed: %r", e2)
                    market = None

            if not market:
                time.sleep(poll_seconds)
                continue

            resolved_ticker = market.get("ticker") if isinstance(market, dict) else None
            if not isinstance(resolved_ticker, str):
                resolved_ticker = target_ticker

            # Orderbook dominance check
            try:
                ob = client.get_orderbook(resolved_ticker)
                yes_shares, no_shares = orderbook_side_shares(ob)
                total = yes_shares + no_shares
                if total > 0:
                    yes_frac = yes_shares / total
                    no_frac = no_shares / total
                    log.info("Orderbook shares YES=%s NO=%s | YES%%=%.3f NO%%=%.3f",
                             yes_shares, no_shares, yes_frac, no_frac)

                    heavy_side = None
                    if yes_frac >= extreme_threshold:
                        heavy_side = "YES"
                    elif no_frac >= extreme_threshold:
                        heavy_side = "NO"

                    if heavy_side:
                        log.info("EXTREME detected: %.2f%%+ on %s side (threshold=%.2f%%)",
                                 100.0 * max(yes_frac, no_frac), heavy_side, 100.0 * extreme_threshold)
                        # Placeholder: your trade execution would go here.
                        # (I am intentionally not placing orders without your confirmed order endpoint + action fields.)
                else:
                    log.info("Orderbook empty or unparseable; skipping dominance check.")
            except Exception as e:
                log.warning("Orderbook fetch/parse failed: %r", e)

        except Exception as outer:
            log.error("LOOP ERROR: %r", outer)
            try:
                send_email("Kalshi bot error", repr(outer))
            except Exception:
                pass

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
import os
import json
import time
import base64
import logging
import datetime as dt
from dataclasses import dataclass
from urllib.parse import urlparse

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
log = logging.getLogger("kalshi-bot")

# ----------------------------
# Env helpers
# ----------------------------
def env_str(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or v.strip() == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return v.strip()

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v.strip())

def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return float(v.strip())

# ----------------------------
# Config
# ----------------------------
@dataclass
class Config:
    api_base: str
    api_key_id: str
    private_key_pem_b64: str

    series_prefix: str
    poll_seconds: int

    enable_trading: bool
    confirm_live_trading: bool

    trade_both_sides: bool
    contracts_per_side: int

    take_profit_cents: int          # profit target in cents per contract (small win farming)
    max_entry_cents: int            # only enter if price <= this
    max_daily_loss_cents: int       # stop trading if daily realized pnl <= -this

    # Email
    email_enabled: bool
    email_to: str | None
    smtp_host: str | None
    smtp_port: int | None
    smtp_user: str | None
    smtp_pass: str | None
    smtp_tls: bool

def load_config() -> Config:
    api_base = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
    return Config(
        api_base=api_base.rstrip("/"),
        api_key_id=env_str("KALSHI_API_KEY_ID"),
        private_key_pem_b64=env_str("KALSHI_PRIVATE_KEY_PEM_BASE64"),

        series_prefix=os.getenv("SERIES_PREFIX", "KXBTC15M").strip(),
        poll_seconds=env_int("POLL_SECONDS", 60),

        enable_trading=env_bool("ENABLE_TRADING", False),
        confirm_live_trading=env_bool("CONFIRM_LIVE_TRADING", False),

        trade_both_sides=env_bool("TRADE_BOTH_SIDES", True),
        contracts_per_side=env_int("CONTRACTS_PER_SIDE", 1),

        take_profit_cents=env_int("TAKE_PROFIT_CENTS", 1),
        max_entry_cents=env_int("MAX_ENTRY_CENTS", 50),
        max_daily_loss_cents=env_int("MAX_DAILY_LOSS_CENTS", 500),  # $5 default stop

        email_enabled=env_bool("EMAIL_ENABLED", False),
        email_to=os.getenv("EMAIL_TO"),
        smtp_host=os.getenv("SMTP_HOST"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")) if os.getenv("SMTP_PORT") else None,
        smtp_user=os.getenv("SMTP_USERNAME"),
        smtp_pass=os.getenv("SMTP_PASSWORD"),
        smtp_tls=env_bool("SMTP_TLS", True),
    )

# ----------------------------
# Kalshi Auth (RSA-PSS)
# ----------------------------
class KalshiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self._private_key = self._load_private_key(cfg.private_key_pem_b64)

    @staticmethod
    def _load_private_key(pem_b64: str):
        pem = base64.b64decode(pem_b64.encode("utf-8"))
        return serialization.load_pem_private_key(pem, password=None)

    @staticmethod
    def _ts_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, msg: bytes) -> str:
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode("utf-8")

    def _auth_headers(self, method: str, path_and_query: str, body: str) -> dict:
        ts = self._ts_ms()
        # Common Kalshi signing pattern: timestamp + method + path + body
        # IMPORTANT: sign ONLY the path (and query if present), NOT the full URL.
        msg = (ts + method.upper() + path_and_query + body).encode("utf-8")
        sig = self._sign(msg)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = f"{self.cfg.api_base}{path}"
        body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))
        # include query in the signed string if requests will send one
        if params:
            # requests will encode params; easiest deterministic: sign with the actual prepared path+query
            req = requests.Request(method.upper(), url, params=params, data=body_str)
            prep = self.session.prepare_request(req)
            parsed = urlparse(prep.url)
            path_and_query = parsed.path + (("?" + parsed.query) if parsed.query else "")
        else:
            path_and_query = path

        headers = self._auth_headers(method, path_and_query, body_str)
        resp = self.session.request(method.upper(), url, params=params, data=body_str, headers=headers, timeout=20)

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")

        if resp.text.strip() == "":
            return {}
        return resp.json()

    # ---- API convenience ----
    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: dict) -> dict:
        return self._request("POST", path, json_body=json_body)

# ----------------------------
# Trading / Strategy
# ----------------------------
STATE_FILE = "state.json"

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"daily": {"date": None, "realized_pnl_cents": 0}, "last_email_date": None}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def et_now():
    # Render is UTC; you’re in ET. Keep it simple without extra deps:
    # ET = UTC-5 in winter. (Good enough for now; DST will be off.)
    return dt.datetime.utcnow() - dt.timedelta(hours=5)

def reset_daily_if_needed(state: dict):
    today = et_now().date().isoformat()
    if state["daily"]["date"] != today:
        state["daily"] = {"date": today, "realized_pnl_cents": 0}
        save_state(state)

def assert_auth_ok(kc: KalshiClient):
    # Hit an authenticated endpoint early so we don't "pretend" we’re live.
    # Portfolio balance path in v2:
    kc.get("/trade-api/v2/portfolio/balance")

def resolve_next_market(kc: KalshiClient, series_prefix: str) -> dict:
    # Get open markets for a series (1 result expected if only 1 currently active)
    # Endpoint varies by spec; this matches Kalshi v2 style.
    data = kc.get("/trade-api/v2/markets", params={"series_ticker": series_prefix, "status": "open", "limit": 10})
    markets = data.get("markets", [])
    if not markets:
        raise RuntimeError(f"No open markets for series {series_prefix}")
    # pick the soonest close / first
    return markets[0]

def fetch_market(kc: KalshiClient, ticker: str) -> dict:
    return kc.get(f"/trade-api/v2/markets/{ticker}")

def place_order(kc: KalshiClient, market_ticker: str, side: str, action: str, price_cents: int, count: int) -> dict:
    # action: "buy" or "sell"
    # side: "yes" or "no"
    payload = {
        "ticker": market_ticker,
        "side": side,
        "action": action,
        "type": "limit",
        "price": price_cents,
        "count": count,
    }
    return kc.post("/trade-api/v2/portfolio/orders", payload)

def try_farm_side(kc: KalshiClient, cfg: Config, market: dict, side: str, state: dict):
    """
    Simple farming:
    - If best ask <= MAX_ENTRY_CENTS, buy 1 contract
    - Immediately place take-profit sell at entry + TAKE_PROFIT_CENTS
    """
    quotes = market.get("yes_ask"), market.get("yes_bid"), market.get("no_ask"), market.get("no_bid")
    if side == "yes":
        best_ask = market.get("yes_ask")
        best_bid = market.get("yes_bid")
    else:
        best_ask = market.get("no_ask")
        best_bid = market.get("no_bid")

    if best_ask is None or best_bid is None:
        log.warning("No quotes available for side=%s", side)
        return

    # If you're trying to farm wins, you need fills. We'll enter at ask if it's cheap enough.
    entry = int(best_ask)

    if entry > cfg.max_entry_cents:
        log.info("Skip %s: ask=%s > MAX_ENTRY_CENTS=%s", side, entry, cfg.max_entry_cents)
        return

    # Daily stop
    realized = int(state["daily"]["realized_pnl_cents"])
    if realized <= -cfg.max_daily_loss_cents:
        log.warning("DAILY STOP HIT: realized=%sc <= -%sc. Trading paused.", realized, cfg.max_daily_loss_cents)
        return

    if not (cfg.enable_trading and cfg.confirm_live_trading):
        log.info("[DRY RUN] Would BUY %s %s @%sc x%s", side.upper(), market["ticker"], entry, cfg.contracts_per_side)
        return

    # BUY
    buy_resp = place_order(kc, market["ticker"], side=side, action="buy", price_cents=entry, count=cfg.contracts_per_side)
    log.info("BUY placed: %s", buy_resp)

    # TAKE PROFIT SELL
    tp = min(99, entry + cfg.take_profit_cents)
    sell_resp = place_order(kc, market["ticker"], side=side, action="sell", price_cents=tp, count=cfg.contracts_per_side)
    log.info("TP SELL placed: %s", sell_resp)

def maybe_send_daily_email(cfg: Config, state: dict):
    if not cfg.email_enabled:
        return

    today = et_now().date().isoformat()
    # send once per day at/after 8:00pm ET
    now = et_now()
    if now.hour < 20:
        return
    if state.get("last_email_date") == today:
        return

    # Build message from our state (we can upgrade later to pull fills for exact P&L)
    pnl_cents = int(state["daily"]["realized_pnl_cents"])
    pnl = pnl_cents / 100.0
    subject = f"Kalshi Bot Daily P&L — {today}"
    body = f"""Kalshi Bot Daily Report ({today})

Realized P&L (approx): ${pnl:,.2f}

Notes:
- This version tracks realized P&L in state.json (upgrade later to reconcile fills exactly).
"""

    send_email(cfg, subject, body)
    state["last_email_date"] = today
    save_state(state)
    log.info("Daily email sent.")

def send_email(cfg: Config, subject: str, body: str):
    import smtplib
    from email.mime.text import MIMEText

    if not (cfg.smtp_host and cfg.smtp_port and cfg.smtp_user and cfg.smtp_pass and cfg.email_to):
        raise RuntimeError("Email enabled but SMTP/EMAIL_TO env vars are incomplete.")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.email_to

    server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
    try:
        if cfg.smtp_tls:
            server.starttls()
        server.login(cfg.smtp_user, cfg.smtp_pass)
        server.sendmail(cfg.smtp_user, [cfg.email_to], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass

# ----------------------------
# Main loop
# ----------------------------
def main():
    cfg = load_config()

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", cfg.enable_trading)
    log.info("CONFIRM_LIVE_TRADING=%s", cfg.confirm_live_trading)
    log.info("POLL_SECONDS=%s", cfg.poll_seconds)
    log.info("SERIES_PREFIX=%s", cfg.series_prefix)
    log.info("API_BASE=%s", cfg.api_base)

    kc = KalshiClient(cfg)
    state = load_state()

    # HARD auth check. If this fails, do not continue.
    try:
        assert_auth_ok(kc)
        log.info("Auth check OK (portfolio/balance).")
    except Exception as e:
        log.error("AUTH CHECK FAILED. This is why you see INCORRECT_API_KEY_SIGNATURE.\n%s", e)
        raise

    while True:
        reset_daily_if_needed(state)

        try:
            m0 = resolve_next_market(kc, cfg.series_prefix)
            ticker = m0.get("ticker") or m0.get("market_ticker") or m0.get("id")
            if not ticker:
                raise RuntimeError(f"Could not determine ticker from {m0}")

            market = fetch_market(kc, ticker)
            yes_bid = market.get("yes_bid")
            yes_ask = market.get("yes_ask")
            no_bid = market.get("no_bid")
            no_ask = market.get("no_ask")

            log.info("Heartbeat ET now=%s | market=%s | yes %s/%s no %s/%s",
                     et_now().strftime("%Y-%m-%d %H:%M:%S"),
                     market.get("ticker", ticker),
                     yes_bid, yes_ask, no_bid, no_ask)

            # FARM both sides (configurable)
            if cfg.trade_both_sides:
                try_farm_side(kc, cfg, market, "yes", state)
                try_farm_side(kc, cfg, market, "no", state)
            else:
                try_farm_side(kc, cfg, market, "yes", state)

            maybe_send_daily_email(cfg, state)

        except Exception as e:
            log.error("LOOP ERROR: %s", repr(e))

        time.sleep(cfg.poll_seconds)

if __name__ == "__main__":
    main()
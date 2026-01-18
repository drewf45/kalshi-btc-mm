import os
import time
import json
import base64
import random
import smtplib
import traceback
from dataclasses import dataclass
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# =========================
# Helpers
# =========================

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v)

def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return float(v)

def now_utc_ms() -> str:
    return str(int(datetime.now(timezone.utc).timestamp() * 1000))

def fmt_ticker(series_prefix: str, dt_utc: datetime) -> str:
    """
    Kalshi ticker format you’re using:
    KXBTC15M-18JAN261900  (UTC time, no seconds)
    """
    dt_utc = dt_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)
    dd = f"{dt_utc.day:02d}"
    mon = dt_utc.strftime("%b").upper()  # JAN
    yy = dt_utc.strftime("%y")           # 26
    hhmm = dt_utc.strftime("%H%M")       # 1900
    return f"{series_prefix}-{dd}{mon}{yy}{hhmm}"

def floor_to_15(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    m = (dt.minute // 15) * 15
    return dt.replace(minute=m)

def ceil_to_next_15(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    floored = floor_to_15(dt)
    if floored == dt:
        return dt
    return floored + timedelta(minutes=15)

def sanitize_pem_text(pem_text: str) -> bytes:
    """
    IMPORTANT: Your error showed the PEM being shoved into an HTTP header.
    We DO NOT do that.
    We only load it as a private key to sign requests.

    This function also fixes common Render env mistakes where '\n' is stored literally.
    """
    pem_text = pem_text.strip()
    # Convert literal "\n" sequences into real newlines
    pem_text = pem_text.replace("\\n", "\n")
    return pem_text.encode("utf-8")


# =========================
# Email notifications (SMTP)
# =========================

@dataclass
class EmailConfig:
    enabled: bool
    host: str
    port: int
    tls: bool
    username: str
    password: str
    to_addr: str
    from_addr: str

def load_email_config() -> EmailConfig:
    enabled = env_bool("EMAIL_ENABLED", False)
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = env_int("SMTP_PORT", 587)
    tls = env_bool("SMTP_TLS", True)
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    to_addr = os.getenv("EMAIL_TO", "")
    from_addr = os.getenv("EMAIL_FROM", username or to_addr or "")
    return EmailConfig(enabled, host, port, tls, username, password, to_addr, from_addr)

def send_email(cfg: EmailConfig, subject: str, body: str) -> None:
    if not cfg.enabled:
        return
    if not cfg.host or not cfg.port or not cfg.username or not cfg.password or not cfg.to_addr:
        print("EMAIL: enabled but missing SMTP_* or EMAIL_TO. Skipping.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to_addr
    msg.set_content(body)

    try:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as s:
            s.ehlo()
            if cfg.tls:
                s.starttls()
                s.ehlo()
            s.login(cfg.username, cfg.password)
            s.send_message(msg)
        print("EMAIL: sent:", subject)
    except Exception as e:
        # Don’t crash the bot if email fails.
        print("EMAIL FAILED:", repr(e))


# =========================
# Kalshi Auth + API Client
# =========================

class KalshiClient:
    def __init__(self, base_url: str, api_key_id: str, private_key):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.session = requests.Session()

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        # Per Kalshi docs: sign timestamp + HTTP_METHOD + path_without_query
        path_no_query = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path_no_query}".encode("utf-8")
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        ts = now_utc_ms()
        sig = self._sign(ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        headers = self._headers("GET", path)
        r = self.session.get(url, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict) -> dict:
        url = self.base_url + path
        headers = self._headers("POST", path)
        r = self.session.post(url, headers=headers, data=json.dumps(payload), timeout=20)
        r.raise_for_status()
        return r.json()


def load_private_key():
    """
    Supports either:
      - KALSHI_PRIVATE_KEY_PEM_B64  (base64 of the PEM)
      - KALSHI_PRIVATE_KEY_PEM_TEXT (raw PEM text, possibly with \n)
    """
    pem_b64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_B64", "")
    pem_text = os.getenv("KALSHI_PRIVATE_KEY_PEM_TEXT", "")

    if pem_b64.strip():
        pem_bytes = base64.b64decode(pem_b64.strip())
    elif pem_text.strip():
        pem_bytes = sanitize_pem_text(pem_text)
    else:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_B64 or KALSHI_PRIVATE_KEY_PEM_TEXT")

    return serialization.load_pem_private_key(pem_bytes, password=None)


# =========================
# Trading logic (simple / configurable)
# =========================

@dataclass
class Config:
    base_url: str
    series_prefix: str
    market_ticker_override: str
    poll_seconds: int
    market_refresh_seconds: int

    dry_run: bool
    enable_trading: bool

    bet_dollars: float
    min_contracts: int
    min_price_cents: int
    max_price_cents: int

    wait_for_open: bool
    open_delay_min_s: int
    open_delay_max_s: int

    startup_test_bet: bool
    stop_after_wagers: int
    max_daily_loss_dollars: float

    # scanning
    use_next_quarter_hour_end: bool
    scan_intervals: int  # how far forward to look for liquidity (15m per step)

    # email
    email_cfg: EmailConfig


def load_config() -> Config:
    base_url = os.getenv("BASE_URL", "https://api.elections.kalshi.com").rstrip("/")
    series_prefix = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()

    # IMPORTANT:
    # If MARKET_TICKER_OVERRIDE is set to a specific ticker, it WILL force that market.
    # To use auto-selection, set it empty OR "auto".
    market_ticker_override = os.getenv("MARKET_TICKER_OVERRIDE", "auto").strip()

    poll_seconds = env_int("POLL_SECONDS", 60)
    market_refresh_seconds = env_int("MARKET_REFRESH_SECONDS", 30)

    dry_run = env_bool("DRY_RUN", True)
    enable_trading = env_bool("ENABLE_TRADING", False)

    bet_dollars = env_float("BET_DOLLARS", 1.0)
    min_contracts = env_int("MIN_CONTRACTS", 1)
    min_price_cents = env_int("MIN_PRICE_CENTS", 5)
    max_price_cents = env_int("MAX_PRICE_CENTS", 95)

    wait_for_open = env_bool("WAIT_FOR_OPEN", True)

    # Example env: OPEN_DELAY_RANGE="3-15"
    delay_range = os.getenv("OPEN_DELAY_RANGE", "3-15")
    try:
        a, b = delay_range.split("-")
        open_delay_min_s = int(a.strip())
        open_delay_max_s = int(b.strip())
    except Exception:
        open_delay_min_s, open_delay_max_s = 3, 15

    startup_test_bet = env_bool("STARTUP_TEST_BET", False)
    stop_after_wagers = env_int("STOP_AFTER_WAGERS", 999999)
    max_daily_loss_dollars = env_float("MAX_DAILY_LOSS", 999999.0)

    use_next_quarter_hour_end = env_bool("USE_NEXT_QUARTER_HOUR_END", True)
    scan_intervals = env_int("SCAN_INTERVALS", 12)  # 12 * 15m = 3 hours forward

    email_cfg = load_email_config()

    return Config(
        base_url=base_url,
        series_prefix=series_prefix,
        market_ticker_override=market_ticker_override,
        poll_seconds=poll_seconds,
        market_refresh_seconds=market_refresh_seconds,
        dry_run=dry_run,
        enable_trading=enable_trading,
        bet_dollars=bet_dollars,
        min_contracts=min_contracts,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
        wait_for_open=wait_for_open,
        open_delay_min_s=open_delay_min_s,
        open_delay_max_s=open_delay_max_s,
        startup_test_bet=startup_test_bet,
        stop_after_wagers=stop_after_wagers,
        max_daily_loss_dollars=max_daily_loss_dollars,
        use_next_quarter_hour_end=use_next_quarter_hour_end,
        scan_intervals=scan_intervals,
        email_cfg=email_cfg,
    )


def get_orderbook(client: KalshiClient, ticker: str) -> dict:
    # Docs: /trade-api/v2/markets/{ticker}/orderbook
    return client.get(f"/trade-api/v2/markets/{ticker}/orderbook")


def parse_best_prices(ob: dict) -> tuple[int | None, int | None]:
    """
    Returns best_yes_ask_cents, best_no_ask_cents
    Kalshi orderbook structures can vary by endpoint version.
    We handle the common pattern: ob["orderbook"]["yes"] etc. or lists of levels.
    """
    orderbook = ob.get("orderbook") or ob

    def best_ask(side_obj):
        # side_obj might be dict with "asks": [[price, qty], ...] or list already
        if side_obj is None:
            return None
        asks = side_obj.get("asks") if isinstance(side_obj, dict) else None
        if asks is None and isinstance(side_obj, dict):
            # Sometimes nested: {"yes": {"buy":..., "sell":...}}
            asks = side_obj.get("sell")
        if asks is None and isinstance(side_obj, list):
            asks = side_obj
        if not asks:
            return None
        # expect [price, qty]
        try:
            return int(asks[0][0])
        except Exception:
            return None

    # common: {"orderbook": {"yes": {"asks":[...],...}, "no": {...}}}
    yes_obj = orderbook.get("yes") if isinstance(orderbook, dict) else None
    no_obj = orderbook.get("no") if isinstance(orderbook, dict) else None

    best_yes = best_ask(yes_obj)
    best_no = best_ask(no_obj)
    return best_yes, best_no


def choose_candidate_times(cfg: Config) -> list[datetime]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    # If you want “next quarter hour END”, we try the next boundary first.
    # This matters because at 2:04pm EST (19:04 UTC), the *next* 15m market is typically 19:15, not 19:00.
    if cfg.use_next_quarter_hour_end:
        start = ceil_to_next_15(now)
    else:
        start = floor_to_15(now)

    times = []
    for i in range(cfg.scan_intervals + 1):
        times.append(start + timedelta(minutes=15 * i))
    return times


def find_liquid_market(client: KalshiClient, cfg: Config) -> tuple[str, dict] | tuple[None, None]:
    # Forced override
    if cfg.market_ticker_override and cfg.market_ticker_override.lower() != "auto":
        t = cfg.market_ticker_override.strip()
        ob = get_orderbook(client, t)
        return t, ob

    # Auto scan forward for a market with asks present
    for dt in choose_candidate_times(cfg):
        ticker = fmt_ticker(cfg.series_prefix, dt)
        print(f"CHECKING MARKET: {ticker}")
        try:
            ob = get_orderbook(client, ticker)
            best_yes, best_no = parse_best_prices(ob)
            if best_yes is not None or best_no is not None:
                return ticker, ob
            print("No liquidity yet")
        except requests.HTTPError as e:
            # Market might not exist yet
            print(f"Market not ready / error for {ticker}: {e}")
        except Exception as e:
            print(f"Error reading orderbook for {ticker}: {repr(e)}")
        time.sleep(0.25)

    return None, None


def place_order(client: KalshiClient, ticker: str, side: str, price_cents: int, contracts: int) -> dict:
    """
    Minimal create order call.
    Endpoint per Kalshi docs: /trade-api/v2/portfolio/orders

    Payload shape can evolve; this is the most common pattern.
    If your account expects different fields, tell me what the API returns on error and I’ll adjust.
    """
    payload = {
        "ticker": ticker,
        "action": "buy",
        "side": side.lower(),           # "yes" or "no"
        "type": "limit",
        "price": int(price_cents),      # cents
        "count": int(contracts),
        "client_order_id": f"bot-{int(time.time())}-{random.randint(1000,9999)}",
    }
    return client.post("/trade-api/v2/portfolio/orders", payload)


def compute_contracts(cfg: Config, price_cents: int) -> int:
    # Rough sizing: dollars / (cents/100) = contracts
    if price_cents <= 0:
        return cfg.min_contracts
    contracts = int(cfg.bet_dollars / (price_cents / 100.0))
    return max(cfg.min_contracts, contracts)


def main():
    cfg = load_config()

    # Print config summary (no secrets)
    print("=== BOT STARTED ===")
    print(f"BASE_URL={cfg.base_url}/trade-api/v2")
    print(f"DRY_RUN={cfg.dry_run} ENABLE_TRADING={cfg.enable_trading} POLL_SECONDS={cfg.poll_seconds} MARKET_REFRESH_SECONDS={cfg.market_refresh_seconds}")
    print(f"SERIES_PREFIX={cfg.series_prefix} MARKET_TICKER_OVERRIDE={cfg.market_ticker_override or '(empty)'} USE_NEXT_QUARTER_HOUR_END={cfg.use_next_quarter_hour_end}")
    print(f"BET_DOLLARS=${cfg.bet_dollars} MIN_CONTRACTS={cfg.min_contracts} MIN_PRICE_CENTS={cfg.min_price_cents} MAX_PRICE_CENTS={cfg.max_price_cents}")
    print(f"WAIT_FOR_OPEN={cfg.wait_for_open} OPEN_DELAY_RANGE={cfg.open_delay_min_s}-{cfg.open_delay_max_s}s")
    print(f"STARTUP_TEST_BET={cfg.startup_test_bet} STOP_AFTER_WAGERS={cfg.stop_after_wagers}")
    print(f"EMAIL_ENABLED={cfg.email_cfg.enabled}")

    # Send startup email
    send_email(cfg.email_cfg, "Kalshi BTC Bot: STARTED", f"Bot started at {datetime.now(timezone.utc).isoformat()} UTC\nDRY_RUN={cfg.dry_run} ENABLE_TRADING={cfg.enable_trading}")

    # Load keys
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    if not api_key_id:
        raise RuntimeError("Missing KALSHI_API_KEY_ID env var")

    private_key = load_private_key()
    client = KalshiClient(cfg.base_url, api_key_id, private_key)

    wagers_placed = 0
    last_market_check = 0.0
    current_ticker = None

    # “market open trigger” state:
    market_open_seen = False
    market_open_time = None

    while wagers_placed < cfg.stop_after_wagers:
        try:
            now = time.time()

            # Refresh which market we are tracking periodically
            if (now - last_market_check) >= cfg.market_refresh_seconds or current_ticker is None:
                last_market_check = now
                ticker, ob = find_liquid_market(client, cfg)
                if ticker is None:
                    # nothing liquid found; wait and try again
                    time.sleep(cfg.poll_seconds)
                    continue

                # If market changed, announce it
                if ticker != current_ticker:
                    current_ticker = ticker
                    market_open_seen = False
                    market_open_time = None
                    msg = f"Tracking market: {current_ticker}"
                    print(msg)
                    send_email(cfg.email_cfg, "Kalshi BTC Bot: Market Selected", msg)

                # We already have liquidity if find_liquid_market returned it:
                best_yes, best_no = parse_best_prices(ob)
                if not market_open_seen and (best_yes is not None or best_no is not None):
                    market_open_seen = True
                    market_open_time = datetime.now(timezone.utc)
                    delay = random.randint(cfg.open_delay_min_s, cfg.open_delay_max_s) if cfg.wait_for_open else 0
                    print(f"MARKET OPEN (liquidity detected). Delay={delay}s before any trade.")
                    send_email(cfg.email_cfg, "Kalshi BTC Bot: Liquidity Detected", f"{current_ticker}\nbest_yes={best_yes} best_no={best_no}\nDelay={delay}s")
                    if delay > 0:
                        time.sleep(delay)

                    # Optional: one-time test bet right after first liquidity detection
                    if cfg.startup_test_bet and wagers_placed == 0:
                        # pick whichever side is available cheapest within bounds
                        chosen_side = None
                        chosen_price = None
                        if best_yes is not None and cfg.min_price_cents <= best_yes <= cfg.max_price_cents:
                            chosen_side = "yes"
                            chosen_price = best_yes
                        elif best_no is not None and cfg.min_price_cents <= best_no <= cfg.max_price_cents:
                            chosen_side = "no"
                            chosen_price = best_no

                        if chosen_side and chosen_price is not None:
                            contracts = compute_contracts(cfg, chosen_price)
                            if cfg.dry_run or not cfg.enable_trading:
                                print(f"DRY_RUN TEST BET: would BUY {chosen_side.upper()} {contracts} @ {chosen_price}c on {current_ticker}")
                                send_email(cfg.email_cfg, "Kalshi BTC Bot: DRY_RUN Test Bet", f"{current_ticker}\nBUY {chosen_side} {contracts} @ {chosen_price}c")
                            else:
                                resp = place_order(client, current_ticker, chosen_side, chosen_price, contracts)
                                print("ORDER PLACED (test):", resp)
                                send_email(cfg.email_cfg, "Kalshi BTC Bot: Test Bet Placed", f"{current_ticker}\nBUY {chosen_side} {contracts} @ {chosen_price}c\n{json.dumps(resp)[:1500]}")
                                wagers_placed += 1
                        else:
                            print("Startup test bet skipped (no price within bounds).")

            # If we have a market, poll its orderbook
            if current_ticker:
                ob = get_orderbook(client, current_ticker)
                best_yes, best_no = parse_best_prices(ob)

                if best_yes is None and best_no is None:
                    print("No liquidity yet")
                    time.sleep(cfg.poll_seconds)
                    continue

                # Simple “cheap entry” logic (NOT a guaranteed win — just threshold based)
                decision = None
                price = None

                if best_yes is not None and cfg.min_price_cents <= best_yes <= cfg.max_price_cents:
                    decision = "yes"
                    price = best_yes
                elif best_no is not None and cfg.min_price_cents <= best_no <= cfg.max_price_cents:
                    decision = "no"
                    price = best_no

                if decision and price is not None and cfg.enable_trading:
                    contracts = compute_contracts(cfg, price)
                    if cfg.dry_run:
                        print(f"DRY_RUN: would BUY {decision.upper()} {contracts} @ {price}c on {current_ticker} (best_yes={best_yes}, best_no={best_no})")
                        send_email(cfg.email_cfg, "Kalshi BTC Bot: DRY_RUN Order", f"{current_ticker}\nBUY {decision} {contracts} @ {price}c\nbest_yes={best_yes} best_no={best_no}")
                    else:
                        resp = place_order(client, current_ticker, decision, price, contracts)
                        print("ORDER PLACED:", resp)
                        send_email(cfg.email_cfg, "Kalshi BTC Bot: Order Placed", f"{current_ticker}\nBUY {decision} {contracts} @ {price}c\n{json.dumps(resp)[:1500]}")
                        wagers_placed += 1
                else:
                    # No trade this cycle
                    print(f"CHECKING MARKET: {current_ticker} (best_yes={best_yes} best_no={best_no})")

            time.sleep(cfg.poll_seconds)

        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            print("ERROR:", err)
            send_email(cfg.email_cfg, "Kalshi BTC Bot: ERROR", err[:1800])
            time.sleep(10)


if __name__ == "__main__":
    main()
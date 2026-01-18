import os
import json
import time
import base64
import uuid
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# =========================
# Config
# =========================

BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com").rstrip("/")
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M")  # You said: 15-min BTC only.
USE_NEXT_QUARTER_HOUR_END = os.getenv("USE_NEXT_QUARTER_HOUR_END", "True").lower() in ("1", "true", "yes")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").lower() in ("1", "true", "yes")
DRY_RUN = os.getenv("DRY_RUN", "False").lower() in ("1", "true", "yes")  # keep False if you really want live
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Strategy/Risk
MIN_SKEW = float(os.getenv("MIN_SKEW", "0.95"))  # "95% of market on one side"
BET_FRACTION = float(os.getenv("BET_FRACTION", "0.01"))  # 1% of liquidity per bet
DAILY_DRAWDOWN_LIMIT = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.20"))  # stop if lose 20% in a day
MIN_PRICE_CENTS = int(os.getenv("MIN_PRICE_CENTS", "1"))
MAX_PRICE_CENTS = int(os.getenv("MAX_PRICE_CENTS", "99"))
MICRO_PROFIT_CENTS = int(os.getenv("MICRO_PROFIT_CENTS", "1"))  # your "1 cent win is fine"
MAX_OPEN_ORDERS = int(os.getenv("MAX_OPEN_ORDERS", "3"))

# Storage for daily state
STATE_PATH = os.getenv("STATE_PATH", "state.json")

# Email settings (optional)
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "True").lower() in ("1", "true", "yes")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")  # where to send notifications
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USERNAME)  # usually same as username

# IMPORTANT: choose SMTP mode correctly:
# - Port 587 -> STARTTLS
# - Port 465 -> SSL
SMTP_MODE = os.getenv("SMTP_MODE", "").strip().upper()  # "STARTTLS" or "SSL" or empty => auto


# =========================
# Logging helpers
# =========================

def log(msg: str, level: str = "INFO") -> None:
    levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
    if levels.index(level) >= levels.index(LOG_LEVEL):
        print(msg, flush=True)


# =========================
# Email notifier
# =========================

def send_email(subject: str, body: str) -> None:
    if not EMAIL_ENABLED:
        return
    if not (SMTP_HOST and SMTP_PORT and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_TO):
        log("Email not configured (missing SMTP_* or EMAIL_TO). Skipping email.", "WARNING")
        return

    import smtplib
    from email.message import EmailMessage

    mode = SMTP_MODE
    if not mode:
        mode = "SSL" if SMTP_PORT == 465 else "STARTTLS"

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM or SMTP_USERNAME
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if mode == "SSL":
            # Correct for port 465
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            # Correct for port 587
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        log(f"Email sent: {subject}", "INFO")
    except Exception as e:
        # This is where your "SSL WRONG VERSION NUMBER" was coming from.
        log(f"EMAIL FAILED: {repr(e)}", "ERROR")


def notify(event: str, details: str) -> None:
    log(f"NOTIFY: {event} | {details}", "INFO")
    send_email(f"[kalshi-btc-mm] {event}", details)


# =========================
# Kalshi auth (docs-correct)
# =========================

def load_private_key_from_env() -> Optional[Any]:
    """
    Expect env var with BASE64-encoded PEM (recommended) to avoid newline issues in Render.
    Supported names (because you've changed them a few times):
      - KALSHI_PRIVATE_KEY_PEM_B64
      - KALSHI_PRIVATE_KEY_B64
      - PRIVATE_KEY_B64
    """
    b64 = (
        os.getenv("KALSHI_PRIVATE_KEY_PEM_B64")
        or os.getenv("KALSHI_PRIVATE_KEY_B64")
        or os.getenv("PRIVATE_KEY_B64")
        or ""
    ).strip()

    if not b64:
        return None

    # Remove accidental whitespace/newlines in base64
    b64 = "".join(b64.split())

    try:
        pem = base64.b64decode(b64)
        key = serialization.load_pem_private_key(pem, password=None)
        return key
    except Exception as e:
        log(f"ERROR: Failed to decode/load private key from base64: {repr(e)}", "ERROR")
        return None


def create_signature(private_key: Any, timestamp_ms: str, method: str, path: str) -> str:
    """
    Per Kalshi docs: message = timestamp + method + path
    Signature = RSA-PSS(SHA256), base64-encoded.
    """
    message = (timestamp_ms + method.upper() + path).encode("utf-8")
    sig = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def kalshi_headers(private_key: Any, api_key_id: str, method: str, path: str) -> Dict[str, str]:
    timestamp_ms = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))
    signature = create_signature(private_key, timestamp_ms, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Content-Type": "application/json",
    }


def request(method: str, path: str, *, auth: bool = False, json_body: Optional[dict] = None) -> Dict[str, Any]:
    url = BASE_URL + path
    method_u = method.upper()

    headers = {}
    if auth:
        api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
        if not api_key_id:
            raise RuntimeError("Missing KALSHI_API_KEY_ID env var")

        private_key = load_private_key_from_env()
        if private_key is None:
            raise RuntimeError("Missing/invalid private key env var (KALSHI_PRIVATE_KEY_PEM_B64)")
        headers = kalshi_headers(private_key, api_key_id, method_u, path)

    resp = requests.request(method_u, url, headers=headers, json=json_body, timeout=20)

    if resp.status_code >= 400:
        # NEVER include the PEM/private key in logs. Only show response text.
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:500]}")
    return resp.json()


# =========================
# Market ticker utilities
# =========================

MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

def format_kalshi_ticker_time(t: dt.datetime) -> str:
    """
    Format: DDMONYYHHMM (no seconds). Must be UTC.
    Example: 18JAN261930
    """
    t = t.astimezone(dt.timezone.utc)
    dd = f"{t.day:02d}"
    mon = MONTHS[t.month - 1]
    yy = f"{t.year % 100:02d}"
    hh = f"{t.hour:02d}"
    mm = f"{t.minute:02d}"
    return f"{dd}{mon}{yy}{hh}{mm}"


def next_quarter_hour_end(now_utc: dt.datetime) -> dt.datetime:
    """
    Ceil to the next 15-min boundary (end time). E.g. 19:02 -> 19:15, 19:16 -> 19:30, etc.
    """
    now_utc = now_utc.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    minutes = now_utc.minute
    next_block = ((minutes // 15) + 1) * 15
    if next_block == 60:
        return (now_utc.replace(minute=0) + dt.timedelta(hours=1))
    return now_utc.replace(minute=next_block)


def build_market_ticker() -> str:
    """
    Builds your desired 15-min BTC contract ticker.
    Uses UTC and no seconds (fixes the "seconds in ticker" failure).
    """
    override = os.getenv("MARKET_TICKER_OVERRIDE", "").strip()
    if override:
        return override

    now_utc = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    end = next_quarter_hour_end(now_utc) if USE_NEXT_QUARTER_HOUR_END else now_utc
    return f"{SERIES_PREFIX}-{format_kalshi_ticker_time(end)}"


# =========================
# Trading helpers
# =========================

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"WARNING: could not save state: {repr(e)}", "WARNING")

def today_key_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

def ensure_daily_state(state: Dict[str, Any], balance_cents: int) -> Dict[str, Any]:
    k = today_key_utc()
    if state.get("day") != k:
        state = {
            "day": k,
            "start_balance_cents": balance_cents,
            "realized_pnl_cents": 0,
            "trades": 0,
            "last_market": "",
        }
    return state

def get_balance_cents() -> int:
    """
    Tries Kalshi portfolio balance endpoint.
    If this endpoint name differs on your account, change it here ONLY.
    """
    # Per Kalshi v2 portfolio endpoints, balance is available via /portfolio/balance (common).
    # If your API uses a different path, update this one string.
    data = request("GET", "/trade-api/v2/portfolio/balance", auth=True)
    # Expected: {"balance": {"available": <cents>, ...}} or similar.
    bal = data.get("balance") or {}
    available = bal.get("available")
    if available is None:
        # fall back to 0 to avoid blowing up
        return 0
    return int(available)

def get_open_orders() -> List[Dict[str, Any]]:
    data = request("GET", "/trade-api/v2/portfolio/orders?status=open&limit=50", auth=True)
    return data.get("orders", []) or []

def place_limit_order(ticker: str, side: str, price_cents: int, count: int) -> Dict[str, Any]:
    """
    side: "yes" or "no"
    price field: yes_price or no_price
    """
    price_cents = max(MIN_PRICE_CENTS, min(MAX_PRICE_CENTS, int(price_cents)))
    count = max(1, int(count))

    order = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": count,
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
    }
    if side == "yes":
        order["yes_price"] = price_cents
    else:
        order["no_price"] = price_cents

    if DRY_RUN or (not ENABLE_TRADING):
        log(f"DRY_RUN/Trading disabled: would place order {order}", "INFO")
        return {"order": order, "dry_run": True}

    resp = request("POST", "/trade-api/v2/portfolio/orders", auth=True, json_body=order)
    return resp

def get_orderbook(ticker: str) -> Dict[str, Any]:
    return request("GET", f"/trade-api/v2/markets/{ticker}/orderbook", auth=False)

def parse_orderbook_skew(ob: Dict[str, Any], depth: int = 10) -> Tuple[float, float, int, int]:
    """
    Kalshi docs: orderbook gives bids for 'yes' and 'no'.
    We'll use top N levels and compute:
      yes_qty = sum(qty)
      no_qty  = sum(qty)
      share_yes = yes_qty / (yes_qty + no_qty)
    Returns: (share_yes, share_no, best_yes_bid, best_no_bid)
    """
    yes_bids = ob.get("orderbook", {}).get("yes") or []
    no_bids = ob.get("orderbook", {}).get("no") or []

    def top_qty(levels):
        total = 0
        for lvl in levels[:depth]:
            # each level commonly [price, count]
            if isinstance(lvl, list) and len(lvl) >= 2:
                total += int(lvl[1])
        return total

    yes_qty = top_qty(yes_bids)
    no_qty = top_qty(no_bids)

    total = yes_qty + no_qty
    share_yes = (yes_qty / total) if total > 0 else 0.5
    share_no = 1.0 - share_yes

    best_yes = int(yes_bids[0][0]) if (len(yes_bids) > 0 and len(yes_bids[0]) >= 1) else 0
    best_no = int(no_bids[0][0]) if (len(no_bids) > 0 and len(no_bids[0]) >= 1) else 0

    return share_yes, share_no, best_yes, best_no

def implied_ask_from_bids(best_opposite_bid: int) -> int:
    """
    If best NO bid is X, implied YES ask is 100 - X.
    If best YES bid is X, implied NO ask is 100 - X.
    """
    if best_opposite_bid <= 0:
        return 99
    return max(1, min(99, 100 - best_opposite_bid))

def compute_bet_size(balance_cents: int, entry_price_cents: int) -> int:
    """
    You said: bet should be 1% of total liquidity.
    Approx cost = count * entry_price_cents
    count = floor( (balance * BET_FRACTION) / price )
    """
    bankroll = int(balance_cents * BET_FRACTION)
    if bankroll <= 0:
        return 1
    count = bankroll // max(1, entry_price_cents)
    return max(1, int(count))


# =========================
# Main loop
# =========================

def main() -> None:
    log("=== BOT STARTED ===", "INFO")
    log(f"BASE_URL={BASE_URL}", "INFO")
    log(f"ENABLE_TRADING={ENABLE_TRADING}", "INFO")
    log(f"POLL_SECONDS={POLL_SECONDS}", "INFO")
    log(f"SERIES_PREFIX={SERIES_PREFIX}", "INFO")

    # Credential check (don’t go backwards into read-only unexpectedly)
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    private_key_ok = load_private_key_from_env() is not None

    if not api_key_id or not private_key_ok:
        log("WARNING: Kalshi credentials missing/invalid -> READ-ONLY mode (no trades).", "WARNING")
        notify("READ-ONLY MODE", "Kalshi creds missing/invalid. Check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM_B64.")
    else:
        log("Kalshi credentials present (will trade only if ENABLE_TRADING=True).", "INFO")

    # Email quick sanity
    if EMAIL_ENABLED:
        log(f"Email enabled. SMTP={SMTP_HOST}:{SMTP_PORT} mode={(SMTP_MODE or 'AUTO')}", "INFO")

    # Load / init state
    state = load_state()

    while True:
        try:
            ticker = build_market_ticker()
            log(f"CHECKING MARKET: {ticker}", "INFO")

            # Check balance if possible (auth); if not, degrade gracefully
            balance_cents = 0
            if api_key_id and private_key_ok:
                try:
                    balance_cents = get_balance_cents()
                except Exception as e:
                    log(f"Balance fetch failed (continuing): {repr(e)}", "WARNING")

            state = ensure_daily_state(state, balance_cents)
            state["last_market"] = ticker
            save_state(state)

            # Daily drawdown stop
            start_bal = int(state.get("start_balance_cents") or 0)
            realized = int(state.get("realized_pnl_cents") or 0)
            # If we can’t get a balance, don’t hard-stop; just be safe.
            if start_bal > 0:
                dd = -realized / start_bal if realized < 0 else 0.0
                if dd >= DAILY_DRAWDOWN_LIMIT:
                    msg = f"Daily drawdown stop hit: realized={realized}c start={start_bal}c (dd={dd:.2%}). Trading paused."
                    log(msg, "ERROR")
                    notify("DAILY STOP HIT", msg)
                    time.sleep(POLL_SECONDS)
                    continue

            # Fetch orderbook (public)
            ob = get_orderbook(ticker)

            share_yes, share_no, best_yes_bid, best_no_bid = parse_orderbook_skew(ob)
            if (best_yes_bid == 0 and best_no_bid == 0):
                log("No liquidity yet", "INFO")
                time.sleep(POLL_SECONDS)
                continue

            log(f"Orderbook: best_yes_bid={best_yes_bid} best_no_bid={best_no_bid} skew_yes={share_yes:.2%} skew_no={share_no:.2%}", "INFO")

            # Don’t spam orders
            if api_key_id and private_key_ok:
                try:
                    open_orders = get_open_orders()
                    if len(open_orders) >= MAX_OPEN_ORDERS:
                        log(f"Open orders >= {MAX_OPEN_ORDERS}. Waiting.", "WARNING")
                        time.sleep(POLL_SECONDS)
                        continue
                except Exception as e:
                    log(f"Open orders fetch failed (continuing): {repr(e)}", "WARNING")

            # Strategy trigger: "95% of market on one side" (using depth qty share)
            if max(share_yes, share_no) < MIN_SKEW:
                time.sleep(POLL_SECONDS)
                continue

            heavy_side = "yes" if share_yes >= share_no else "no"
            contrarian_side = "no" if heavy_side == "yes" else "yes"

            # Determine entry price using implied ask from opposite bids
            if contrarian_side == "yes":
                entry = implied_ask_from_bids(best_no_bid)  # buy YES => implied ask from NO bid
            else:
                entry = implied_ask_from_bids(best_yes_bid)  # buy NO => implied ask from YES bid

            entry = max(MIN_PRICE_CENTS, min(MAX_PRICE_CENTS, entry))

            # Size order: 1% of liquidity
            count = compute_bet_size(balance_cents, entry) if balance_cents > 0 else 1

            msg = f"Trigger: heavy={heavy_side} ({max(share_yes,share_no):.2%}) -> buying {contrarian_side} at {entry}c x{count}"
            log(msg, "INFO")
            notify("TRADE SIGNAL", msg)

            # Place order
            if api_key_id and private_key_ok and ENABLE_TRADING:
                resp = place_limit_order(ticker, contrarian_side, entry, count)
                log(f"Order response: {resp}", "INFO")
                state["trades"] = int(state.get("trades") or 0) + 1
                save_state(state)
            else:
                log("Trading disabled or creds missing; not placing order.", "WARNING")

            time.sleep(POLL_SECONDS)

        except Exception as e:
            err = f"Loop error: {repr(e)}"
            log(err, "ERROR")
            notify("BOT ERROR", err)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
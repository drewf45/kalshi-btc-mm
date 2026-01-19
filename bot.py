import os
import time
import json
import uuid
import base64
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

# --- crypto signing (RSA-PSS) ---
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ----------------- CONFIG -----------------
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# Strategy
DOMINANCE_THRESHOLD = float(os.getenv("DOMINANCE_THRESHOLD", "0.95"))  # 95%
BET_FRACTION_OF_CASH = float(os.getenv("BET_FRACTION_OF_CASH", "0.01"))  # 1% per trade
DAILY_DRAWDOWN_LIMIT = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.20"))  # stop at -20%
TAKE_PROFIT_CENTS = int(os.getenv("TAKE_PROFIT_CENTS", "1"))  # +1 cent
MAX_TRADES_PER_MARKET = int(os.getenv("MAX_TRADES_PER_MARKET", "1"))

# Email daily summary
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_REPORT_HOUR_ET = int(os.getenv("EMAIL_REPORT_HOUR_ET", "20"))  # 8pm ET default

# Auth
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY", "")

# PRIVATE_KEY may be stored with literal "\n" in Render env vars; normalize.
if "\\n" in PRIVATE_KEY_PEM:
    PRIVATE_KEY_PEM = PRIVATE_KEY_PEM.replace("\\n", "\n")


# ----------------- HELPERS -----------------
def now_utc() -> dt.datetime:
    # timezone-aware UTC
    return dt.datetime.now(dt.timezone.utc)

def et_now() -> dt.datetime:
    # Use fixed ET offset; good enough for short-run bot. If you want DST-perfect, use zoneinfo.
    # Render runs UTC; we convert using -05:00 or -04:00 manually is messy.
    # We'll instead compute ET by using US/Eastern via zoneinfo when available.
    try:
        from zoneinfo import ZoneInfo
        return now_utc().astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # fallback: EST
        return now_utc().astimezone(dt.timezone(dt.timedelta(hours=-5)))

def ms_timestamp() -> str:
    return str(int(time.time() * 1000))


def require_trading_auth() -> Tuple[str, str]:
    if not API_KEY_ID:
        raise RuntimeError("Missing KALSHI_API_KEY_ID env var.")
    if not PRIVATE_KEY_PEM.strip():
        raise RuntimeError(
            "Missing KALSHI_PRIVATE_KEY env var. You cannot trade without the RSA private key."
        )
    return API_KEY_ID, PRIVATE_KEY_PEM


def load_private_key(pem: str):
    return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)


def canonical_string(ts_ms: str, method: str, path: str, body: str) -> bytes:
    """
    Kalshi signing convention used in their examples:
    message = timestamp + method + path + body
    (method uppercase, path includes /trade-api/v2/..., body is raw json string or "")
    """
    return (ts_ms + method.upper() + path + body).encode("utf-8")


def sign_request(ts_ms: str, method: str, path: str, body: str, private_key_pem: str) -> str:
    pk = load_private_key(private_key_pem)
    msg = canonical_string(ts_ms, method, path, body)
    sig = pk.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    auth: bool = False,
    timeout: int = 20,
) -> Dict[str, Any]:
    url = API_BASE.rstrip("/") + path

    body_str = ""
    if json_body is not None:
        body_str = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

    headers = {"Content-Type": "application/json"}

    if auth:
        key_id, private_pem = require_trading_auth()
        ts_ms = ms_timestamp()
        sig = sign_request(ts_ms, method, path, body_str, private_pem)

        headers.update(
            {
                "KALSHI-ACCESS-KEY": key_id,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            }
        )

    resp = requests.request(
        method=method.upper(),
        url=url,
        params=params,
        data=body_str if body_str else None,
        headers=headers,
        timeout=timeout,
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")

    if resp.text.strip() == "":
        return {}
    return resp.json()


# ----------------- KALSHI API WRAPPERS -----------------
def list_open_markets(series_ticker: str, limit: int = 200) -> Dict[str, Any]:
    return request(
        "GET",
        "/trade-api/v2/markets",
        params={"limit": limit, "status": "open", "series_ticker": series_ticker},
        auth=False,  # public read is fine
    )


def get_market(ticker: str) -> Dict[str, Any]:
    return request("GET", f"/trade-api/v2/markets/{ticker}", auth=False)


def get_balance_cash() -> Optional[float]:
    """
    If auth is correct, returns cash balance as float dollars.
    If auth fails, caller can catch.
    """
    data = request("GET", "/trade-api/v2/portfolio/balance", auth=True)
    # Typical shape includes "balance" fields; we defensively search.
    # If yours differs, paste the JSON and I'll match it precisely.
    for k in ["cash", "available_cash", "balance_cash", "cash_balance"]:
        if k in data:
            return float(data[k])
    # Sometimes nested:
    if "balance" in data and isinstance(data["balance"], dict):
        for k in ["cash", "available_cash"]:
            if k in data["balance"]:
                return float(data["balance"][k])
    return None


def place_order(
    *,
    ticker: str,
    side: str,      # "yes" or "no"
    action: str,    # "buy" or "sell"
    count: int,
    order_type: str = "limit",
    yes_price: Optional[int] = None,
    no_price: Optional[int] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "client_order_id": str(uuid.uuid4()),
        "count": int(count),
        "type": order_type,
    }
    if yes_price is not None:
        body["yes_price"] = int(yes_price)
    if no_price is not None:
        body["no_price"] = int(no_price)

    # Create order endpoint per docs
    # POST https://api.elections.kalshi.com/trade-api/v2/portfolio/orders   [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order?utm_source=chatgpt.com)
    return request("POST", "/trade-api/v2/portfolio/orders", json_body=body, auth=True)


# ----------------- STRATEGY STATE -----------------
STATE_PATH = "state.json"

@dataclass
class BotState:
    day: str
    start_cash: float
    realized_pnl: float
    trades_by_market: Dict[str, int]
    last_report_day: str

def load_state() -> BotState:
    if not os.path.exists(STATE_PATH):
        today = et_now().date().isoformat()
        return BotState(day=today, start_cash=0.0, realized_pnl=0.0, trades_by_market={}, last_report_day="")
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    return BotState(
        day=d.get("day", et_now().date().isoformat()),
        start_cash=float(d.get("start_cash", 0.0)),
        realized_pnl=float(d.get("realized_pnl", 0.0)),
        trades_by_market=dict(d.get("trades_by_market", {})),
        last_report_day=d.get("last_report_day", ""),
    )

def save_state(st: BotState) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "day": st.day,
                "start_cash": st.start_cash,
                "realized_pnl": st.realized_pnl,
                "trades_by_market": st.trades_by_market,
                "last_report_day": st.last_report_day,
            },
            f,
            indent=2,
        )


# ----------------- EMAIL -----------------
def send_email(subject: str, body: str) -> None:
    if not EMAIL_ENABLED:
        return
    if not (EMAIL_TO and EMAIL_FROM and GMAIL_APP_PASSWORD):
        log.warning("EMAIL_ENABLED=true but missing EMAIL_TO/EMAIL_FROM/GMAIL_APP_PASSWORD")
        return

    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())


def maybe_send_daily_report(st: BotState) -> None:
    if not EMAIL_ENABLED:
        return
    now_et = et_now()
    today = now_et.date().isoformat()

    if now_et.hour < EMAIL_REPORT_HOUR_ET:
        return
    if st.last_report_day == today:
        return

    pnl = st.realized_pnl
    subject = f"Kalshi Bot Daily P&L ({today})"
    body = (
        f"Date (ET): {today}\n"
        f"Start cash: ${st.start_cash:,.2f}\n"
        f"Realized P&L tracked by bot: ${pnl:,.2f}\n"
        f"Trades by market: {json.dumps(st.trades_by_market, indent=2)}\n"
    )
    send_email(subject, body)
    st.last_report_day = today
    save_state(st)


# ----------------- MARKET SELECTION -----------------
def resolve_next_open_market(series_prefix: str) -> Optional[str]:
    data = list_open_markets(series_prefix)
    markets = data.get("markets", []) or data.get("data", []) or []

    if not markets:
        return None

    # Choose the earliest close/settle time among open markets.
    # Kalshi schemas vary; we try several keys.
    def market_time(m: Dict[str, Any]) -> str:
        for k in ["close_time", "expiration_time", "settle_time", "end_time"]:
            if k in m and m[k]:
                return str(m[k])
        return "9999-12-31T00:00:00Z"

    markets_sorted = sorted(markets, key=market_time)
    return markets_sorted[0].get("ticker")


def best_book_prices(mkt: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """
    Returns yes_bid, yes_ask, no_bid, no_ask in cents.
    Your logs show these are present.
    """
    return (
        int(mkt.get("yes_bid", 0) or 0),
        int(mkt.get("yes_ask", 0) or 0),
        int(mkt.get("no_bid", 0) or 0),
        int(mkt.get("no_ask", 0) or 0),
    )


def best_bid_sizes(mkt: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    Dominance needs sizes. Some Kalshi market payloads include top-of-book sizes like:
    yes_bid_size, no_bid_size.
    If missing, we return None and dominance check is skipped.
    """
    y = mkt.get("yes_bid_size")
    n = mkt.get("no_bid_size")
    try:
        return (float(y) if y is not None else None, float(n) if n is not None else None)
    except Exception:
        return (None, None)


# ----------------- TRADING LOGIC -----------------
def can_trade_today(st: BotState, cash: float) -> bool:
    # set start-of-day cash on first successful balance fetch
    today = et_now().date().isoformat()
    if st.day != today:
        st.day = today
        st.start_cash = cash
        st.realized_pnl = 0.0
        st.trades_by_market = {}
        save_state(st)

    if st.start_cash <= 0:
        st.start_cash = cash
        save_state(st)

    max_loss = st.start_cash * DAILY_DRAWDOWN_LIMIT
    if st.realized_pnl <= -max_loss:
        log.warning("DAILY STOP HIT: realized_pnl=%.2f <= -%.2f", st.realized_pnl, max_loss)
        return False

    return True


def place_micro_win_trade(market_ticker: str, mkt: Dict[str, Any], cash: float, st: BotState) -> None:
    trades_done = st.trades_by_market.get(market_ticker, 0)
    if trades_done >= MAX_TRADES_PER_MARKET:
        log.info("Already traded this market (%s). Skipping.", market_ticker)
        return

    yes_bid, yes_ask, no_bid, no_ask = best_book_prices(mkt)

    # Dominance check
    y_size, n_size = best_bid_sizes(mkt)
    if y_size is not None and n_size is not None and (y_size + n_size) > 0:
        dom_yes = y_size / (y_size + n_size)
        dom_no = n_size / (y_size + n_size)
        dominant_side = "yes" if dom_yes >= dom_no else "no"
        dom_val = max(dom_yes, dom_no)
        log.info("Dominance: yes=%.3f no=%.3f -> %s (%.3f)", dom_yes, dom_no, dominant_side, dom_val)

        if dom_val < DOMINANCE_THRESHOLD:
            log.info("Dominance below threshold %.2f; no trade.", DOMINANCE_THRESHOLD)
            return
    else:
        log.info("No bid sizes available; dominance check skipped (safe mode).")
        return  # your rule requires dominance; if we cannot compute it, we do nothing.

    # Determine entry: if market is dominated on YES bids, we fade it by buying NO (contrarian),
    # OR follow it by buying YES (momentum). You said: "Buy/sell when 95% of market is on one side"
    # but didn’t specify fade vs follow. Default: FADE (contrarian) for micro scalps.
    fade = os.getenv("DOMINANCE_MODE", "fade").lower()  # "fade" or "follow"
    if fade == "follow":
        entry_side = dominant_side
    else:
        entry_side = "no" if dominant_side == "yes" else "yes"

    # Price to buy: cross the spread at ask to get filled (micro wins need fills)
    if entry_side == "yes":
        buy_price = yes_ask
        tp_price = min(99, buy_price + TAKE_PROFIT_CENTS)
        # We sell YES later at higher YES price
        buy_kwargs = dict(yes_price=buy_price)
        sell_kwargs = dict(yes_price=tp_price)
    else:
        buy_price = no_ask
        tp_price = min(99, buy_price + TAKE_PROFIT_CENTS)
        buy_kwargs = dict(no_price=buy_price)
        sell_kwargs = dict(no_price=tp_price)

    # Bet sizing: 1 contract costs ~price cents. Cost per contract in dollars:
    cost_per_contract = buy_price / 100.0
    max_spend = cash * BET_FRACTION_OF_CASH
    count = int(max(1, max_spend // cost_per_contract)) if cost_per_contract > 0 else 0
    if count <= 0:
        log.info("Not enough cash for even 1 contract at %.2f", cost_per_contract)
        return

    # Place orders
    if not ENABLE_TRADING:
        log.info("[DRY RUN] Would BUY %s %d @ %d and TP SELL @ %d", entry_side, count, buy_price, tp_price)
        return

    log.info("Placing BUY: side=%s count=%d price=%d on %s", entry_side, count, buy_price, market_ticker)
    buy_resp = place_order(
        ticker=market_ticker,
        side=entry_side,
        action="buy",
        count=count,
        order_type="limit",
        **buy_kwargs,
    )
    log.info("BUY order response: %s", json.dumps(buy_resp)[:800])

    log.info("Placing TAKE-PROFIT SELL: side=%s count=%d price=%d on %s", entry_side, count, tp_price, market_ticker)
    sell_resp = place_order(
        ticker=market_ticker,
        side=entry_side,
        action="sell",
        count=count,
        order_type="limit",
        **sell_kwargs,
    )
    log.info("TP SELL order response: %s", json.dumps(sell_resp)[:800])

    # Track that we attempted a trade for this market.
    st.trades_by_market[market_ticker] = trades_done + 1
    save_state(st)


# ----------------- MAIN LOOP -----------------
def main():
    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", ENABLE_TRADING)
    log.info("POLL_SECONDS=%s", POLL_SECONDS)
    log.info("SERIES_PREFIX=%s", SERIES_PREFIX)
    log.info("API_BASE=%s", API_BASE)
    log.info("EMAIL_ENABLED=%s", EMAIL_ENABLED)

    st = load_state()

    while True:
        loop_start = time.time()

        try:
            # Cash balance (auth required)
            cash = None
            if ENABLE_TRADING:
                try:
                    cash = get_balance_cash()
                except Exception as e:
                    log.warning("Could not fetch cash balance: %s", e)

            # Resolve next open market
            mkt_ticker = resolve_next_open_market(SERIES_PREFIX)
            now_et = et_now()

            if not mkt_ticker:
                log.info("Heartbeat ET now=%s | No open markets found for %s", now_et.strftime("%Y-%m-%d %H:%M:%S %Z"), SERIES_PREFIX)
                maybe_send_daily_report(st)
                time.sleep(POLL_SECONDS)
                continue

            mkt = get_market(mkt_ticker)
            yes_bid, yes_ask, no_bid, no_ask = best_book_prices(mkt)

            log.info(
                "Heartbeat ET now=%s | market=%s | yes %s/%s no %s/%s",
                now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                mkt_ticker,
                yes_bid, yes_ask, no_bid, no_ask,
            )

            # Daily email
            maybe_send_daily_report(st)

            # Trade only if we have cash and within limits
            if cash is not None:
                if can_trade_today(st, cash):
                    place_micro_win_trade(mkt_ticker, mkt, cash, st)

        except Exception as e:
            log.error("LOOP ERROR: %s", e)

        elapsed = time.time() - loop_start
        sleep_for = max(1, POLL_SECONDS - elapsed)
        log.info("Loop complete in %.2fs; sleeping %ds", elapsed, int(sleep_for))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
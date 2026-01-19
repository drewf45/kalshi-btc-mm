import os
import json
import time
import base64
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc15m-bot")


# ----------------------------
# Config
# ----------------------------
load_dotenv()

DEFAULT_API_BASE = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

# Strategy knobs
IMBALANCE_THRESHOLD = int(os.getenv("IMBALANCE_THRESHOLD", "95"))  # 95% one-sided
TAKE_PROFIT_CENTS = int(os.getenv("TAKE_PROFIT_CENTS", "1"))       # +1c take-profit
MAX_TRADE_PCT = float(os.getenv("MAX_TRADE_PCT", "0.01"))          # 1% of cash per trade
DAILY_MAX_DRAWDOWN_PCT = float(os.getenv("DAILY_MAX_DRAWDOWN_PCT", "0.20"))  # 20% of cash

# Market only
ONLY_SERIES = SERIES_PREFIX  # KXBTC15M only

# Subaccount (optional)
SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT", "").strip()  # e.g. "btc1" if you use it

# Kalshi auth
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

# Email (optional)
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
EMAIL_TO = os.getenv("EMAIL_TO", "").strip()

# ----------------------------
# Helpers
# ----------------------------
def utc_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def sanitize_api_base(raw: str) -> str:
    """
    Fix the #1 mistake causing 404s:
    - People set KALSHI_API_BASE to https://api.elections.kalshi.com/trade-api/v2
      then code appends /trade-api/v2 again.
    """
    raw = (raw or "").strip()
    if not raw:
        raw = DEFAULT_API_BASE

    # strip trailing slash
    raw = raw.rstrip("/")

    # If they accidentally included /trade-api/... remove it
    idx = raw.find("/trade-api/")
    if idx != -1:
        raw = raw[:idx]

    return raw


API_BASE = sanitize_api_base(os.getenv("KALSHI_API_BASE", DEFAULT_API_BASE))

log.info("=== BOT STARTED ===")
log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
log.info(f"POLL_SECONDS={POLL_SECONDS}")
log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
log.info(f"API_BASE={API_BASE}")


# ----------------------------
# Kalshi Client
# ----------------------------
class KalshiClient:
    def __init__(self, base: str, key_id: str, private_key_b64: str, subaccount: str = ""):
        self.base = base.rstrip("/")
        self.key_id = key_id
        self.subaccount = subaccount.strip()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        self.private_key = None
        if private_key_b64:
            try:
                pem = base64.b64decode(private_key_b64.encode("utf-8"))
                self.private_key = serialization.load_pem_private_key(pem, password=None)
            except Exception as e:
                raise RuntimeError(f"Failed to decode/load private key from KALSHI_PRIVATE_KEY_B64: {e}")

    def _full_url(self, path: str) -> str:
        # Ensure single prefix + correct API path
        if not path.startswith("/"):
            path = "/" + path
        # Always route through /trade-api/v2
        if not path.startswith(API_PREFIX + "/"):
            path = API_PREFIX + path
        return self.base + path

    def _sign(self, method: str, path_with_prefix: str, body: str, ts_ms: int) -> str:
        """
        Kalshi header requires RSA-PSS signature of the request. Docs show:
        KALSHI-ACCESS-KEY (key id), KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE (rsa-pss)
        Endpoint example: /trade-api/v2/portfolio/balance  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/portfolio/get-balance)

        The canonical string used by most Kalshi examples is:
            f"{ts}{METHOD}{PATH}{BODY}"
        where PATH includes /trade-api/v2/...
        """
        if not self.private_key:
            raise RuntimeError("Missing private key: set KALSHI_PRIVATE_KEY_B64")

        method = method.upper()
        msg = f"{ts_ms}{method}{path_with_prefix}{body}".encode("utf-8")

        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None, auth: bool = False) -> Any:
        method = method.upper()
        url = self._full_url(path)
        parsed_path = url.replace(self.base, "")  # includes /trade-api/v2/...

        body_str = ""
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))

        headers = {}
        if self.subaccount:
            # Subaccount header name can vary; this one is commonly accepted by Kalshi in practice.
            headers["KALSHI-SUBACCOUNT"] = self.subaccount

        if auth:
            ts = utc_ms()
            headers["KALSHI-ACCESS-KEY"] = self.key_id
            headers["KALSHI-ACCESS-TIMESTAMP"] = str(ts)
            headers["KALSHI-ACCESS-SIGNATURE"] = self._sign(method, parsed_path, body_str, ts)

        resp = self.session.request(method, url, headers=headers, data=body_str if body_str else None, timeout=20)

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")

        if resp.text:
            return resp.json()
        return None

    # --------- Convenience endpoints ----------
    def get_balance(self) -> Dict[str, Any]:
        return self.request("GET", "/portfolio/balance", auth=True)

    def get_markets(self, series_ticker: str, status: str = "open", limit: int = 50) -> Dict[str, Any]:
        # Many Kalshi endpoints use query params. We'll keep it simple with requests params manually.
        url = self._full_url("/markets")
        params = {"series_ticker": series_ticker, "status": status, "limit": limit}
        resp = self.session.get(url, params=params, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text}")
        return resp.json()

    def get_market(self, market_ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/markets/{market_ticker}")

    def get_orderbook(self, market_ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/markets/{market_ticker}/orderbook")

    def create_order(self, market_ticker: str, side: str, action: str, price: int, count: int) -> Dict[str, Any]:
        payload = {
            "market_ticker": market_ticker,
            "side": side,          # "yes" or "no"
            "action": action,      # "buy" or "sell"
            "type": "limit",
            "price": price,        # cents 0-100
            "count": count,        # contracts
        }
        return self.request("POST", "/orders", json_body=payload, auth=True)

    def get_fills(self, limit: int = 200) -> Dict[str, Any]:
        return self.request("GET", f"/portfolio/fills?limit={limit}", auth=True)


# ----------------------------
# Market selection (NEXT open 15m)
# ----------------------------
def resolve_next_open_market(kc: KalshiClient, series_prefix: str) -> Optional[str]:
    """
    Uses /markets?series_ticker=KXBTC15M&status=open
    Then picks the earliest close/open time by ticker sorting (good enough for 15m series).
    """
    data = kc.get_markets(series_prefix, status="open", limit=50)
    markets = data.get("markets", []) or data.get("data", []) or []

    if not markets:
        return None

    # Sort by ticker string (Kalshi’s BTC 15m tickers embed time; this is stable enough for next market)
    markets_sorted = sorted(markets, key=lambda m: m.get("ticker", ""))
    return markets_sorted[0].get("ticker")


def parse_best_bid_ask(orderbook: Dict[str, Any], side: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Normalize various orderbook shapes.
    We want best bid and best ask (both in cents).
    """
    # common shapes: {"orderbook":{"yes":{"bids":[[price,count],...],"asks":[...]}, "no":{...}}}
    ob = orderbook.get("orderbook") or orderbook

    side_book = ob.get(side) or {}
    bids = side_book.get("bids") or []
    asks = side_book.get("asks") or []

    best_bid = None
    best_ask = None

    if bids:
        # bids descending price
        best_bid = int(bids[0][0]) if isinstance(bids[0], (list, tuple)) else int(bids[0].get("price"))
    if asks:
        # asks ascending price
        best_ask = int(asks[0][0]) if isinstance(asks[0], (list, tuple)) else int(asks[0].get("price"))

    return best_bid, best_ask


# ----------------------------
# Strategy: "farm micro wins" on extreme imbalance
# ----------------------------
@dataclass
class DayRisk:
    day_key: str
    start_cash_cents: int
    realized_pnl_cents: int = 0

def day_key_et() -> str:
    # Simple ET day boundary using UTC-5 assumption (good enough for your use; avoid zoneinfo dependency)
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    return now.strftime("%Y-%m-%d")

def compute_order_size(cash_cents: int, price_cents: int, max_pct: float) -> int:
    """
    Buy contracts at price_cents each.
    Cost = count * price_cents (in cents)
    Max cost = cash_cents * max_pct
    """
    if price_cents <= 0:
        return 0
    max_cost = int(cash_cents * max_pct)
    count = max_cost // price_cents
    return max(0, count)

def should_trade(yes_bid, yes_ask, no_bid, no_ask, threshold: int) -> Optional[Tuple[str, int]]:
    """
    Return (side_to_buy, price_to_buy_at) if imbalance triggers.
    Trigger: if YES is crowded (>=threshold), buy NO cheap; if NO crowded, buy YES cheap.
    We use ASK of crowded side OR BID, whichever is available.
    """
    # Determine crowding using best available quotes
    yes_level = max([x for x in [yes_bid, yes_ask] if x is not None], default=None)
    no_level = max([x for x in [no_bid, no_ask] if x is not None], default=None)

    if yes_level is None or no_level is None:
        return None

    if yes_level >= threshold and no_ask is not None:
        # crowd is YES, buy NO at ask
        return ("no", int(no_ask))
    if no_level >= threshold and yes_ask is not None:
        # crowd is NO, buy YES at ask
        return ("yes", int(yes_ask))

    return None


# ----------------------------
# Email P&L (optional)
# ----------------------------
def send_email(subject: str, body: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        return  # silently skip if not configured

    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())


# ----------------------------
# Main loop
# ----------------------------
def main():
    if not KALSHI_KEY_ID:
        raise RuntimeError("Missing env var: KALSHI_KEY_ID")
    if not KALSHI_PRIVATE_KEY_B64:
        raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64 (base64 of RSA private key PEM)")

    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)

    # Auth check
    bal = kc.get_balance()
    cash_cents = int(bal.get("balance", 0))
    log.info("Auth check OK (portfolio/balance). cash_cents=%s", cash_cents)

    risk = DayRisk(day_key=day_key_et(), start_cash_cents=cash_cents)

    last_market = None
    traded_markets_today = set()

    while True:
        try:
            # roll day
            dk = day_key_et()
            if dk != risk.day_key:
                # send daily email on rollover (end-of-day summary)
                try:
                    send_email(
                        subject=f"Kalshi BTC15m Daily P&L {risk.day_key}",
                        body=f"Start cash: ${risk.start_cash_cents/100:.2f}\nRealized P&L: ${risk.realized_pnl_cents/100:.2f}\n",
                    )
                    log.info("Daily email sent.")
                except Exception as e:
                    log.warning("Daily email failed: %s", e)

                # reset
                bal = kc.get_balance()
                cash_cents = int(bal.get("balance", 0))
                risk = DayRisk(day_key=dk, start_cash_cents=cash_cents, realized_pnl_cents=0)
                traded_markets_today.clear()

            # daily max loss check
            bal = kc.get_balance()
            cash_cents = int(bal.get("balance", 0))

            drawdown = risk.start_cash_cents - cash_cents
            if drawdown >= int(risk.start_cash_cents * DAILY_MAX_DRAWDOWN_PCT):
                log.warning("DAILY STOP HIT: drawdown=%s cents (>= %.0f%%). Sleeping.", drawdown, DAILY_MAX_DRAWDOWN_PCT * 100)
                time.sleep(POLL_SECONDS)
                continue

            market = resolve_next_open_market(kc, ONLY_SERIES)
            if not market:
                log.warning("No open markets found for series %s", ONLY_SERIES)
                time.sleep(POLL_SECONDS)
                continue

            if market != last_market:
                log.info("Resolved next market=%s", market)
                last_market = market

            ob = kc.get_orderbook(market)
            yes_bid, yes_ask = parse_best_bid_ask(ob, "yes")
            no_bid, no_ask = parse_best_bid_ask(ob, "no")

            log.info(
                "Heartbeat | market=%s | yes %s/%s no %s/%s | cash=$%.2f",
                market, yes_bid, yes_ask, no_bid, no_ask, cash_cents / 100.0
            )

            # Need quotes to trade
            if any(x is None for x in [yes_bid, yes_ask, no_bid, no_ask]):
                log.warning("Orderbook incomplete (missing bid/ask). Skipping this loop.")
                time.sleep(POLL_SECONDS)
                continue

            decision = should_trade(yes_bid, yes_ask, no_bid, no_ask, IMBALANCE_THRESHOLD)
            if not decision:
                time.sleep(POLL_SECONDS)
                continue

            side_to_buy, entry_price = decision

            # only one trade per market per day (your “1 per each 15 minute” rule)
            mk_key = f"{risk.day_key}:{market}"
            if mk_key in traded_markets_today:
                time.sleep(POLL_SECONDS)
                continue

            # size: 1% of cash
            count = compute_order_size(cash_cents, entry_price, MAX_TRADE_PCT)
            if count <= 0:
                log.warning("Computed order size is 0. cash=%s price=%s", cash_cents, entry_price)
                time.sleep(POLL_SECONDS)
                continue

            if ENABLE_TRADING and CONFIRM_LIVE_TRADING:
                log.info("PLACING BUY: market=%s side=%s price=%s count=%s", market, side_to_buy, entry_price, count)
                res = kc.create_order(market_ticker=market, side=side_to_buy, action="buy", price=entry_price, count=count)
                log.info("BUY placed: %s", res)

                # place take-profit sell at +1 cent (micro-win target)
                tp_price = min(99, entry_price + TAKE_PROFIT_CENTS)
                log.info("PLACING TAKE-PROFIT SELL: market=%s side=%s price=%s count=%s", market, side_to_buy, tp_price, count)
                res2 = kc.create_order(market_ticker=market, side=side_to_buy, action="sell", price=tp_price, count=count)
                log.info("TP sell placed: %s", res2)

                traded_markets_today.add(mk_key)
            else:
                log.info("SIGNAL ONLY (CONFIRM_LIVE_TRADING is false). Would BUY %s @%s x%s and TP @%s",
                         side_to_buy, entry_price, count, entry_price + TAKE_PROFIT_CENTS)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error("LOOP ERROR: %r", e)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
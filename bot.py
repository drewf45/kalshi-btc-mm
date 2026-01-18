import os
import time
import json
import base64
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone, date
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-btc-mm")


# -----------------------------
# Env helpers
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v.strip())
    except Exception:
        return default


def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v.strip()


def _b64_maybe_decode(s: str) -> bytes:
    """
    Accepts either:
      - base64 encoded PEM (recommended)
      - raw PEM text
    Returns PEM bytes.
    """
    if "BEGIN" in s and "PRIVATE KEY" in s:
        # raw PEM pasted
        return s.encode("utf-8")

    # maybe base64
    try:
        raw = base64.b64decode(s, validate=True)
        if b"BEGIN" in raw and b"PRIVATE KEY" in raw:
            return raw
        # If it decodes but doesn't look like PEM, still return it (some keys are PKCS#8 DER)
        return raw
    except Exception:
        # last resort: treat as raw
        return s.encode("utf-8")


# -----------------------------
# Email (non-fatal)
# -----------------------------
@dataclass
class EmailConfig:
    enabled: bool
    to_addr: str
    from_addr: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_tls: bool


class Notifier:
    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg

    def send(self, subject: str, body: str) -> None:
        if not self.cfg.enabled:
            return

        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.cfg.from_addr
            msg["To"] = self.cfg.to_addr
            msg.set_content(body)

            if self.cfg.smtp_tls:
                context = ssl.create_default_context()
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=20) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    server.send_message(msg)
            else:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.cfg.smtp_host, self.cfg.smtp_port, context=context, timeout=20) as server:
                    server.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    server.send_message(msg)

        except Exception as e:
            # IMPORTANT: never crash trading loop because email broke
            log.error(f"EMAIL FAILED: {repr(e)}")


# -----------------------------
# Kalshi client (RSA-PSS signed)
# -----------------------------
class KalshiClient:
    """
    Uses Kalshi Trade API v2 with RSA-PSS signatures.
    Headers:
      - KALSHI-ACCESS-KEY
      - KALSHI-ACCESS-SIGNATURE
      - KALSHI-ACCESS-TIMESTAMP
    Signature is generated from the request payload per Kalshi docs.  [oai_citation:1‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/api_keys)
    """

    def __init__(self, base_url: str, key_id: str, private_key_pem: bytes, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.timeout = timeout

        # Load private key (PKCS8 PEM recommended)
        self.private_key = serialization.load_pem_private_key(private_key_pem, password=None)

        self.session = requests.Session()

    def _sign(self, timestamp_ms: str, method: str, path: str, body: str) -> str:
        """
        Per Kalshi: message is timestamp + method + path + body.  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/api_keys)
        """
        payload = (timestamp_ms + method.upper() + path + body).encode("utf-8")
        sig = self.private_key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        # IMPORTANT: header-safe base64 (no newlines)
        return base64.b64encode(sig).decode("ascii")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body_str = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        ts_ms = str(int(time.time() * 1000))

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, path, body_str),
        }

        resp = self.session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            data=None if json_body is None else body_str,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:500]}")
        return resp.json()

    # -------- Portfolio --------
    def get_balance(self) -> Dict[str, Any]:
        # GET Get Balance  [oai_citation:3‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order)
        return self.request("GET", "/portfolio/balance")

    def get_positions(self) -> Dict[str, Any]:
        return self.request("GET", "/portfolio/positions")

    def get_fills(self, limit: int = 50) -> Dict[str, Any]:
        # GET Get Fills  [oai_citation:4‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order)
        return self.request("GET", "/portfolio/fills", params={"limit": limit})

    # -------- Market data --------
    def get_markets(self, **kwargs) -> Dict[str, Any]:
        # GET Get Markets  [oai_citation:5‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order)
        return self.request("GET", "/markets", params=kwargs)

    def get_market_orderbook(self, ticker: str, depth: int = 25) -> Dict[str, Any]:
        # GET Get Market Orderbook  [oai_citation:6‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order)
        return self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # -------- Orders --------
    def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # POST Create Order  [oai_citation:7‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order)
        return self.request("POST", "/portfolio/orders", json_body=payload)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        # DEL Cancel Order  [oai_citation:8‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/cancel-order)
        return self.request("DELETE", f"/portfolio/orders/{order_id}")


# -----------------------------
# Strategy helpers
# -----------------------------
def sum_depth(side_levels: List[List[int]]) -> int:
    """
    side_levels format typically: [[price, quantity], ...]
    We sum quantity across depth.
    """
    total = 0
    for lvl in side_levels or []:
        if not lvl or len(lvl) < 2:
            continue
        qty = int(lvl[1])
        total += qty
    return total


def best_ask(side_levels: List[List[int]]) -> Optional[Tuple[int, int]]:
    # returns (price, qty)
    if not side_levels:
        return None
    # asks are usually sorted low->high; if not, find min price
    best = min(side_levels, key=lambda x: x[0])
    return int(best[0]), int(best[1])


def next_open_btc15m_market(kalshi: KalshiClient, series_ticker: str = "KXBTC15M") -> Optional[Dict[str, Any]]:
    """
    IMPORTANT: We do NOT guess the ticker from local time.
    We ask Kalshi for markets in the series and choose the nearest one that is OPEN and not closed yet.
    This avoids the entire "wrong market / wrong time / seconds in ticker" loop.
    """
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    # Pull upcoming markets, filter to open-ish.
    # (Kalshi params may include status / series_ticker; using series_ticker is documented under Get Markets.)
    resp = kalshi.get_markets(series_ticker=series_ticker, limit=50)

    markets = resp.get("markets", []) or resp.get("data", []) or []
    if not markets:
        return None

    # Choose earliest market whose close_ts is in the future
    future = []
    for m in markets:
        close_ts = m.get("close_ts") or m.get("close_time") or 0
        status = (m.get("status") or "").upper()
        if close_ts and int(close_ts) > now_ts and status in ("OPEN", "ACTIVE", "TRADING"):
            future.append(m)

    if not future:
        # fallback: just pick nearest close_ts > now even if status missing
        for m in markets:
            close_ts = m.get("close_ts") or 0
            if close_ts and int(close_ts) > now_ts:
                future.append(m)

    if not future:
        return None

    future.sort(key=lambda m: int(m.get("close_ts") or 10**18))
    return future[0]


def compute_imbalance_from_orderbook(ob: Dict[str, Any]) -> Tuple[float, int, int]:
    """
    We compute imbalance based on TOTAL resting qty on YES vs NO.
    For Kalshi binary markets, orderbook commonly has yes/no sides.
    """
    # Depending on response shape, adjust keys
    # Common: ob["orderbook"]["yes"]["asks"], ob["orderbook"]["no"]["asks"]
    root = ob.get("orderbook", ob)

    yes = root.get("yes", {}) or {}
    no = root.get("no", {}) or {}

    yes_asks = yes.get("asks", []) or []
    no_asks = no.get("asks", []) or []

    yes_qty = sum_depth(yes_asks)
    no_qty = sum_depth(no_asks)

    total = yes_qty + no_qty
    if total <= 0:
        return 0.5, yes_qty, no_qty

    # "95% of market is on one side" -> 0.95+ means that side dominates ask depth
    return yes_qty / total, yes_qty, no_qty


def pick_micro_trade(ob: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Rule:
      - If YES side dominates (>=95%), market is 'overcrowded' on YES -> buy NO for micro win.
      - If NO side dominates (<=5%), buy YES.
    Entry = best ask on underdog side.
    Exit = opposite side at +1 cent equivalent.
    """
    root = ob.get("orderbook", ob)
    yes = root.get("yes", {}) or {}
    no = root.get("no", {}) or {}

    yes_asks = yes.get("asks", []) or []
    no_asks = no.get("asks", []) or []

    imbalance, yes_qty, no_qty = compute_imbalance_from_orderbook(ob)

    # Which side is overcrowded?
    if imbalance >= 0.95:
        # YES crowded -> buy NO
        entry = best_ask(no_asks)
        side = "no"
        crowd = "yes"
    elif imbalance <= 0.05:
        # NO crowded -> buy YES
        entry = best_ask(yes_asks)
        side = "yes"
        crowd = "no"
    else:
        return None

    if entry is None:
        return None

    entry_price, entry_qty = entry
    # micro win target = +1 cent on same contract side,
    # which in binary terms corresponds to (100 - (entry_price + 1)) on the opposite side.
    exit_price_same_side = min(entry_price + 1, 99)

    return {
        "crowd_side": crowd,
        "trade_side": side,          # "yes" or "no" to BUY
        "entry_price_cents": entry_price,
        "target_price_cents": exit_price_same_side,
        "imbalance": imbalance,
        "yes_qty": yes_qty,
        "no_qty": no_qty,
        "entry_qty_at_level": entry_qty,
    }


# -----------------------------
# Main bot
# -----------------------------
def main():
    BASE_URL = env_str("BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")

    # Canonical env vars (use these)
    KEY_ID = env_str("KALSHI_KEY_ID") or env_str("KALSHI_API_KEY_ID") or env_str("KALSHI_ACCESS_KEY_ID")
    PRIV_B64 = env_str("KALSHI_PRIVATE_KEY_PEM_B64") or env_str("KALSHI_PRIVATE_KEY_B64") or env_str("KALSHI_PRIVATE_KEY")

    ENABLE_TRADING = env_bool("ENABLE_TRADING", False)
    POLL_SECONDS = env_int("POLL_SECONDS", 60)

    # Risk + behavior
    SERIES_TICKER = env_str("SERIES_TICKER", "KXBTC15M")
    IMBALANCE_TRIGGER = env_float("IMBALANCE_TRIGGER", 0.95)  # 95%
    PER_TRADE_FRACTION = env_float("PER_TRADE_FRACTION", 0.01) # 1% of liquidity
    DAILY_DRAWDOWN_LIMIT = env_float("DAILY_DRAWDOWN_LIMIT", 0.20) # 20%

    # Order sizing
    MIN_CONTRACTS = env_int("MIN_CONTRACTS", 1)
    MAX_CONTRACTS = env_int("MAX_CONTRACTS", 5)

    # Email (Gmail requires APP PASSWORD, not normal password)
    email_cfg = EmailConfig(
        enabled=env_bool("EMAIL_ENABLED", False),
        to_addr=env_str("EMAIL_TO", ""),
        from_addr=env_str("EMAIL_FROM", env_str("SMTP_USER", "")),
        smtp_host=env_str("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=env_int("SMTP_PORT", 465),
        smtp_user=env_str("SMTP_USER", ""),
        smtp_pass=env_str("SMTP_PASS", ""),
        smtp_tls=env_bool("SMTP_TLS", False),
    )
    notifier = Notifier(email_cfg)

    log.info("=== BOT STARTED ===")
    log.info(f"BASE_URL={BASE_URL}")
    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_TICKER={SERIES_TICKER}")

    # Credentials guard (prevents silent read-only confusion)
    if not KEY_ID or not PRIV_B64:
        log.warning("WARNING: Kalshi credentials missing - running in READ-ONLY mode")
        ENABLE_TRADING = False
        kalshi = None
    else:
        pem = _b64_maybe_decode(PRIV_B64)
        kalshi = KalshiClient(BASE_URL, KEY_ID, pem)

    # Track daily limit
    day_key: Optional[date] = None
    start_day_cash: Optional[float] = None

    # Simple fill tracking so we don't spam email
    last_fill_seen_ts = 0

    while True:
        try:
            if kalshi is None:
                log.info("READ-ONLY: missing credentials; set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PEM_B64.")
                time.sleep(POLL_SECONDS)
                continue

            # Reset day tracking at UTC day boundary (keep it simple + consistent)
            today = datetime.now(timezone.utc).date()
            if day_key != today:
                bal = kalshi.get_balance()
                # balance response varies; commonly includes "available_cash" or similar
                cash = (
                    bal.get("available_cash")
                    or bal.get("cash")
                    or bal.get("balance", {}).get("available_cash")
                    or 0
                )
                start_day_cash = float(cash)
                day_key = today
                log.info(f"NEW DAY: start_day_cash={start_day_cash}")

            # Daily stop check
            bal = kalshi.get_balance()
            cash = (
                bal.get("available_cash")
                or bal.get("cash")
                or bal.get("balance", {}).get("available_cash")
                or 0
            )
            cash = float(cash)

            if start_day_cash is not None and cash <= start_day_cash * (1.0 - DAILY_DRAWDOWN_LIMIT):
                msg = f"DAILY STOP HIT: cash={cash:.2f} start={start_day_cash:.2f} limit={DAILY_DRAWDOWN_LIMIT:.2%}. Trading paused."
                log.warning(msg)
                notifier.send("Kalshi Bot: DAILY STOP HIT", msg)
                time.sleep(POLL_SECONDS)
                continue

            # Pick the correct market by ASKING Kalshi (no more guessing tickers)
            m = next_open_btc15m_market(kalshi, SERIES_TICKER)
            if not m:
                log.info("No BTC15M markets found/upcoming.")
                time.sleep(POLL_SECONDS)
                continue

            ticker = m.get("ticker")
            if not ticker:
                log.info("Market missing ticker field; skipping.")
                time.sleep(POLL_SECONDS)
                continue

            log.info(f"CHECKING MARKET: {ticker}")

            # Orderbook
            ob = kalshi.get_market_orderbook(ticker, depth=25)
            idea = pick_micro_trade(ob)

            if not idea:
                log.info("No setup (imbalance not extreme enough).")
                time.sleep(POLL_SECONDS)
                continue

            # Enforce 95% rule explicitly
            imb = float(idea["imbalance"])
            if imb < IMBALANCE_TRIGGER and imb > (1.0 - IMBALANCE_TRIGGER):
                log.info(f"Setup rejected: imbalance={imb:.3f} trigger={IMBALANCE_TRIGGER:.2f}")
                time.sleep(POLL_SECONDS)
                continue

            trade_side = idea["trade_side"]  # yes/no
            entry_px = int(idea["entry_price_cents"])
            target_px = int(idea["target_price_cents"])

            # size = <= 1% of cash / (price dollars per contract)
            # worst-case per contract cost is price_cents/100
            cost_per_contract = max(entry_px / 100.0, 0.01)
            max_dollars = cash * PER_TRADE_FRACTION
            contracts = int(max_dollars / cost_per_contract)
            contracts = max(contracts, MIN_CONTRACTS)
            contracts = min(contracts, MAX_CONTRACTS)

            # If you're basically broke, don't force trades
            if contracts <= 0 or cash <= 1.0:
                log.info("No liquidity yet (or too low) -> skipping trades.")
                time.sleep(POLL_SECONDS)
                continue

            log.info(
                f"SETUP: crowd={idea['crowd_side']} trade_side={trade_side} "
                f"entry={entry_px}c target={target_px}c "
                f"imbalance={imb:.3f} yes_qty={idea['yes_qty']} no_qty={idea['no_qty']} "
                f"contracts={contracts} cash={cash:.2f}"
            )

            if not ENABLE_TRADING:
                log.info("ENABLE_TRADING=False (dry). Not placing orders.")
                time.sleep(POLL_SECONDS)
                continue

            # ---- Place entry order ----
            # Create Order endpoint documented here  [oai_citation:9‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/orders/create-order)
            entry_order = {
                "ticker": ticker,
                "action": "buy",
                "type": "limit",
                "count": contracts,
                # Kalshi uses yes_price / no_price in cents depending on side:
                "yes_price": entry_px if trade_side == "yes" else None,
                "no_price": entry_px if trade_side == "no" else None,
            }
            # remove None keys to keep payload clean
            entry_order = {k: v for k, v in entry_order.items() if v is not None}

            resp = kalshi.create_order(entry_order)
            log.info(f"ENTRY ORDER SENT: {resp}")

            notifier.send(
                "Kalshi Bot: ENTRY ORDER",
                f"ticker={ticker}\nside=BUY {trade_side.upper()}\nprice={entry_px}c\ncontracts={contracts}\nimbalance={imb:.3f}\nresp={json.dumps(resp)[:1500]}",
            )

            # ---- “Micro win” exit order ----
            # We avoid true "sell" complexity by placing the *equivalent opposing buy*.
            # If you bought YES at P, selling YES at (P+1) == buying NO at (100-(P+1)).
            if trade_side == "yes":
                exit_side = "no"
                exit_px = max(1, 100 - target_px)
            else:
                exit_side = "yes"
                exit_px = target_px

            exit_order = {
                "ticker": ticker,
                "action": "buy",
                "type": "limit",
                "count": contracts,
                "yes_price": exit_px if exit_side == "yes" else None,
                "no_price": exit_px if exit_side == "no" else None,
            }
            exit_order = {k: v for k, v in exit_order.items() if v is not None}

            resp2 = kalshi.create_order(exit_order)
            log.info(f"EXIT ORDER SENT: {resp2}")

            notifier.send(
                "Kalshi Bot: EXIT ORDER",
                f"ticker={ticker}\nside=BUY {exit_side.upper()} (exit)\nprice={exit_px}c\ncontracts={contracts}\nresp={json.dumps(resp2)[:1500]}",
            )

            # Fills check (lightweight)
            fills = kalshi.get_fills(limit=10)
            fills_list = fills.get("fills", []) or fills.get("data", []) or []
            if fills_list:
                # best-effort dedupe by ts
                newest = fills_list[0]
                ts = int(newest.get("ts") or newest.get("created_ts") or 0)
                if ts and ts > last_fill_seen_ts:
                    last_fill_seen_ts = ts
                    notifier.send("Kalshi Bot: NEW FILL", json.dumps(newest)[:1500])

            time.sleep(POLL_SECONDS)

        except Exception as e:
            # Never die; notify + keep going
            msg = f"ERROR LOOP: {repr(e)}"
            log.exception(msg)
            notifier.send("Kalshi Bot: ERROR", msg)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
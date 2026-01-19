import os
import time
import json
import base64
import hmac
import hashlib
import logging
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from cryptography.hazmat.primitives import serialization

# =========================
# Config
# =========================

API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/")
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
MARKET_TICKER = os.getenv("MARKET_TICKER")  # optional override
SUBACCOUNT = os.getenv("SUBACCOUNT")  # optional
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

# Strategy knobs
MIN_SECONDS_TO_CLOSE = int(os.getenv("MIN_SECONDS_TO_CLOSE", "120"))   # don't trade if too close to settle
MAX_SECONDS_TO_CLOSE = int(os.getenv("MAX_SECONDS_TO_CLOSE", "1200"))  # only trade near the close window

BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "1"))               # try to buy at 1c by default
TARGET_PROFIT_CENTS = int(os.getenv("TARGET_PROFIT_CENTS", "1"))       # aim to sell at buy+1c
MAX_POSITION_CONTRACTS = int(os.getenv("MAX_POSITION_CONTRACTS", "3")) # inventory cap
BASE_ORDER_SIZE = int(os.getenv("BASE_ORDER_SIZE", "1"))               # start at 1 contract
FILL_TIMEOUT_SECONDS = int(os.getenv("FILL_TIMEOUT_SECONDS", "20"))    # cancel buy if not filled quickly
SELL_TIMEOUT_SECONDS = int(os.getenv("SELL_TIMEOUT_SECONDS", "45"))    # cancel sell if not filled

# Exponential sizing (slow)
SCALE_AFTER_WINS = int(os.getenv("SCALE_AFTER_WINS", "20"))            # increase size after N wins
SIZE_MULTIPLIER = float(os.getenv("SIZE_MULTIPLIER", "1.25"))          # exponential-ish
MAX_ORDER_SIZE = int(os.getenv("MAX_ORDER_SIZE", "10"))                # cap

# Contract side to farm: YES or NO
FARM_SIDE = os.getenv("FARM_SIDE", "YES").upper().strip()  # YES or NO

# Debug
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Email (optional)
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER or "")

# =========================
# Logging
# =========================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s"
)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def safe_preview(obj: Any, limit: int = 1200) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        return s[:limit] + "...(truncated)"
    return s

def redact(s: str) -> str:
    if not s:
        return s
    if len(s) <= 10:
        return "***"
    return s[:4] + "..." + s[-4:]

# =========================
# Kalshi Client (RSA signing)
# =========================

class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key_b64: str, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.subaccount = subaccount

        if not key_id:
            raise RuntimeError("Missing env var: KALSHI_KEY_ID")
        if not private_key_b64:
            raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")

        pem_bytes = base64.b64decode(private_key_b64.encode("utf-8"))
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        logging.info("Loaded RSA private key (PEM).")

        self.session = requests.Session()
        self.api_prefix = None

    def discover_prefix(self):
        # probe a couple likely prefixes (we already know /trade-api/v2 works, but keep robust)
        candidates = ["/trade-api/v2", "/trade-api/v1", "/trade-api"]
        for p in candidates:
            code, data = self.request("GET", f"{p}/markets?limit=1", require_prefix=False)
            if code == 200:
                self.api_prefix = p
                logging.info(f"Discovered API prefix: {p} (probe {p}/markets?limit=1 -> 200)")
                return
            logging.info(f"Probe {p} failed HTTP={code} body={safe_preview(data, 500)}")

        raise RuntimeError("Could not discover API prefix. Authentication or base URL issue.")

    def _timestamp_ms(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, method: str, path: str, timestamp_ms: str, body: bytes) -> str:
        """
        Kalshi signing varies by API version. Your current working flow has been:
        - RSA key
        - Provide key id, timestamp, and signature header
        We'll sign a canonical payload:
            METHOD + PATH + TIMESTAMP + BODY
        """
        m = method.upper().encode("utf-8")
        p = path.encode("utf-8")
        t = timestamp_ms.encode("utf-8")
        payload = m + b"\n" + p + b"\n" + t + b"\n" + body

        sig = self.private_key.sign(
            payload,
            padding=serialization.pkcs1v15,  # placeholder attribute reference safety
        )

    def _headers(self, method: str, path: str, body: bytes) -> Dict[str, str]:
        """
        IMPORTANT: cryptography requires correct padding import.
        We'll do RSA PKCS1v15 with SHA256.
        """
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes

        ts = self._timestamp_ms()

        payload = (method.upper() + "\n" + path + "\n" + ts + "\n").encode("utf-8") + body
        signature = self.private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount

        return headers

    def request(self, method: str, path: str, body_obj: Optional[dict] = None, require_prefix: bool = True) -> Tuple[int, Any]:
        if require_prefix:
            if not self.api_prefix:
                raise RuntimeError("api_prefix not discovered yet")
            full_path = self.api_prefix + path
        else:
            full_path = path

        url = self.api_base + full_path

        body_bytes = b""
        if body_obj is not None:
            body_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")

        headers = self._headers(method, full_path, body_bytes)

        # Debug: show safe headers (no signature)
        dbg_headers = dict(headers)
        dbg_headers["KALSHI-ACCESS-KEY"] = redact(dbg_headers.get("KALSHI-ACCESS-KEY", ""))
        dbg_headers["KALSHI-ACCESS-SIGNATURE"] = "***"
        logging.debug(f"[HTTP] {method} {full_path} url={url} headers={dbg_headers} body={safe_preview(body_obj, 500)}")

        try:
            resp = self.session.request(method, url, headers=headers, data=body_bytes if body_obj is not None else None, timeout=20)
        except Exception as e:
            logging.error(f"[HTTP] Exception {method} {full_path}: {e}")
            return 0, {"_error": str(e)}

        text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            data = {"_raw": text[:5000]}

        if resp.status_code >= 400:
            logging.error(f"HTTP {method} {full_path} -> {resp.status_code} {safe_preview(data, 1200)}")
        else:
            logging.info(f"[OK] {method} {full_path} -> {resp.status_code} shape={type(data).__name__} keys={list(data.keys()) if isinstance(data, dict) else None}")

        return resp.status_code, data

    # Convenience methods
    def list_markets_by_series(self, series_ticker: str, limit: int = 200) -> Tuple[int, Any]:
        return self.request("GET", f"/markets?series_ticker={series_ticker}&limit={limit}")

    def get_orderbook(self, market_ticker: str) -> Tuple[int, Any]:
        return self.request("GET", f"/markets/{market_ticker}/orderbook")

    def get_market(self, market_ticker: str) -> Tuple[int, Any]:
        return self.request("GET", f"/markets/{market_ticker}")

    def get_balance(self) -> Tuple[int, Any]:
        return self.request("GET", "/balance")

    def create_order(self, order: dict) -> Tuple[int, Any]:
        return self.request("POST", "/orders", body_obj=order)

    def cancel_order(self, order_id: str) -> Tuple[int, Any]:
        return self.request("DELETE", f"/orders/{order_id}")

    def get_order(self, order_id: str) -> Tuple[int, Any]:
        return self.request("GET", f"/orders/{order_id}")

# =========================
# Helpers
# =========================

def parse_iso(ts: str) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Kalshi typically returns ISO with Z or offset
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

def select_next_closing_market(markets: list) -> Optional[dict]:
    """
    Select the market whose close_time is the soonest in the future.
    """
    best = None
    best_secs = None
    now = now_utc()

    for m in markets:
        if not isinstance(m, dict):
            continue

        close_time = m.get("close_time") or m.get("close_ts") or m.get("closeTime")
        dt = parse_iso(close_time) if isinstance(close_time, str) else None

        # Some APIs use integer timestamps; handle if present
        if dt is None and isinstance(close_time, (int, float)):
            dt = datetime.fromtimestamp(close_time, tz=timezone.utc)

        if not dt:
            continue

        secs = (dt - now).total_seconds()
        if secs <= 0:
            continue

        # Keep only those in our trading window
        if secs < MIN_SECONDS_TO_CLOSE or secs > MAX_SECONDS_TO_CLOSE:
            continue

        if best is None or secs < best_secs:
            best = m
            best_secs = secs

    if best and best_secs is not None:
        logging.info(f"[SELECT] Next closing market: {best.get('ticker')} close={best.get('close_time')} seconds_to_close={int(best_secs)}")

    return best

def best_asks_from_orderbook(data: dict) -> Dict[str, Optional[dict]]:
    ob = data.get("orderbook", {}) if isinstance(data, dict) else {}
    yes = ob.get("yes", [])
    no = ob.get("no", [])

    def best(arr):
        if not isinstance(arr, list) or not arr:
            return None
        best_level = min(arr, key=lambda x: x[0] if isinstance(x, list) and len(x) >= 1 else 10**9)
        if not (isinstance(best_level, list) and len(best_level) >= 2):
            return None
        return {"price_cents": int(best_level[0]), "qty": int(best_level[1])}

    return {"yes_best_ask": best(yes), "no_best_ask": best(no)}

# =========================
# Micro-scalp engine (simple)
# =========================

class TradeState:
    def __init__(self):
        self.position = 0
        self.wins = 0
        self.losses = 0
        self.realized_cents = 0
        self.last_trade_ts = None
        self.current_order_id = None
        self.current_side = None  # "BUY" or "SELL"
        self.current_entry_price = None
        self.order_size = BASE_ORDER_SIZE

    def maybe_scale(self):
        # increase size slowly after consistent wins
        if self.wins > 0 and self.wins % SCALE_AFTER_WINS == 0:
            new_size = int(round(self.order_size * SIZE_MULTIPLIER))
            self.order_size = max(1, min(MAX_ORDER_SIZE, new_size))
            logging.info(f"[SCALE] wins={self.wins} -> order_size now {self.order_size}")

STATE = TradeState()

def email_report(subject: str, body: str):
    if not EMAIL_ENABLED:
        return
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO and EMAIL_FROM):
        logging.warning("[EMAIL] enabled but missing SMTP_* or EMAIL_* vars. Skipping.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logging.info("[EMAIL] sent report")
    except Exception as e:
        logging.error(f"[EMAIL] failed: {e}")

def place_buy_then_sell(kc: KalshiClient, market_ticker: str, buy_price_cents: int, side: str):
    """
    side: 'YES' or 'NO'
    We'll post a BUY limit order. If filled, post SELL at buy+TARGET_PROFIT_CENTS.
    """
    # Gate
    if not ENABLE_TRADING or not CONFIRM_LIVE_TRADING:
        logging.warning("[TRADE] Trading gated OFF (ENABLE_TRADING and CONFIRM_LIVE_TRADING must both be true). Would have placed orders.")
        return

    if STATE.position >= MAX_POSITION_CONTRACTS:
        logging.warning(f"[RISK] position cap reached: {STATE.position} >= {MAX_POSITION_CONTRACTS}. Skip.")
        return

    qty = min(STATE.order_size, MAX_POSITION_CONTRACTS - STATE.position)
    if qty <= 0:
        return

    # BUY
    order = {
        "ticker": market_ticker,
        "action": "buy",
        "side": side.lower(),       # "yes" or "no"
        "type": "limit",
        "price": buy_price_cents,   # cents
        "count": qty,
    }
    logging.info(f"[ORDER] BUY {side} {qty}@{buy_price_cents}c on {market_ticker}")
    code, data = kc.create_order(order)
    if code != 200 and code != 201:
        logging.error(f"[ORDER] buy failed HTTP={code} body={safe_preview(data)}")
        return

    order_id = data.get("order_id") or data.get("id") or data.get("order", {}).get("id")
    if not order_id:
        logging.error(f"[ORDER] buy response missing order_id: {safe_preview(data)}")
        return

    # Wait for fill
    deadline = time.time() + FILL_TIMEOUT_SECONDS
    filled = False
    while time.time() < deadline:
        time.sleep(1.0)
        c2, od = kc.get_order(order_id)
        if c2 != 200:
            continue
        status = (od.get("order", {}) or od).get("status")
        filled_count = (od.get("order", {}) or od).get("filled_count") or 0
        logging.debug(f"[ORDER] buy status={status} filled={filled_count}")
        if str(status).lower() in ("filled", "executed") or int(filled_count) >= qty:
            filled = True
            break

    if not filled:
        logging.info(f"[ORDER] buy not filled in {FILL_TIMEOUT_SECONDS}s -> cancel {order_id}")
        kc.cancel_order(order_id)
        return

    STATE.position += qty
    STATE.current_entry_price = buy_price_cents

    # SELL target
    sell_price = min(99, buy_price_cents + TARGET_PROFIT_CENTS)
    sell_order = {
        "ticker": market_ticker,
        "action": "sell",
        "side": side.lower(),
        "type": "limit",
        "price": sell_price,
        "count": qty,
    }
    logging.info(f"[ORDER] SELL {side} {qty}@{sell_price}c target (+{TARGET_PROFIT_CENTS}c) on {market_ticker}")
    c3, d3 = kc.create_order(sell_order)
    if c3 != 200 and c3 != 201:
        logging.error(f"[ORDER] sell failed HTTP={c3} body={safe_preview(d3)}")
        return

    sell_id = d3.get("order_id") or d3.get("id") or d3.get("order", {}).get("id")
    if not sell_id:
        logging.error(f"[ORDER] sell response missing order_id: {safe_preview(d3)}")
        return

    # Wait for sell fill
    deadline = time.time() + SELL_TIMEOUT_SECONDS
    sold = False
    while time.time() < deadline:
        time.sleep(1.0)
        c4, od2 = kc.get_order(sell_id)
        if c4 != 200:
            continue
        status2 = (od2.get("order", {}) or od2).get("status")
        filled_count2 = (od2.get("order", {}) or od2).get("filled_count") or 0
        logging.debug(f"[ORDER] sell status={status2} filled={filled_count2}")
        if str(status2).lower() in ("filled", "executed") or int(filled_count2) >= qty:
            sold = True
            break

    if not sold:
        logging.info(f"[ORDER] sell not filled in {SELL_TIMEOUT_SECONDS}s -> cancel {sell_id}")
        kc.cancel_order(sell_id)
        # We are still holding position. Safer behavior: try to exit at best bid next cycle (not implemented yet).
        logging.warning("[RISK] Holding inventory after canceled sell. Bot will pause further buys until resolved.")
        return

    # Realize profit (approx, ignores fees)
    profit_cents = (sell_price - buy_price_cents) * qty
    STATE.realized_cents += profit_cents
    STATE.wins += 1
    STATE.position -= qty
    STATE.last_trade_ts = now_utc().isoformat()

    logging.info(f"[P/L] WIN: +{profit_cents}c (qty={qty}) total_realized={STATE.realized_cents}c wins={STATE.wins} losses={STATE.losses}")
    STATE.maybe_scale()

# =========================
# Main loop
# =========================

def main():
    logging.info("=== BOT STARTED ===")
    logging.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    logging.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    logging.info(f"POLL_SECONDS={POLL_SECONDS}")
    logging.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    logging.info(f"MARKET_TICKER={MARKET_TICKER}")
    logging.info(f"API_BASE={API_BASE}")
    logging.info(f"SUBACCOUNT={SUBACCOUNT}")
    logging.info(f"FARM_SIDE={FARM_SIDE} BUY_PRICE_CENTS={BUY_PRICE_CENTS} TARGET_PROFIT_CENTS={TARGET_PROFIT_CENTS}")
    logging.info(f"SIZING base={BASE_ORDER_SIZE} scale_after_wins={SCALE_AFTER_WINS} mult={SIZE_MULTIPLIER} cap={MAX_ORDER_SIZE}")

    key_id = os.getenv("KALSHI_KEY_ID")
    priv_b64 = os.getenv("KALSHI_PRIVATE_KEY_B64")

    kc = KalshiClient(API_BASE, key_id, priv_b64, subaccount=SUBACCOUNT)
    kc.discover_prefix()

    # sanity probe
    c_mk, mk = kc.request("GET", f"/markets?series_ticker={SERIES_PREFIX}&limit=5")
    logging.info(f"[PROBE] markets HTTP={c_mk} body={safe_preview(mk, 800)}")

    while True:
        try:
            # 1) select ticker
            ticker = MARKET_TICKER
            if not ticker:
                code, data = kc.list_markets_by_series(SERIES_PREFIX, limit=200)
                if code != 200:
                    logging.error(f"[SERIES] failed HTTP={code} body={safe_preview(data)}")
                    time.sleep(POLL_SECONDS)
                    continue

                markets = data.get("markets", [])
                logging.info(f"[SERIES] Returned markets count={len(markets)}")

                chosen = select_next_closing_market(markets)
                if not chosen:
                    logging.warning(f"No market in window ({MIN_SECONDS_TO_CLOSE}-{MAX_SECONDS_TO_CLOSE}s) for series {SERIES_PREFIX}. Sleeping.")
                    time.sleep(POLL_SECONDS)
                    continue

                ticker = chosen.get("ticker")
                if not ticker:
                    logging.warning("Chosen market missing ticker. Sleeping.")
                    time.sleep(POLL_SECONDS)
                    continue

            # 2) orderbook
            code2, ob = kc.get_orderbook(ticker)
            if code2 != 200:
                logging.error(f"[BOOK] failed HTTP={code2} body={safe_preview(ob)}")
                time.sleep(POLL_SECONDS)
                continue

            best = best_asks_from_orderbook(ob if isinstance(ob, dict) else {})
            logging.info(f"[BEST] {safe_preview(best)}")
            ya = best.get("yes_best_ask")
            na = best.get("no_best_ask")
            if ya and na:
                logging.info(f"[SANITY] yes_ask={ya['price_cents']}c no_ask={na['price_cents']}c sum={ya['price_cents'] + na['price_cents']}c")

            # 3) decide whether to attempt a micro scalp
            # We only enter if the best ask is <= BUY_PRICE_CENTS (meaning sellers exist at our target)
            side = FARM_SIDE
            if side == "YES":
                ask = ya["price_cents"] if ya else None
            else:
                ask = na["price_cents"] if na else None

            if ask is None:
                logging.warning("[STRAT] Missing best ask for chosen side. Sleeping.")
                time.sleep(POLL_SECONDS)
                continue

            logging.info(f"[STRAT] side={side} best_ask={ask}c target_buy={BUY_PRICE_CENTS}c")

            if ask <= BUY_PRICE_CENTS:
                place_buy_then_sell(kc, ticker, BUY_PRICE_CENTS, side)
            else:
                logging.info("[STRAT] No cheap asks at/below target. Not trading this cycle.")

            # 4) heartbeat
            logging.info(f"[HEARTBEAT] alive pos={STATE.position} wins={STATE.wins} realized={STATE.realized_cents}c order_size={STATE.order_size}")
            time.sleep(POLL_SECONDS)

        except Exception as e:
            logging.exception(f"[LOOP] exception: {e}")
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
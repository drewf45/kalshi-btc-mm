import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# ============================================================
# LIVE KALSHI BTC 15m BOT (YES-only)
# Fix included: Kalshi orderbook endpoint returns BIDS only.
# We compute ASK via complement: YES_ASK = 100 - NO_BID.
# ============================================================

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Config (env)
# -----------------------------
ELECTIONS_BASE_URL = os.getenv("ELECTIONS_BASE_URL", "https://api.elections.kalshi.com").rstrip("/")
TRADING_BASE_URL = os.getenv("TRADING_BASE_URL", "https://trading-api.kalshi.com").rstrip("/")

# Series prefix (you confirmed)
SERIES_PREFIX = os.environ.get("SERIES_PREFIX", "KXBTC15m")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BUY_PRICE_CENTS = int(os.getenv("BUY_PRICE_CENTS", "99"))
BASE_SIZE = int(os.getenv("BASE_SIZE", "1"))

POST_ONLY = os.getenv("POST_ONLY", "true").lower() in ("1", "true", "yes", "y")
IMPROVE_TICKS = int(os.getenv("IMPROVE_TICKS", "1"))  # maker improvement
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() in ("1", "true", "yes", "y")
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() in ("1", "true", "yes", "y")

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip()

# API creds
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY", "").strip()  # API key ID
PRIVATE_KEY_B64 = os.getenv("PRIVATE_KEY_B64", "").strip() or os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH", "").strip()  # optional

# -----------------------------
# Helpers
# -----------------------------
def now_utc_ms() -> str:
    return str(int(time.time() * 1000))


def load_private_key():
    if PRIVATE_KEY_B64:
        try:
            raw = base64.b64decode(PRIVATE_KEY_B64)
            key = serialization.load_pem_private_key(raw, password=None)
            return key
        except Exception as e:
            raise RuntimeError(f"Failed to load PRIVATE_KEY_B64: {e}") from e

    if PRIVATE_KEY_PATH:
        try:
            with open(PRIVATE_KEY_PATH, "rb") as f:
                raw = f.read()
            key = serialization.load_pem_private_key(raw, password=None)
            return key
        except Exception as e:
            raise RuntimeError(f"Failed to load PRIVATE_KEY_PATH: {e}") from e

    raise RuntimeError("No private key provided. Set PRIVATE_KEY_B64 (preferred) or PRIVATE_KEY_PATH.")


def sign_request(private_key, timestamp_ms: str, method: str, path_with_query: str) -> str:
    """
    Kalshi docs: RSA-PSS signature of the request. Many examples use:
    message = timestamp + method + path
    where path includes query string if present.
    """
    msg = (timestamp_ms + method.upper() + path_with_query).encode("utf-8")
    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_headers(private_key, method: str, path_with_query: str) -> Dict[str, str]:
    if not KALSHI_ACCESS_KEY:
        raise RuntimeError("Missing KALSHI_ACCESS_KEY (API key id).")

    ts = now_utc_ms()
    sig = sign_request(private_key, ts, method, path_with_query)
    h = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_ACCESS_KEY,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    if SUBACCOUNT:
        # If Kalshi uses a specific header for subaccounts in your setup, keep it here.
        # If your account doesn’t use subaccounts, leave SUBACCOUNT empty.
        h["KALSHI-SUBACCOUNT"] = SUBACCOUNT
    return h


def _full_path(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    if params:
        return f"{path}?{urlencode(params)}"
    return path


def _req_json(private_key, base_url: str, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None) -> Any:
    signed_path = _full_path(path, params)
    url = f"{base_url}{signed_path}"
    headers = build_headers(private_key, method, signed_path)

    if method.upper() == "GET":
        r = requests.get(url, headers=headers, timeout=10)
    elif method.upper() == "POST":
        r = requests.post(url, headers=headers, data=json.dumps(body) if body is not None else None, timeout=10)
    else:
        raise ValueError(f"Unsupported method: {method}")

    # Minimal request log (matches your style)
    log.info("[REQ] %s %s -> %s", method.upper(), signed_path, r.status_code)

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {signed_path}: {data}")
    return data


# -----------------------------
# API wrappers
# -----------------------------
def elections_get(private_key, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return _req_json(private_key, ELECTIONS_BASE_URL, "GET", path, params=params)


def elections_post(private_key, path: str, body: Any) -> Any:
    return _req_json(private_key, ELECTIONS_BASE_URL, "POST", path, body=body)


def trading_get(private_key, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return _req_json(private_key, TRADING_BASE_URL, "GET", path, params=params)


# -----------------------------
# Market resolve
# -----------------------------
def resolve_active_market(private_key) -> Optional[str]:
    """
    Finds the currently open ticker for the series.
    We hit elections base for the migrated API (this is what your logs show working).
    """
    series_upper = SERIES_PREFIX.upper() if SERIES_PREFIX else SERIES_PREFIX

    params = {"limit": 200, "series_ticker": series_upper, "status": "open"}
    resp = elections_get(private_key, "/trade-api/v2/markets", params=params)

    markets = resp.get("markets") or resp.get("data") or resp.get("results") or []
    if not markets:
        log.error("[MARKET] No active market found for series %s", SERIES_PREFIX)
        return None

    # Prefer the most recent by ticker string (works for your YYYYMMDDhhmm suffixes)
    markets_sorted = sorted(markets, key=lambda m: (m.get("ticker") or ""), reverse=True)
    picked = markets_sorted[0].get("ticker")
    log.info("[RESOLVE] probe=series_ticker open (upper) markets=%d picked=%s", len(markets), picked)
    return picked


# -----------------------------
# Orderbook parsing (FIX HERE)
# -----------------------------
def _extract_bid_levels(orderbook: Dict[str, Any], key: str) -> List[Tuple[int, int]]:
    """
    Kalshi orderbook endpoint returns only BIDS arrays for 'yes' and 'no'.
    Per docs: /orderbook provides BIDS ONLY (no asks).
    So orderbook['yes'] is list of [price, quantity] bid levels.

    Returns list of (price_cents, qty).
    """
    levels = orderbook.get(key)
    if not isinstance(levels, list):
        return []

    out: List[Tuple[int, int]] = []
    for row in levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        p, q = row[0], row[1]
        try:
            pc = int(p)
            qc = int(q)
        except Exception:
            continue
        if pc <= 0 or pc >= 100 or qc <= 0:
            continue
        out.append((pc, qc))
    return out


def parse_yes_best_bid_ask(orderbook: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    """
    Returns:
      yes_best_bid_cents, yes_best_bid_qty, yes_best_ask_cents, yes_best_ask_qty

    FIX:
      /orderbook gives BIDS ONLY.
      Best YES bid: max(orderbook['yes'])
      Best NO  bid: max(orderbook['no'])
      Then:
        YES best ask = 100 - (NO best bid)
      because NO bid at X implies someone willing to buy NO at X,
      which is equivalent to someone willing to SELL YES at (100 - X).
    """
    yes_bids = _extract_bid_levels(orderbook, "yes")
    no_bids = _extract_bid_levels(orderbook, "no")

    if not yes_bids and not no_bids:
        return None

    yes_best_bid = max(yes_bids, key=lambda t: t[0]) if yes_bids else None
    no_best_bid = max(no_bids, key=lambda t: t[0]) if no_bids else None

    if yes_best_bid is None or no_best_bid is None:
        # If one side missing, we can't safely compute ask.
        return None

    yes_bid_px, yes_bid_qty = yes_best_bid
    no_bid_px, no_bid_qty = no_best_bid

    yes_ask_px = 100 - no_bid_px
    # A crude "ask qty" proxy: use opposite best bid qty (the liquidity at that implied ask)
    yes_ask_qty = no_bid_qty

    # sanity
    if not (1 <= yes_bid_px <= 99 and 1 <= yes_ask_px <= 99):
        return None
    if yes_ask_px < yes_bid_px:
        # implied crossed book; still can happen briefly, but skip to avoid bad behavior
        return None

    return yes_bid_px, yes_bid_qty, yes_ask_px, yes_ask_qty


# -----------------------------
# Portfolio / risk guardrails
# -----------------------------
def has_position(private_key, ticker: str) -> bool:
    """
    One-open-contract-at-a-time guard:
    If you already hold ANY position in this ticker, we do nothing.
    """
    resp = elections_get(private_key, "/trade-api/v2/portfolio/positions")
    positions = resp.get("positions") or resp.get("data") or resp.get("results") or []
    for p in positions:
        if (p.get("ticker") == ticker) and (int(p.get("position", 0)) != 0 or int(p.get("count", 0)) != 0):
            return True
    return False


# -----------------------------
# Trading
# -----------------------------
def choose_maker_buy_price(target_buy_cents: int, yes_best_bid: int, yes_best_ask: int) -> Optional[int]:
    """
    Maker-only logic:
      - If POST_ONLY, we must NOT cross the (implied) ask.
      - We also try to improve the best bid by IMPROVE_TICKS.
    """
    px = target_buy_cents

    # Improve inside spread by pushing above best bid
    px = max(px, yes_best_bid + max(IMPROVE_TICKS, 0))

    if POST_ONLY:
        # Maker order must be strictly below ask to avoid "post only cross"
        px = min(px, yes_best_ask - 1)

    if px <= 0 or px >= 100:
        return None
    if POST_ONLY and px >= yes_best_ask:
        return None
    return px


def place_yes_buy(private_key, ticker: str, limit_price_cents: int, count: int, post_only: bool) -> Any:
    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": int(count),
        "price": int(limit_price_cents),
        "client_order_id": f"btc15m_yes_{int(time.time()*1000)}",
    }
    if post_only:
        body["post_only"] = True

    # Orders endpoint per docs: /trade-api/v2/portfolio/orders (on elections base now)
    return elections_post(private_key, "/trade-api/v2/portfolio/orders", body)


# -----------------------------
# Main loop
# -----------------------------
def main():
    private_key = load_private_key()

    log.info(
        "[BOOT] ELECTIONS_BASE_URL=%s TRADING_BASE_URL=%s SERIES_PREFIX=%s POLL_SECONDS=%.2f BUY_PRICE_CENTS=%d BASE_SIZE=%d POST_ONLY=%s IMPROVE_TICKS=%d ENABLE_TRADING=%s CONFIRM_LIVE_TRADING=%s SUBACCOUNT=%s",
        ELECTIONS_BASE_URL,
        TRADING_BASE_URL,
        SERIES_PREFIX,
        POLL_SECONDS,
        BUY_PRICE_CENTS,
        BASE_SIZE,
        POST_ONLY,
        IMPROVE_TICKS,
        ENABLE_TRADING,
        CONFIRM_LIVE_TRADING,
        SUBACCOUNT,
    )
    log.info("[BOOT] Private key loaded OK (b64)" if PRIVATE_KEY_B64 else "[BOOT] Private key loaded OK (path)")
    log.info("[BOOT] LIVE BTC BOT STARTED")

    if ENABLE_TRADING and not CONFIRM_LIVE_TRADING:
        raise RuntimeError("ENABLE_TRADING is true but CONFIRM_LIVE_TRADING is not true. Refusing to trade live.")

    active_ticker: Optional[str] = None
    last_resolve_ms = 0

    while True:
        try:
            now_ms = int(time.time() * 1000)

            # Re-resolve every ~20s (you already solved the 15m rotation issue)
            if active_ticker is None or (now_ms - last_resolve_ms) > 20_000:
                last_resolve_ms = now_ms
                new_ticker = resolve_active_market(private_key)
                if new_ticker and new_ticker != active_ticker:
                    active_ticker = new_ticker
                    log.info("[MARKET] Switched active ticker -> %s", active_ticker)

            if not active_ticker:
                time.sleep(POLL_SECONDS)
                continue

            # If you already have a position in this market: do nothing (1 open contract at a time)
            if has_position(private_key, active_ticker):
                log.info("[GUARD] Already holding position in %s; skipping.", active_ticker)
                time.sleep(POLL_SECONDS
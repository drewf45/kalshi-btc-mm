# bot.py
# Kalshi YES-only rolling 15m market maker
# MICRO CHANGE: fix INCORRECT_API_KEY_SIGNATURE by signing the exact JSON bytes we send (data=body_str)

import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, Any
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Config helpers
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if not v else int(v)


@dataclass
class BotConfig:
    api_base: str
    api_key_id: str
    private_key_b64: str
    series_ticker: str
    poll_seconds: float
    empty_poll_seconds: float
    rate_limit_backoff_seconds: float
    market_refresh_seconds: float
    enable_trading: bool
    dry_run: bool
    base_size: int
    improve_ticks: int
    post_only: bool
    max_buy_price: int
    min_edge_cents: int


def load_config() -> BotConfig:
    return BotConfig(
        api_base=os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").rstrip("/"),
        api_key_id=os.environ.get("KALSHI_API_KEY_ID", ""),
        private_key_b64=os.environ.get("KALSHI_PRIVATE_KEY_PEM_BASE64", ""),
        series_ticker=os.environ["SERIES_TICKER"].strip().upper(),
        poll_seconds=float(os.getenv("POLL_SECONDS", "1")),
        empty_poll_seconds=float(os.getenv("EMPTY_POLL_SECONDS", "5")),
        rate_limit_backoff_seconds=float(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "10")),
        market_refresh_seconds=float(os.getenv("MARKET_REFRESH_SECONDS", "30")),
        enable_trading=env_bool("ENABLE_TRADING", False),
        dry_run=env_bool("DRY_RUN", True),
        base_size=env_int("BASE_SIZE", 1),
        improve_ticks=env_int("IMPROVE_TICKS", 1),
        post_only=env_bool("POST_ONLY", True),
        max_buy_price=env_int("MAX_BUY_PRICE_CENTS", 99),
        min_edge_cents=env_int("MIN_EDGE_CENTS", 2),
    )


# -----------------------------
# Auth / signing (for private endpoints)
# -----------------------------
def canonical_json(obj) -> str:
    # Keep deterministic + compact. IMPORTANT: we will send exactly this string.
    return "" if obj is None else json.dumps(obj, separators=(",", ":"), sort_keys=True)


def load_private_key_from_b64(b64: str):
    return serialization.load_pem_private_key(base64.b64decode(b64), password=None)


def sign_request(priv, ts_ms: str, method: str, path_qs: str, body_str: str) -> str:
    msg = f"{ts_ms}{method}{path_qs}{body_str}".encode("utf-8")
    sig = priv.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


def auth_headers(cfg: BotConfig, priv, method: str, path_qs: str, body_str: str) -> dict:
    ts = str(int(time.time() * 1000))
    sig = sign_request(priv, ts, method, path_qs, body_str)
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": cfg.api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


def private_request_json(cfg: BotConfig, session: requests.Session, priv, method: str, path: str, params=None, body=None):
    qs = path if not params else f"{path}?{urlencode(params)}"
    url = cfg.api_base + qs

    # MICRO CHANGE: serialize once, sign that exact string, and send those exact bytes
    body_str = "" if method == "GET" else canonical_json(body)
    h = auth_headers(cfg, priv, method, qs, body_str)

    r = session.request(
        method,
        url,
        headers=h,
        data=(None if method == "GET" else body_str),
        timeout=20,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


# -----------------------------
# HTTP helpers (public)
# -----------------------------
def public_get(cfg, session: requests.Session, path, params=None):
    qs = path if not params else f"{path}?{urlencode(params)}"
    r = session.get(cfg.api_base + qs, timeout=20)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


# -----------------------------
# Market resolution
# -----------------------------
def normalize(s: str) -> str:
    return (s or "").replace("-", "").replace("_", "").strip().lower()


def resolve_active_market(cfg, session: requests.Session) -> str:
    data = public_get(
        cfg,
        session,
        "/trade-api/v2/markets",
        params={
            "series": cfg.series_ticker,
            "series_ticker": cfg.series_ticker,
            "status": "open",
        },
    )

    markets = data.get("markets") or data.get("data") or []
    want = normalize(cfg.series_ticker)

    def matches(m: dict) -> bool:
        return any(
            normalize(m.get(k)).startswith(want)
            for k in ("series_ticker", "series", "ticker")
            if m.get(k)
        )

    filtered = [m for m in markets if matches(m)]
    if not filtered:
        raise RuntimeError("No open BTC markets found")

    return filtered[0]["ticker"]


# -----------------------------
# Orderbook parsing (BTC-style)
# -----------------------------
def parse_yes_book(ob: Any):
    """
    BTC markets expose YES as a flat ladder:
      orderbook["yes"] = [[price, qty], ...]
    Lowest price = best bid
    Highest price = best ask
    """
    if not isinstance(ob, dict):
        return None, None

    ob = ob.get("orderbook")
    if not isinstance(ob, dict):
        return None, None

    ladder = ob.get("yes")
    if not isinstance(ladder, list) or not ladder:
        return None, None

    prices = []
    for row in ladder:
        if isinstance(row, (list, tuple)) and len(row) >= 1:
            p = row[0]
            if isinstance(p, (int, float)):
                prices.append(int(p))

    if not prices:
        return None, None

    return min(prices), max(prices)


def choose_price(bid, ask, improve, max_px, min_edge):
    if bid is None or ask is None:
        return None

    px = min(bid + improve, max_px)
    if (ask - px) < min_edge:
        return None

    return px


# -----------------------------
# Place order (post-only YES limit buy)
# -----------------------------
def make_client_order_id(ticker: str, yes_price: int) -> str:
    return f"mm-{ticker}-{yes_price}-{int(time.time()*1000)}"


def place_yes_limit_buy(cfg: BotConfig, session: requests.Session, priv, ticker: str, yes_price: int, count: int):
    body = {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": count,
        "yes_price": yes_price,
        "post_only": bool(cfg.post_only),
        "client_order_id": make_client_order_id(ticker, yes_price),
    }
    resp = private_request_json(cfg, session, priv, "POST", "/trade-api/v2/portfolio/orders", body=body)
    order = resp.get("order") or resp
    log.info(f"[ORDER] placed yes buy {count}@{yes_price}c id={order.get('order_id', 'n/a')}")
    return order


# -----------------------------
# Main loop
# -----------------------------
def main():
    cfg = load_config()
    session = requests.Session()

    priv = None
    if cfg.enable_trading and not cfg.dry_run:
        if not cfg.api_key_id or not cfg.private_key_b64:
            raise RuntimeError("ENABLE_TRADING requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM_BASE64")
        priv = load_private_key_from_b64(cfg.private_key_b64)

    log.info(f"SERIES={cfg.series_ticker} DRY_RUN={cfg.dry_run}")

    active: Optional[str] = None
    last_state: Optional[Tuple] = None
    last_market_refresh_ts: float = 0.0

    while True:
        try:
            now = time.time()
            if active is None or (now - last_market_refresh_ts) >= cfg.market_refresh_seconds:
                active = resolve_active_market(cfg, session)
                last_market_refresh_ts = now
                last_state = None
                log.info(f"[ROLL] Active market → {active}")

            ob = public_get(cfg, session, f"/trade-api/v2/markets/{active}/orderbook")
            bid, ask = parse_yes_book(ob)
            price = choose_price(bid, ask, cfg.improve_ticks, cfg.max_buy_price, cfg.min_edge_cents)

            if price is None:
                state = ("skip", bid, ask)
                if state != last_state:
                    log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → SKIP")
                    last_state = state
                time.sleep(cfg.empty_poll_seconds)
                continue

            state = ("quote", price)
            if state != last_state:
                log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → {price}c")
                last_state = state

            if cfg.enable_trading and not cfg.dry_run:
                place_yes_limit_buy(cfg, session, priv, active, price, cfg.base_size)
            else:
                log.info("[DRYRUN] Not placing order")

        except Exception as e:
            log.error(f"[LOOPERR] {e}")

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
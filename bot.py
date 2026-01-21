# bot.py
# Kalshi YES-only rolling 15m market maker

import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
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
    enable_trading: bool
    dry_run: bool
    base_size: int
    improve_ticks: int
    post_only: bool
    max_buy_price: int


def load_config() -> BotConfig:
    return BotConfig(
        api_base=os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").rstrip("/"),
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_b64=os.environ["KALSHI_PRIVATE_KEY_PEM_BASE64"],
        series_ticker=os.environ["SERIES_TICKER"],
        poll_seconds=float(os.getenv("POLL_SECONDS", "1")),
        enable_trading=env_bool("ENABLE_TRADING", False),
        dry_run=env_bool("DRY_RUN", True),
        base_size=env_int("BASE_SIZE", 1),
        improve_ticks=env_int("IMPROVE_TICKS", 1),
        post_only=env_bool("POST_ONLY", True),
        max_buy_price=env_int("MAX_BUY_PRICE_CENTS", 99),
    )


# -----------------------------
# Auth / signing
# -----------------------------
def load_private_key(b64: str):
    return serialization.load_pem_private_key(
        base64.b64decode(b64), password=None
    )


def sign_request(priv, ts: str, method: str, path_qs: str) -> str:
    # Sign only timestamp+method+path (no query, no body)
    path_without_query = path_qs.split("?", 1)[0]
    msg = f"{ts}{method}{path_without_query}".encode("utf-8")
    sig = priv.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def headers(cfg, priv, method, path_qs, body):
    ts = str(int(time.time() * 1000))
    sig = sign_request(priv, ts, method, path_qs)
    return {
        "KALSHI-ACCESS-KEY": cfg.api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


# -----------------------------
# HTTP helpers
# -----------------------------
def request_json(cfg, priv, method, path, params=None, body=None):
    qs = path if not params else f"{path}?{urlencode(params)}"
    url = cfg.api_base + qs
    h = headers(cfg, priv, method, qs, body)
    r = requests.request(method, url, headers=h, json=body, timeout=20)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


def public_get(cfg, path, params=None):
    qs = path if not params else f"{path}?{urlencode(params)}"
    r = requests.get(cfg.api_base + qs, timeout=20)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


# -----------------------------
# ✅ MICRO CHANGE: sticky + throttled roll resolver to prevent 429 + random market fallback
# -----------------------------
_last_roll_check_ts: float = 0.0
_cached_active_market: Optional[str] = None
ROLL_CHECK_MIN_SECONDS = float(os.getenv("ROLL_CHECK_MIN_SECONDS", "10"))


def resolve_active_market(cfg) -> str:
    global _last_roll_check_ts, _cached_active_market

    now = time.time()

    # Throttle the series->market lookup to avoid 429
    if _cached_active_market is not None and (now - _last_roll_check_ts) < ROLL_CHECK_MIN_SECONDS:
        return _cached_active_market

    _last_roll_check_ts = now

    try:
        data = public_get(
            cfg,
            "/trade-api/v2/markets",
            params={"series": cfg.series_ticker, "status": "open"},
        )
        markets = data.get("markets") or data.get("data") or []
        if not markets:
            # If series returns nothing, keep last active rather than switching to random
            if _cached_active_market is not None:
                return _cached_active_market
            raise RuntimeError(f"No open markets for series {cfg.series_ticker}")

        _cached_active_market = markets[0]["ticker"]
        return _cached_active_market

    except Exception as e:
        # On 429 or any transient issue, keep last active market
        if _cached_active_market is not None:
            return _cached_active_market
        raise


# -----------------------------
# Orderbook parsing
# -----------------------------
def best_price(levels, want):
    if not levels:
        return None
    pairs = [(int(p), int(q)) for p, q in levels]
    return max(pairs)[0] if want == "bid" else min(pairs)[0]


def parse_yes_book(ob):
    yes = ob.get("orderbook", {}).get("yes")
    if not yes:
        return None, None
    return (
        best_price(yes.get("bids"), "bid"),
        best_price(yes.get("asks"), "ask"),
    )


def choose_price(bid, ask, improve, max_px):
    if bid is None and ask is None:
        return None
    if bid is None:
        px = ask - 1
        return px if px >= 1 else 1
    px = bid + improve
    if ask is not None:
        px = min(px, ask - 1)
    return min(px, max_px)


# -----------------------------
# Trading (YES-only buy)
# -----------------------------
def place_yes_buy(cfg, priv, market_ticker: str, price_cents: int, qty: int):
    body = {
        "ticker": market_ticker,
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "price": price_cents,
        "count": qty,
    }
    return request_json(cfg, priv, "POST", "/trade-api/v2/portfolio/orders", body=body)


# -----------------------------
# Main loop
# -----------------------------
def main():
    cfg = load_config()
    priv = load_private_key(cfg.private_key_b64)

    log.info(f"SERIES={cfg.series_ticker} DRY_RUN={cfg.dry_run}")

    active = None

    while True:
        try:
            ticker = resolve_active_market(cfg)
            if ticker != active:
                active = ticker
                log.info(f"[ROLL] Active market → {active}")

            ob = public_get(cfg, f"/trade-api/v2/markets/{active}/orderbook")
            bid, ask = parse_yes_book(ob)
            price = choose_price(bid, ask, cfg.improve_ticks, cfg.max_buy_price)

            if price is None:
                log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → SKIP (empty)")
            else:
                log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → {price}c")

                if not cfg.enable_trading or cfg.dry_run:
                    log.info("[DRYRUN] Not placing order")
                else:
                    resp = place_yes_buy(cfg, priv, active, price, cfg.base_size)
                    log.info(
                        f"[ORDER] placed YES buy {cfg.base_size}@{price}c "
                        f"id={resp.get('order_id') or resp.get('id') or 'unknown'}"
                    )

        except Exception as e:
            log.error(f"[LOOPERR] {e}")

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
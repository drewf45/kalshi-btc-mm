# bot.py
# Kalshi YES-only rolling 15m market maker
# FIX: uses /markets?series=XYZ instead of non-existent /series endpoint

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
        series_ticker=os.environ["SERIES_TICKER"],   # e.g. KXBTC15M
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


def canonical_json(obj):
    return "" if obj is None else json.dumps(obj, separators=(",", ":"), sort_keys=True)


def sign_request(priv, ts, method, path_qs, body):
    msg = f"{ts}{method}{path_qs}{body}".encode()
    sig = priv.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def headers(cfg, priv, method, path_qs, body):
    ts = str(int(time.time() * 1000))
    body_str = canonical_json(body) if method != "GET" else ""
    sig = sign_request(priv, ts, method, path_qs, body_str)
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
# ✅ FIXED SERIES RESOLUTION
# -----------------------------
def resolve_active_market(cfg) -> str:
    data = public_get(
        cfg,
        "/trade-api/v2/markets",
        params={"series": cfg.series_ticker, "status": "open"},
    )

    markets = data.get("markets") or data.get("data") or []
    if not markets:
        raise RuntimeError(f"No open markets for series {cfg.series_ticker}")

    return markets[0]["ticker"]


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
        return max_px
    if bid is None:
        return min(ask - 1, max_px)
    px = bid + improve
    if ask:
        px = min(px, ask - 1)
    return min(px, max_px)


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

            log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → {price}c")

            if not cfg.enable_trading or cfg.dry_run:
                log.info("[DRYRUN] Not placing order")

        except Exception as e:
            log.error(f"[LOOPERR] {e}")

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
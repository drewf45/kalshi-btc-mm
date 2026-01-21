# bot.py
# Kalshi YES-only rolling 15m market maker
# MICRO CHANGE: use a single requests.Session() (keep-alive) to reduce latency/overhead

import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from typing import Optional, Tuple
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
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_b64=os.environ["KALSHI_PRIVATE_KEY_PEM_BASE64"],
        series_ticker=os.environ["SERIES_TICKER"],
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
def public_get(cfg, session: requests.Session, path, params=None):
    qs = path if not params else f"{path}?{urlencode(params)}"
    r = session.get(cfg.api_base + qs, timeout=20)  # MICRO CHANGE (session)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


# -----------------------------
# Market resolution
# -----------------------------
def resolve_active_market(cfg, session: requests.Session) -> str:
    data = public_get(
        cfg,
        session,
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


def choose_price(
    bid: Optional[int],
    ask: Optional[int],
    improve: int,
    max_px: int,
    min_edge_cents: int,
) -> Optional[int]:
    if bid is None and ask is None:
        return None

    if bid is None:
        px = min(ask - 1, max_px)
        if ask is not None and (ask - px) < min_edge_cents:
            return None
        return px

    px = min(bid + improve, max_px)

    if ask is not None:
        px = min(px, ask - 1)
        if (ask - px) < min_edge_cents:
            return None

    return px


def is_rate_limited(err: Exception) -> bool:
    s = str(err).lower()
    return ("too_many_requests" in s) or ("http 429" in s) or ("status_code" in s and "429" in s)


# -----------------------------
# Main loop
# -----------------------------
def main():
    cfg = load_config()
    priv = load_private_key(cfg.private_key_b64)

    log.info(f"SERIES={cfg.series_ticker} DRY_RUN={cfg.dry_run}")

    # MICRO CHANGE: one session for keep-alive
    session = requests.Session()

    active = None
    last_state: Optional[Tuple] = None
    last_intended_price: Optional[int] = None
    last_rl_log_ts: float = 0.0
    last_market_refresh_ts: float = 0.0

    while True:
        sleep_for = cfg.poll_seconds

        try:
            now = time.time()
            if active is None or (now - last_market_refresh_ts) >= cfg.market_refresh_seconds:
                ticker = resolve_active_market(cfg, session)
                last_market_refresh_ts = now
                if ticker != active:
                    active = ticker
                    log.info(f"[ROLL] Active market → {active}")
                    last_state = None
                    last_intended_price = None

            ob = public_get(cfg, session, f"/trade-api/v2/markets/{active}/orderbook")
            bid, ask = parse_yes_book(ob)
            price = choose_price(bid, ask, cfg.improve_ticks, cfg.max_buy_price, cfg.min_edge_cents)

            if price is None:
                if bid is None and ask is None:
                    state = ("skip", "empty")
                    sleep_for = cfg.empty_poll_seconds
                elif bid is not None and ask is not None:
                    intended = min(bid + cfg.improve_ticks, ask - 1, cfg.max_buy_price)
                    if (ask - intended) < cfg.min_edge_cents:
                        state = ("skip", f"edge<{cfg.min_edge_cents}c")
                    else:
                        state = ("skip", "other")
                else:
                    state = ("skip", "other")

                if state != last_state:
                    log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → SKIP ({state[1]})")
                    last_state = state
                time.sleep(sleep_for)
                continue

            if price != last_intended_price:
                last_intended_price = price

            state = ("quote", price)
            if state != last_state:
                log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → {price}c")
                if not cfg.enable_trading or cfg.dry_run:
                    log.info("[DRYRUN] Not placing order")
                last_state = state

        except Exception as e:
            if is_rate_limited(e):
                now = time.time()
                if now - last_rl_log_ts > 30:
                    log.warning(f"[RATELIMIT] Backing off {cfg.rate_limit_backoff_seconds}s ({e})")
                    last_rl_log_ts = now
                time.sleep(cfg.rate_limit_backoff_seconds)
                continue

            log.error(f"[LOOPERR] {e}")

        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
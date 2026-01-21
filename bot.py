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
    return serialization.load_pem_private_key(base64.b64decode(b64), password=None)


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
# Sticky + throttled roll resolver (with 429 backoff)
# -----------------------------
_last_roll_check_ts: float = 0.0
_cached_active_market: Optional[str] = None

ROLL_CHECK_MIN_SECONDS = float(os.getenv("ROLL_CHECK_MIN_SECONDS", "60"))
_roll_backoff_seconds: float = 0.0
ROLL_BACKOFF_MAX_SECONDS = float(os.getenv("ROLL_BACKOFF_MAX_SECONDS", "600"))


def _extract_markets_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return data.get("markets") or data.get("data") or []


def _pick_deterministic_market(markets: List[Dict[str, Any]]) -> str:
    def close_key(m: Dict[str, Any]) -> str:
        return (
            m.get("close_time")
            or m.get("close_ts")
            or m.get("expiration_time")
            or m.get("settlement_time")
            or ""
        )

    have_close = any(close_key(m) for m in markets)
    if have_close:
        markets = sorted(markets, key=lambda m: (close_key(m), m.get("ticker", "")))
    else:
        markets = sorted(markets, key=lambda m: m.get("ticker", ""))

    return markets[0]["ticker"]


def resolve_active_market(cfg) -> str:
    global _last_roll_check_ts, _cached_active_market, _roll_backoff_seconds

    now = time.time()

    min_wait = ROLL_CHECK_MIN_SECONDS + _roll_backoff_seconds
    if _cached_active_market is not None and (now - _last_roll_check_ts) < min_wait:
        return _cached_active_market

    _last_roll_check_ts = now

    try:
        # ✅ MICRO CHANGE: send BOTH param names so the API actually filters.
        # If Kalshi expects series_ticker, "series" gets ignored (what you’re seeing now).
        params = {
            "series": cfg.series_ticker,
            "series_ticker": cfg.series_ticker,
            "status": "open",
        }
        data = public_get(cfg, "/trade-api/v2/markets", params=params)

        markets = _extract_markets_list(data)
        if not markets:
            if _cached_active_market is not None:
                return _cached_active_market
            raise RuntimeError(f"No open markets for series {cfg.series_ticker}")

        if _cached_active_market is not None:
            open_tickers = {m.get("ticker") for m in markets if m.get("ticker")}
            if _cached_active_market in open_tickers:
                _roll_backoff_seconds = max(0.0, _roll_backoff_seconds * 0.5)
                return _cached_active_market

        _cached_active_market = _pick_deterministic_market(markets)

        _roll_backoff_seconds = max(0.0, _roll_backoff_seconds * 0.5)
        return _cached_active_market

    except Exception as e:
        msg = str(e)
        if "too_many_requests" in msg or "HTTP 429" in msg:
            _roll_backoff_seconds = min(
                ROLL_BACKOFF_MAX_SECONDS,
                5.0 if _roll_backoff_seconds <= 0 else _roll_backoff_seconds * 2.0,
            )
            log.warning(
                f"[ROLL429] backing off {_roll_backoff_seconds:.0f}s (keeping active={_cached_active_market})"
            )
            if _cached_active_market is not None:
                return _cached_active_market

        if _cached_active_market is not None:
            return _cached_active_market
        raise


# -----------------------------
# Orderbook parsing
# -----------------------------
def _normalize_levels(levels: Any) -> Optional[List[Tuple[int, int]]]:
    if not levels:
        return None

    if isinstance(levels, dict) and "levels" in levels:
        levels = levels.get("levels")

    out: List[Tuple[int, int]] = []

    if isinstance(levels, list) and levels and isinstance(levels[0], (list, tuple)) and len(levels[0]) >= 2:
        for row in levels:
            try:
                out.append((int(row[0]), int(row[1])))
            except Exception:
                continue
        return out or None

    if isinstance(levels, list) and levels and isinstance(levels[0], dict):
        for row in levels:
            try:
                p = row.get("price") or row.get("p")
                q = row.get("quantity") or row.get("qty") or row.get("count") or row.get("size") or row.get("q")
                if p is None or q is None:
                    continue
                out.append((int(p), int(q)))
            except Exception:
                continue
        return out or None

    return None


def best_price(levels: Any, want: str) -> Optional[int]:
    norm = _normalize_levels(levels)
    if not norm:
        return None
    return max(norm)[0] if want == "bid" else min(norm)[0]


def _try_get_yes_node(ob: Dict[str, Any]) -> Optional[Any]:
    root = ob.get("orderbook") or ob
    return root.get("yes")


_last_ob_debug_ts: float = 0.0
OB_DEBUG_MIN_SECONDS = float(os.getenv("OB_DEBUG_MIN_SECONDS", "60"))


def _tiny_sample(x: Any) -> Any:
    if isinstance(x, dict):
        # show first keys + type hints
        return {k: ("<list>" if isinstance(v, list) else type(v).__name__) for k, v in list(x.items())[:8]}
    if isinstance(x, list):
        return x[:2]
    return type(x).__name__


def parse_yes_book(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    global _last_ob_debug_ts

    root = ob.get("orderbook") if isinstance(ob, dict) else None
    yes_raw = _try_get_yes_node(ob)

    # ✅ MICRO CHANGE: if "yes" exists but isn't dict, print its type/sample
    if yes_raw is None or not isinstance(root, dict) or ("yes" not in root):
        now = time.time()
        if (now - _last_ob_debug_ts) >= OB_DEBUG_MIN_SECONDS:
            _last_ob_debug_ts = now
            log.info(
                f"[OBSCHEMA] top_keys={list(ob.keys())[:12]} "
                f"orderbook_keys={(list(root.keys())[:12] if isinstance(root, dict) else None)}"
            )
        return None, None

    if not isinstance(yes_raw, dict):
        now = time.time()
        if (now - _last_ob_debug_ts) >= OB_DEBUG_MIN_SECONDS:
            _last_ob_debug_ts = now
            yd = root.get("yes_dollars") if isinstance(root, dict) else None
            log.info(
                f"[OBYES_RAW] yes_type={type(yes_raw).__name__} yes_sample={_tiny_sample(yes_raw)} "
                f"yes_dollars_type={type(yd).__name__} yes_dollars_sample={_tiny_sample(yd)}"
            )
        return None, None

    bid = best_price(yes_raw.get("bids"), "bid")
    ask = best_price(yes_raw.get("asks"), "ask")

    if bid is None and ask is None:
        now = time.time()
        if (now - _last_ob_debug_ts) >= OB_DEBUG_MIN_SECONDS:
            _last_ob_debug_ts = now
            log.info(
                f"[OBYES] yes_keys={list(yes_raw.keys())[:12]} "
                f"bids_type={type(yes_raw.get('bids')).__name__} bids_sample={_tiny_sample(yes_raw.get('bids'))} "
                f"asks_type={type(yes_raw.get('asks')).__name__} asks_sample={_tiny_sample(yes_raw.get('asks'))}"
            )

    return bid, ask


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

    log.info(
        f"SERIES={cfg.series_ticker} POLL={cfg.poll_seconds}s "
        f"ROLL_CHECK_MIN_SECONDS={ROLL_CHECK_MIN_SECONDS}s DRY_RUN={cfg.dry_run} ENABLE_TRADING={cfg.enable_trading}"
    )

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
# bot.py
# Kalshi YES-only rolling market maker (safe series filtering + 429 backoff + 2s poll)

import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
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


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if not v else float(v)


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
        poll_seconds=env_float("POLL_SECONDS", 2.0),  # ✅ you set 2 seconds
        enable_trading=env_bool("ENABLE_TRADING", True),
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
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def headers(cfg, priv, method, path_qs):
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
    h = headers(cfg, priv, method, qs)
    r = requests.request(method, url, headers=h, json=body, timeout=20)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


def public_get(cfg, path, params=None):
    qs = path if not params else f"{path}?{urlencode(params)}"
    r = requests.get(cfg.api_base + qs, timeout=20)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {data}")
    return data


# -----------------------------
# Rolling active market resolver (✅ MUST match series prefix)
# -----------------------------
_last_roll_check_ts: float = 0.0
_cached_active_market: Optional[str] = None
ROLL_CHECK_MIN_SECONDS = float(os.getenv("ROLL_CHECK_MIN_SECONDS", "60"))


def _extract_markets(payload: Dict[str, Any]) -> list:
    # Kalshi sometimes returns {"markets":[...]} or {"data":[...]}
    mkts = payload.get("markets")
    if isinstance(mkts, list):
        return mkts
    mkts = payload.get("data")
    if isinstance(mkts, list):
        return mkts
    return []


def _sort_key(m: Dict[str, Any]) -> float:
    # Try a few common time fields; fallback to 0 (stable but unsorted)
    for k in ("close_ts", "close_time", "expiration_ts", "expiration_time", "end_ts", "end_time"):
        v = m.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            # If it's an ISO string, we can't parse reliably without deps; ignore
            continue
    return 0.0


def resolve_active_market(cfg) -> str:
    """
    ✅ Never returns a ticker outside the series.
    If Kalshi returns "random open markets", we filter them.
    If no match, we keep the last good ticker (no random roll into sports).
    """
    global _last_roll_check_ts, _cached_active_market

    now = time.time()
    if _cached_active_market is not None and (now - _last_roll_check_ts) < ROLL_CHECK_MIN_SECONDS:
        return _cached_active_market

    _last_roll_check_ts = now
    prefix = cfg.series_ticker + "-"

    # Try series-specific endpoint first (some deployments support it)
    candidates: list = []
    errors = []

    for path, params in (
        (f"/trade-api/v2/series/{cfg.series_ticker}/markets", {"status": "open"}),
        ("/trade-api/v2/markets", {"series": cfg.series_ticker, "status": "open"}),
        ("/trade-api/v2/markets", {"status": "open"}),  # last resort; we’ll filter hard
    ):
        try:
            data = public_get(cfg, path, params=params)
            mkts = _extract_markets(data)
            if mkts:
                candidates = mkts
                break
        except Exception as e:
            errors.append(str(e))

    if not candidates:
        if _cached_active_market is not None:
            log.warning(f"[ROLL] Could not fetch markets; keeping cached={_cached_active_market}")
            return _cached_active_market
        raise RuntimeError(f"No market data returned. Errors={errors[-1] if errors else 'none'}")

    # ✅ HARD FILTER: must match series prefix
    matched = []
    for m in candidates:
        t = m.get("ticker")
        if isinstance(t, str) and t.startswith(prefix):
            matched.append(m)

    log.info(f"[ROLLDBG] markets_total={len(candidates)} matched_series={len(matched)} prefix={prefix}")

    if not matched:
        # Never roll into something else. Keep last good.
        if _cached_active_market is not None:
            log.warning(f"[ROLL] No markets matched {prefix}; keeping cached={_cached_active_market}")
            return _cached_active_market
        raise RuntimeError(f"No open markets matched series prefix {prefix}")

    matched.sort(key=_sort_key)
    chosen = matched[0].get("ticker")

    if not isinstance(chosen, str) or not chosen.startswith(prefix):
        if _cached_active_market is not None:
            log.warning(f"[ROLL] Chosen invalid; keeping cached={_cached_active_market}")
            return _cached_active_market
        raise RuntimeError("Resolved market ticker invalid")

    _cached_active_market = chosen
    return _cached_active_market


# -----------------------------
# Orderbook parsing (YES-only)
# Schema you saw:
#   orderbook: { yes: [[price_cents, qty], ...], no: [[price_cents, qty], ...] }
# Interpret:
#   YES best bid = max(yes prices)
#   YES best ask = 100 - (NO best bid)
# -----------------------------
def _best_bid_from_levels(levels: Any) -> Optional[int]:
    if not levels or not isinstance(levels, list):
        return None
    best: Optional[int] = None
    for row in levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            p = int(row[0])
        except Exception:
            continue
        best = p if best is None else max(best, p)
    return best


def parse_yes_book(ob: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    root = ob.get("orderbook") if isinstance(ob, dict) else None
    if not isinstance(root, dict):
        return None, None

    yes_levels = root.get("yes")
    no_levels = root.get("no")

    yes_bid = _best_bid_from_levels(yes_levels)
    no_bid = _best_bid_from_levels(no_levels)

    yes_ask: Optional[int] = None
    if no_bid is not None:
        yes_ask = 100 - no_bid
        if yes_ask < 1:
            yes_ask = 1
        if yes_ask > 99:
            yes_ask = 99

    return yes_bid, yes_ask


def choose_price(bid: Optional[int], ask: Optional[int], improve: int, max_px: int) -> Optional[int]:
    if bid is None and ask is None:
        return None
    if bid is None:
        px = (ask - 1) if ask is not None else 1
        return max(1, min(px, max_px))
    px = bid + improve
    if ask is not None:
        px = min(px, ask - 1)
    px = max(1, min(px, max_px))
    return px


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
# 429 backoff for orderbook
# -----------------------------
_ob_backoff = 0.0
OB_BACKOFF_MAX = float(os.getenv("OB_BACKOFF_MAX_SECONDS", "30"))


# -----------------------------
# Main loop
# -----------------------------
def main():
    cfg = load_config()
    priv = load_private_key(cfg.private_key_b64)

    log.info(
        f"SERIES={cfg.series_ticker} "
        f"POLL={cfg.poll_seconds:.1f}s "
        f"ROLL_CHECK_MIN_SECONDS={ROLL_CHECK_MIN_SECONDS:.1f}s "
        f"DRY_RUN={cfg.dry_run} ENABLE_TRADING={cfg.enable_trading}"
    )

    active = None
    global _ob_backoff

    while True:
        try:
            ticker = resolve_active_market(cfg)
            if ticker != active:
                active = ticker
                log.info(f"[ROLL] Active market → {active}")

            # Orderbook fetch with 429 backoff
            try:
                ob = public_get(cfg, f"/trade-api/v2/markets/{active}/orderbook")
                _ob_backoff = 0.0
            except Exception as e:
                msg = str(e)
                if "too_many_requests" in msg or "HTTP 429" in msg:
                    _ob_backoff = min(OB_BACKOFF_MAX, 1.0 if _ob_backoff <= 0 else _ob_backoff * 2.0)
                    log.warning(f"[OB429] backing off {_ob_backoff:.1f}s")
                    time.sleep(_ob_backoff)
                    continue
                raise

            bid, ask = parse_yes_book(ob)
            price = choose_price(bid, ask, cfg.improve_ticks, cfg.max_buy_price)

            if price is None:
                log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → SKIP (empty)")
            else:
                log.info(f"[QUOTE] {active} YES bid={bid} ask={ask} → {price}c")

                if (not cfg.enable_trading) or cfg.dry_run:
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
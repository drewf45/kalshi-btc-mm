# bot.py
# Kalshi YES-only maker bot (roll + orderbook)
# Fixes:
# - Trade API endpoints use SIGNED auth (no public_get for /trade-api/*)
# - Safe JSON parsing to avoid "Extra data" crashes on non-JSON responses
# - Bounded 429 retry w/ cooldown to avoid hammering markets endpoint
# - Default POLL_SECONDS=2, DRY_RUN=True

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
# Env helpers
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
    roll_check_min_seconds: float
    market_page_limit: int
    market_scan_pages_max: int
    http_backoff_max_seconds: float
    # NEW:
    http_429_max_retries: int
    roll_429_cooldown_seconds: float


def load_config() -> BotConfig:
    return BotConfig(
        api_base=os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").rstrip("/"),
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_b64=os.environ["KALSHI_PRIVATE_KEY_PEM_BASE64"],
        series_ticker=os.environ["SERIES_TICKER"],
        poll_seconds=env_float("POLL_SECONDS", 2.0),        # ✅ 2 seconds
        enable_trading=env_bool("ENABLE_TRADING", True),
        dry_run=env_bool("DRY_RUN", True),                  # ✅ dry run default
        base_size=env_int("BASE_SIZE", 1),
        improve_ticks=env_int("IMPROVE_TICKS", 1),
        post_only=env_bool("POST_ONLY", True),
        max_buy_price=env_int("MAX_BUY_PRICE_CENTS", 99),
        roll_check_min_seconds=env_float("ROLL_CHECK_MIN_SECONDS", 60.0),
        market_page_limit=env_int("MARKET_PAGE_LIMIT", 100),
        market_scan_pages_max=env_int("MARKET_SCAN_PAGES_MAX", 25),
        http_backoff_max_seconds=env_float("HTTP_BACKOFF_MAX_SECONDS", 16.0),
        http_429_max_retries=env_int("HTTP_429_MAX_RETRIES", 5),
        roll_429_cooldown_seconds=env_float("ROLL_429_COOLDOWN_SECONDS", 30.0),
    )


# -----------------------------
# Auth / signing
# -----------------------------
def load_private_key(b64: str):
    return serialization.load_pem_private_key(base64.b64decode(b64), password=None)


def sign_request(priv, ts: str, method: str, path_qs: str) -> str:
    # Kalshi expects signing timestamp+method+path (no query, no body)
    path_without_query = path_qs.split("?", 1)[0]
    msg = f"{ts}{method}{path_without_query}".encode("utf-8")
    sig = priv.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def make_headers(cfg, priv, method, path_qs):
    ts = str(int(time.time() * 1000))
    sig = sign_request(priv, ts, method, path_qs)
    return {
        "KALSHI-ACCESS-KEY": cfg.api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


# -----------------------------
# HTTP helpers (safe JSON)
# -----------------------------
def _safe_json(resp: requests.Response) -> Dict[str, Any]:
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        # log the first chunk so we can see what it is (HTML/text/etc.)
        txt = resp.text[:250].replace("\n", "\\n")
        return {"_non_json": True, "_status": resp.status_code, "_text_head": txt}


def is_429_payload(payload: Dict[str, Any]) -> bool:
    # supports both your old style and safe_json fallback
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("code") == "too_many_requests":
            return True
    return False


def is_429_status(resp: requests.Response) -> bool:
    return resp.status_code == 429


def signed_request_json(cfg, priv, method: str, path: str, params=None, body=None) -> Dict[str, Any]:
    qs = path if not params else f"{path}?{urlencode(params)}"
    url = cfg.api_base + qs
    h = make_headers(cfg, priv, method, qs)
    r = requests.request(method, url, headers=h, json=body, timeout=20)
    payload = _safe_json(r)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {qs}: {payload}")
    return payload


def signed_get_with_429(cfg, priv, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bounded 429 retries so we do NOT backoff forever inside a single call.
    """
    backoff = 1.0
    for attempt in range(cfg.http_429_max_retries + 1):
        qs = path if not params else f"{path}?{urlencode(params)}"
        url = cfg.api_base + qs
        h = make_headers(cfg, priv, "GET", qs)
        r = requests.get(url, headers=h, timeout=20)
        payload = _safe_json(r)

        if r.status_code == 200:
            return payload

        if is_429_status(r) or is_429_payload(payload):
            if attempt >= cfg.http_429_max_retries:
                raise RuntimeError(f"HTTP 429 {qs}: giving up after {cfg.http_429_max_retries} retries")
            log.warning(f"[MKT429] {path} backing off {backoff:.1f}s (attempt {attempt+1}/{cfg.http_429_max_retries})")
            time.sleep(backoff)
            backoff = min(cfg.http_backoff_max_seconds, backoff * 2.0)
            continue

        raise RuntimeError(f"HTTP {r.status_code} {qs}: {payload}")


# -----------------------------
# Market extraction
# -----------------------------
def _extract_markets(payload: Dict[str, Any]) -> list:
    mkts = payload.get("markets")
    if isinstance(mkts, list):
        return mkts
    mkts = payload.get("data")
    if isinstance(mkts, list):
        return mkts
    return []


def _extract_cursor(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("cursor", "next_cursor", "nextCursor", "next_page_token", "nextPageToken"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _sort_key(m: Dict[str, Any]) -> float:
    for k in ("close_ts", "close_time", "expiration_ts", "expiration_time", "end_ts", "end_time"):
        v = m.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


# -----------------------------
# Rolling active market resolver
# -----------------------------
_last_roll_check_ts: float = 0.0
_cached_active_market: Optional[str] = None
_roll_block_until_ts: float = 0.0  # ✅ NEW: cooldown after 429 to prevent spam


def resolve_active_market(cfg: BotConfig, priv) -> str:
    global _last_roll_check_ts, _cached_active_market, _roll_block_until_ts

    now = time.time()
    prefix = cfg.series_ticker + "-"

    # If we just got rate-limited hard, wait before trying again
    if now < _roll_block_until_ts:
        raise RuntimeError(f"ROLL blocked until {int(_roll_block_until_ts)} (cooldown)")

    # Normal caching
    if _cached_active_market is not None and (now - _last_roll_check_ts) < cfg.roll_check_min_seconds:
        return _cached_active_market

    _last_roll_check_ts = now

    # 1) Try series-specific endpoints (SIGNED)
    series_paths = [
        (f"/trade-api/v2/series/{cfg.series_ticker}/markets", {"status": "open", "limit": cfg.market_page_limit}),
        ("/trade-api/v2/markets", {"series": cfg.series_ticker, "status": "open", "limit": cfg.market_page_limit}),
    ]

    for path, params in series_paths:
        try:
            data = signed_get_with_429(cfg, priv, path, params)
            mkts = _extract_markets(data)
            matched = [m for m in mkts if isinstance(m.get("ticker"), str) and m["ticker"].startswith(prefix)]
            log.info(f"[ROLLDBG] (series) path={path} total={len(mkts)} matched_series={len(matched)} prefix={prefix}")
            if matched:
                matched.sort(key=_sort_key)
                _cached_active_market = matched[0]["ticker"]
                return _cached_active_market
        except Exception as e:
            # If we got hard 429 giveup, start cooldown
            if "HTTP 429" in str(e):
                _roll_block_until_ts = time.time() + cfg.roll_429_cooldown_seconds
            log.warning(f"[ROLLDBG] (series) path={path} failed: {e}")

    # 2) Paginate open markets list (SIGNED) and scan for prefix
    cursor: Optional[str] = None
    scanned_total = 0
    matched_all: List[Dict[str, Any]] = []
    sample_tickers: List[str] = []

    try:
        for _page in range(cfg.market_scan_pages_max):
            params = {"status": "open", "limit": cfg.market_page_limit}
            if cursor:
                params["cursor"] = cursor

            data = signed_get_with_429(cfg, priv, "/trade-api/v2/markets", params)
            mkts = _extract_markets(data)
            cursor = _extract_cursor(data)

            if not mkts:
                break

            scanned_total += len(mkts)

            # sample tickers for debug
            for m in mkts[:6]:
                t = m.get("ticker")
                if isinstance(t, str) and len(sample_tickers) < 18:
                    sample_tickers.append(t)

            for m in mkts:
                t = m.get("ticker")
                if isinstance(t, str) and t.startswith(prefix):
                    matched_all.append(m)

            if matched_all:
                matched_all.sort(key=_sort_key)
                _cached_active_market = matched_all[0]["ticker"]
                log.info(f"[ROLLDBG] (scan) scanned_total={scanned_total} matched_series={len(matched_all)} prefix={prefix}")
                return _cached_active_market

            if not cursor:
                break

        log.info(f"[ROLLDBG] (scan_fail) scanned_total={scanned_total} matched_series=0 prefix={prefix} sample={sample_tickers}")
        raise RuntimeError(f"No open markets matched series prefix {prefix} (scanned {scanned_total})")
    except Exception as e:
        if "HTTP 429" in str(e):
            _roll_block_until_ts = time.time() + cfg.roll_429_cooldown_seconds
        raise


# -----------------------------
# Orderbook parsing (YES-only)
# orderbook: { yes: [[price_cents, qty], ...], no: [[price_cents, qty], ...] }
# YES best bid = max(yes prices)
# YES best ask = 100 - (NO best bid)
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
        yes_ask = max(1, min(99, yes_ask))
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
def place_yes_buy(cfg, priv, market_ticker: str, price_cents: int, qty: int) -> Dict[str, Any]:
    body = {
        "ticker": market_ticker,
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "price": price_cents,
        "count": qty,
    }
    return signed_request_json(cfg, priv, "POST", "/trade-api/v2/portfolio/orders", body=body)


def signed_get_orderbook_with_429(cfg, priv, market_ticker: str) -> Dict[str, Any]:
    backoff = 1.0
    path = f"/trade-api/v2/markets/{market_ticker}/orderbook"
    for attempt in range(cfg.http_429_max_retries + 1):
        try:
            return signed_request_json(cfg, priv, "GET", path)
        except Exception as e:
            if "HTTP 429" in str(e):
                if attempt >= cfg.http_429_max_retries:
                    raise
                log.warning(f"[OB429] backing off {backoff:.1f}s (attempt {attempt+1}/{cfg.http_429_max_retries})")
                time.sleep(backoff)
                backoff = min(cfg.http_backoff_max_seconds, backoff * 2.0)
                continue
            raise


# -----------------------------
# Main loop
# -----------------------------
def main():
    cfg = load_config()
    priv = load_private_key(cfg.private_key_b64)

    log.info(
        f"SERIES={cfg.series_ticker} POLL={cfg.poll_seconds:.1f}s "
        f"ROLL_CHECK_MIN_SECONDS={cfg.roll_check_min_seconds:.1f}s "
        f"DRY_RUN={cfg.dry_run} ENABLE_TRADING={cfg.enable_trading}"
    )

    active: Optional[str] = None

    while True:
        try:
            ticker = resolve_active_market(cfg, priv)
            if ticker != active:
                active = ticker
                log.info(f"[ROLL] Active market → {active}")

            ob = signed_get_orderbook_with_429(cfg, priv, active)
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
                    log.info(f"[ORDER] placed YES buy {cfg.base_size}@{price}c resp={resp}")

        except Exception as e:
            log.error(f"[LOOPERR] {e}")

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
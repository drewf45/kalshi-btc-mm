# bot.py
# Kalshi YES-only quoting bot (fast loop)
# - Resolves the CURRENT active market from a SERIES TICKER (so you don't have to hardcode MARKET_TICKER)
# - Signs requests using the EXACT body bytes that are sent (prevents INCORRECT_API_KEY_SIGNATURE mismatches)
#
# REQUIRED ENV:
#   KALSHI_API_KEY_ID              = your Kalshi API key ID (UUID)
#   KALSHI_PRIVATE_KEY_PEM_BASE64  = base64 of the downloaded .pem private key (UNENCRYPTED)
#
# REQUIRED FOR SERIES RESOLVE:
#   SERIES_TICKER                  = e.g. KXBTC15M   (series ticker, NOT full market ticker)
#
# OPTIONAL:
#   KALSHI_API_BASE                = https://trading-api.kalshi.com   (default)
#   POLL_SECONDS                   = 1
#   ENABLE_TRADING                 = false (default)
#   DRY_RUN                        = true  (default)
#   BASE_SIZE                      = 1
#   POST_ONLY                      = true
#   IMPROVE_TICKS                  = 1
#   MAX_BUY_PRICE_CENTS            = 99
#   SUBACCOUNT                     = (optional subaccount string if you use one)
#
# NOTES:
# - Public endpoints: orderbook + markets list (no auth)
# - Private endpoints: portfolio/orders (auth)
# - If you still get 401 INCORRECT_API_KEY_SIGNATURE, your key-id and PEM likely don't match.

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
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v.strip())


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return float(v.strip())


@dataclass
class BotConfig:
    api_base: str
    api_key_id: str
    private_key_pem_b64: str

    # dynamic resolve:
    series_ticker: str

    poll_seconds: float
    enable_trading: bool
    dry_run: bool

    # strategy knobs (YES-only)
    base_size: int
    post_only: bool
    improve_ticks: int
    max_buy_price_cents: int  # never buy above this

    subaccount: str


def load_config() -> BotConfig:
    api_base = os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").strip().rstrip("/")
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    private_key_pem_b64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()
    series_ticker = os.getenv("SERIES_TICKER", "").strip()

    if not api_key_id:
        raise RuntimeError("Missing env var KALSHI_API_KEY_ID")
    if not private_key_pem_b64:
        raise RuntimeError("Missing env var KALSHI_PRIVATE_KEY_PEM_BASE64")
    if not series_ticker:
        raise RuntimeError("Missing env var SERIES_TICKER (e.g. KXBTC15M)")

    return BotConfig(
        api_base=api_base,
        api_key_id=api_key_id,
        private_key_pem_b64=private_key_pem_b64,
        series_ticker=series_ticker,

        poll_seconds=env_float("POLL_SECONDS", 1.0),
        enable_trading=env_bool("ENABLE_TRADING", False),
        dry_run=env_bool("DRY_RUN", True),

        base_size=env_int("BASE_SIZE", 1),
        post_only=env_bool("POST_ONLY", True),
        improve_ticks=env_int("IMPROVE_TICKS", 1),
        max_buy_price_cents=env_int("MAX_BUY_PRICE_CENTS", 99),

        subaccount=os.getenv("SUBACCOUNT", "").strip(),
    )


# -----------------------------
# Key + signing
# -----------------------------
def load_private_key_from_env(pem_b64: str):
    pem_bytes = base64.b64decode(pem_b64)
    return serialization.load_pem_private_key(pem_bytes, password=None)


def _canonical_json(obj: Optional[dict]) -> str:
    if obj is None:
        return ""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _build_path_with_query(path: str, params: Optional[dict]) -> str:
    if not params:
        return path
    # urlencode will be deterministic for a dict in py3.7+ insertion order;
    # we build params in a stable order anyway
    return f"{path}?{urlencode(params)}"


def _sign(private_key, ts_ms: str, method: str, path_with_query: str, body_str: str) -> str:
    payload = f"{ts_ms}{method.upper()}{path_with_query}{body_str}".encode("utf-8")
    sig = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


def _headers(cfg: BotConfig, private_key, method: str, path_with_query: str, body_str: str) -> Dict[str, str]:
    ts_ms = str(int(time.time() * 1000))
    sig = _sign(private_key, ts_ms, method, path_with_query, body_str)
    hdrs = {
        "KALSHI-ACCESS-KEY": cfg.api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }
    # optional subaccount header (only if you use it)
    if cfg.subaccount:
        hdrs["KALSHI-SUBACCOUNT"] = cfg.subaccount
    return hdrs


# -----------------------------
# HTTP helpers
# -----------------------------
def _req_json(
    cfg: BotConfig,
    private_key,
    method: str,
    path: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: int = 20,
) -> Any:
    path_with_query = _build_path_with_query(path, params)
    url = cfg.api_base + path_with_query

    # IMPORTANT: sign EXACTLY the bytes sent over the wire
    body_str = ""
    data_payload = None
    if method.upper() in ("POST", "PUT", "PATCH"):
        body_str = _canonical_json(json_body) if json_body is not None else ""
        data_payload = body_str

    hdrs = _headers(cfg, private_key, method, path_with_query, body_str)

    log.info(f"[REQ] {method.upper()} {path_with_query}")
    r = requests.request(
        method.upper(),
        url,
        headers=hdrs,
        data=data_payload,  # DO NOT use json= (would re-serialize differently)
        timeout=timeout,
    )

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path_with_query}: {data}")
    return data


def public_get(cfg: BotConfig, path: str, params: Optional[dict] = None, timeout: int = 20) -> Any:
    path_with_query = _build_path_with_query(path, params)
    url = cfg.api_base + path_with_query
    log.info(f"[REQ] GET {path_with_query} (public)")
    r = requests.get(url, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path_with_query}: {data}")
    return data


# -----------------------------
# Kalshi endpoints (v2)
# -----------------------------
def list_open_markets_for_series(cfg: BotConfig, series_ticker: str, limit: int = 200) -> List[dict]:
    # Public in your logs
    data = public_get(cfg, "/trade-api/v2/markets", params={"limit": limit, "series_ticker": series_ticker, "status": "open"})
    # Common shapes
    if isinstance(data, dict):
        for k in ("markets", "data", "results"):
            if k in data and isinstance(data[k], list):
                return data[k]
    if isinstance(data, list):
        return data
    return []


def resolve_active_market_ticker(cfg: BotConfig) -> str:
    markets = list_open_markets_for_series(cfg, cfg.series_ticker, limit=200)
    if not markets:
        raise RuntimeError(f"No open markets found for series_ticker={cfg.series_ticker}")

    # Prefer the first open market returned.
    # If API returns multiple, you can refine selection here (e.g., nearest close time).
    # For your BTC15M case, you were seeing markets=1.
    picked = None
    for m in markets:
        t = m.get("ticker") or m.get("market_ticker")
        if t:
            picked = t
            break

    if not picked:
        raise RuntimeError(f"Could not find ticker in markets response for series_ticker={cfg.series_ticker}")

    return str(picked)


def get_orderbook(cfg: BotConfig, ticker: str) -> dict:
    return public_get(cfg, f"/trade-api/v2/markets/{ticker}/orderbook")


def list_open_orders(cfg: BotConfig, private_key, limit: int = 200) -> List[dict]:
    data = _req_json(
        cfg,
        private_key,
        "GET",
        "/trade-api/v2/portfolio/orders",
        params={"limit": limit, "status": "open"},
        json_body=None,
    )
    if isinstance(data, dict):
        for k in ("orders", "data", "results"):
            if k in data and isinstance(data[k], list):
                return data[k]
    if isinstance(data, list):
        return data
    return []


def create_order_yes_buy(cfg: BotConfig, private_key, ticker: str, price_cents: int, size: int, post_only: bool) -> dict:
    body = {
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "yes_price": int(price_cents),
        "count": int(size),
        "post_only": bool(post_only),
    }
    return _req_json(cfg, private_key, "POST", "/trade-api/v2/portfolio/orders", params=None, json_body=body)


def cancel_order(cfg: BotConfig, private_key, order_id: str) -> Optional[dict]:
    # Endpoint may differ by account/version; keep best-effort.
    try:
        return _req_json(cfg, private_key, "POST", f"/trade-api/v2/portfolio/orders/{order_id}/cancel", params=None, json_body={})
    except Exception as e:
        log.warning(f"[CANCEL] Failed cancel {order_id}: {e}")
        return None


# -----------------------------
# Orderbook parsing (YES only)
# -----------------------------
def _best_from_levels(levels: Any, want: str) -> Optional[Tuple[int, int]]:
    """
    levels can be:
      - list of [price, qty] or {"price":..,"qty":..}
      - dict mapping price->qty
    want: "bid" (highest price) or "ask" (lowest price)
    returns (price_cents, qty)
    """
    if levels is None:
        return None

    items: List[Tuple[int, int]] = []

    if isinstance(levels, dict):
        for p, q in levels.items():
            try:
                items.append((int(p), int(q)))
            except Exception:
                pass
    elif isinstance(levels, list):
        for x in levels:
            if isinstance(x, (list, tuple)) and len(x) >= 2:
                try:
                    items.append((int(x[0]), int(x[1])))
                except Exception:
                    pass
            elif isinstance(x, dict):
                for pk, qk in (("price", "qty"), ("price_cents", "count"), ("p", "q")):
                    if pk in x and qk in x:
                        try:
                            items.append((int(x[pk]), int(x[qk])))
                        except Exception:
                            pass
                        break

    if not items:
        return None

    if want == "bid":
        return max(items, key=lambda t: t[0])
    else:
        return min(items, key=lambda t: t[0])


def parse_yes_best_bid_ask(ob: dict) -> Tuple[Optional[int], Optional[int]]:
    """
    Tries likely Kalshi shapes.
    Returns (best_bid_cents, best_ask_cents) for YES.
    """
    root = ob.get("orderbook") if isinstance(ob, dict) and "orderbook" in ob else ob
    if not isinstance(root, dict):
        return (None, None)

    yes = root.get("yes")
    if not isinstance(yes, dict):
        # some shapes might nest deeper
        nested = root.get("orderbook")
        if isinstance(nested, dict):
            yes = nested.get("yes")

    if not isinstance(yes, dict):
        return (None, None)

    bids = yes.get("bids") or yes.get("bid") or yes.get("buy")
    asks = yes.get("asks") or yes.get("ask") or yes.get("sell")

    best_bid = _best_from_levels(bids, "bid")
    best_ask = _best_from_levels(asks, "ask")

    return (best_bid[0] if best_bid else None, best_ask[0] if best_ask else None)


# -----------------------------
# Strategy (YES-only)
# -----------------------------
def choose_yes_buy_price(best_bid: Optional[int], best_ask: Optional[int], improve_ticks: int, max_buy: int) -> int:
    """
    Maker-ish logic:
      - If there's a best_bid, improve it by improve_ticks (but never >= best_ask)
      - If no book, bid at max_buy
    """
    if best_bid is None and best_ask is None:
        return max_buy

    if best_bid is None and best_ask is not None:
        px = max(1, best_ask - max(1, improve_ticks))
        return min(px, max_buy)

    px = best_bid + max(1, improve_ticks)
    if best_ask is not None:
        px = min(px, best_ask - 1)
    px = max(1, px)
    return min(px, max_buy)


def find_existing_yes_buy(open_orders: List[dict], ticker: str) -> Optional[dict]:
    for o in open_orders:
        t = o.get("ticker") or o.get("market_ticker")
        if t != ticker:
            continue
        side = (o.get("side") or o.get("contract") or o.get("outcome") or "").lower()
        action = (o.get("action") or o.get("direction") or "").lower()
        if action == "buy" and side == "yes":
            return o
        if "buy" in action and "yes" in side:
            return o
    return None


def order_price_cents(order: dict) -> Optional[int]:
    if not order:
        return None
    for k in ("yes_price", "price", "price_cents", "limit_price"):
        if k in order:
            try:
                return int(order[k])
            except Exception:
                pass
    return None


# -----------------------------
# Main loop
# -----------------------------
def main():
    cfg = load_config()
    private_key = load_private_key_from_env(cfg.private_key_pem_b64)

    log.info(f"[BOOT] API_BASE={cfg.api_base} SERIES_TICKER={cfg.series_ticker} POLL_SECONDS={cfg.poll_seconds:.2f}")
    log.info(f"[BOOT] ENABLE_TRADING={cfg.enable_trading} DRY_RUN={cfg.dry_run} SUBACCOUNT={'set' if cfg.subaccount else ''}")
    log.info(f"[BOOT] YES-only: BASE_SIZE={cfg.base_size} POST_ONLY={cfg.post_only} IMPROVE_TICKS={cfg.improve_ticks} MAX_BUY_PRICE_CENTS={cfg.max_buy_price_cents}")

    active_ticker = None
    last_resolve_ts = 0.0
    RESOLVE_EVERY_SECONDS = 15.0  # re-resolve often enough to follow your 15m markets

    while True:
        t0 = time.time()
        try:
            # Resolve current market ticker from series
            if (active_ticker is None) or (time.time() - last_resolve_ts >= RESOLVE_EVERY_SECONDS):
                new_ticker = resolve_active_market_ticker(cfg)
                last_resolve_ts = time.time()
                if new_ticker != active_ticker:
                    active_ticker = new_ticker
                    log.info(f"[MARKET] Switched active ticker -> {active_ticker}")

            # Orderbook (public)
            ob = get_orderbook(cfg, active_ticker)
            preview = {"keys": list(ob.keys())} if isinstance(ob, dict) else {"type": str(type(ob))}
            log.info(f"[OB] {active_ticker} orderbook_preview={preview}")

            best_bid, best_ask = parse_yes_best_bid_ask(ob)
            log.info(f"[SPREAD] {active_ticker} YES best_bid={best_bid} best_ask={best_ask}")

            target_px = choose_yes_buy_price(best_bid, best_ask, cfg.improve_ticks, cfg.max_buy_price_cents)
            log.info(f"[QUOTE] {active_ticker} target YES buy = {target_px}c post_only={cfg.post_only}")

            # Portfolio (auth)
            open_orders = list_open_orders(cfg, private_key)
            existing = find_existing_yes_buy(open_orders, active_ticker)
            existing_px = order_price_cents(existing)

            if existing:
                log.info(f"[OPEN] Found existing YES buy order price={existing_px}c id={existing.get('id') or existing.get('order_id')}")
            else:
                log.info("[OPEN] No existing YES buy order found for this market")

            if existing and existing_px == target_px:
                log.info("[SKIP] Existing order already at target price")
            else:
                if cfg.dry_run or not cfg.enable_trading:
                    log.info("[DRYRUN] Would place/replace YES buy now")
                else:
                    if existing:
                        oid = existing.get("id") or existing.get("order_id")
                        if oid:
                            cancel_order(cfg, private_key, str(oid))

                    resp = create_order_yes_buy(cfg, private_key, active_ticker, target_px, cfg.base_size, cfg.post_only)
                    log.info(f"[PLACED] YES buy placed: {resp}")

        except Exception as e:
            log.error(f"[LOOPERR] {repr(e)}")

        elapsed = time.time() - t0
        time.sleep(max(0.0, cfg.poll_seconds - elapsed))


if __name__ == "__main__":
    main()
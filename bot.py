# bot.py
# Kalshi YES-only quoting bot (fast loop) with correct signing (includes query + body)
#
# REQUIRED ENV:
#   KALSHI_API_KEY_ID              = your Kalshi API key ID (looks like a UUID)
#   KALSHI_PRIVATE_KEY_PEM_BASE64  = base64 of the downloaded .pem private key
#
# STRONGLY RECOMMENDED ENV:
#   KALSHI_API_BASE                = https://trading-api.kalshi.com   (default below)
#   MARKET_TICKER                  = exact market ticker to trade (e.g. KXBTC15M-26JAN201900-00)
#
# SAFETY DEFAULTS:
#   ENABLE_TRADING=false by default (dry-run logging only)

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
# Config
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

    market_ticker: str

    poll_seconds: float
    enable_trading: bool
    dry_run: bool

    # strategy knobs (YES-only)
    base_size: int
    post_only: bool
    improve_ticks: int
    max_buy_price_cents: int  # never buy above this


def load_config() -> BotConfig:
    api_base = os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").strip().rstrip("/")
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    private_key_pem_b64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()

    market_ticker = os.getenv("MARKET_TICKER", "").strip()

    if not api_key_id:
        raise RuntimeError("Missing env var KALSHI_API_KEY_ID")
    if not private_key_pem_b64:
        raise RuntimeError("Missing env var KALSHI_PRIVATE_KEY_PEM_BASE64")
    if not market_ticker:
        raise RuntimeError("Missing env var MARKET_TICKER (set the exact market ticker)")

    return BotConfig(
        api_base=api_base,
        api_key_id=api_key_id,
        private_key_pem_b64=private_key_pem_b64,
        market_ticker=market_ticker,

        poll_seconds=float(os.getenv("POLL_SECONDS", "1").strip()),
        enable_trading=env_bool("ENABLE_TRADING", False),
        dry_run=env_bool("DRY_RUN", True),

        base_size=env_int("BASE_SIZE", 1),
        post_only=env_bool("POST_ONLY", True),
        improve_ticks=env_int("IMPROVE_TICKS", 1),
        max_buy_price_cents=env_int("MAX_BUY_PRICE_CENTS", 99),
    )


# -----------------------------
# Key + signing
# -----------------------------
def load_private_key_from_env(pem_b64: str):
    pem_bytes = base64.b64decode(pem_b64)
    # Most Kalshi keys are unencrypted
    return serialization.load_pem_private_key(pem_bytes, password=None)


def _canonical_json(obj: Optional[dict]) -> str:
    if obj is None:
        return ""
    # stable JSON for signing
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _build_path_with_query(path: str, params: Optional[dict]) -> str:
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


def _sign(private_key, ts_ms: str, method: str, path_with_query: str, body_str: str) -> str:
    # IMPORTANT: sign EXACTLY what you request:
    # timestamp + METHOD + path_with_query + body_str
    payload = f"{ts_ms}{method.upper()}{path_with_query}{body_str}".encode("utf-8")
    sig = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


def _headers(cfg: BotConfig, private_key, method: str, path_with_query: str, json_body: Optional[dict]) -> Dict[str, str]:
    ts_ms = str(int(time.time() * 1000))
    body_str = _canonical_json(json_body) if method.upper() in ("POST", "PUT", "PATCH") else ""
    sig = _sign(private_key, ts_ms, method, path_with_query, body_str)

    return {
        "KALSHI-ACCESS-KEY": cfg.api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


# -----------------------------
# HTTP helpers
# -----------------------------
def _req_json(cfg: BotConfig, private_key, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None, timeout: int = 20) -> Any:
    path_with_query = _build_path_with_query(path, params)
    url = cfg.api_base + path_with_query
    hdrs = _headers(cfg, private_key, method, path_with_query, json_body)

    log.info(f"[REQ] {method.upper()} {path_with_query}")
    r = requests.request(method.upper(), url, headers=hdrs, json=json_body, timeout=timeout)

    # Try parse JSON either way
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path_with_query}: {data}")
    return data


def public_get(cfg: BotConfig, path: str, params: Optional[dict] = None, timeout: int = 20) -> Any:
    # Public endpoints don't need auth headers
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
def get_orderbook(cfg: BotConfig, ticker: str) -> dict:
    # Public in your logs (200 without auth)
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
    # Try common shapes
    if isinstance(data, dict):
        for k in ("orders", "data", "results"):
            if k in data and isinstance(data[k], list):
                return data[k]
    if isinstance(data, list):
        return data
    return []


def create_order_yes_buy(cfg: BotConfig, private_key, ticker: str, price_cents: int, size: int, post_only: bool) -> dict:
    # IMPORTANT: must include "ticker" or you get missing_parameters (your earlier 400)
    body = {
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "yes_price": price_cents,
        "count": size,
        "post_only": bool(post_only),
    }
    return _req_json(cfg, private_key, "POST", "/trade-api/v2/portfolio/orders", params=None, json_body=body)


def cancel_order(cfg: BotConfig, private_key, order_id: str) -> Optional[dict]:
    # Kalshi cancel route can vary; try a common one.
    # If this 404s, tell me the error and I’ll adjust to the exact endpoint.
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
                # try common keys
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
    Tries multiple likely Kalshi shapes.
    Returns (best_bid_cents, best_ask_cents) for YES.
    """
    # Your preview showed: {'keys':['orderbook'], 'yes': None}
    # So 'orderbook' exists, and inside it there may be yes bids/asks.
    root = ob.get("orderbook") if isinstance(ob, dict) else None
    if root is None:
        root = ob

    yes = None
    if isinstance(root, dict):
        yes = root.get("yes")
        if yes is None and "orderbook" in root and isinstance(root["orderbook"], dict):
            yes = root["orderbook"].get("yes")

    # If still none, return (None, None)
    if not isinstance(yes, dict):
        return (None, None)

    # Common possibilities:
    # yes["bids"], yes["asks"] OR yes["bid"], yes["ask"] OR yes["buy"], yes["sell"]
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
    Simple maker-ish logic:
      - If there's a best_bid, improve it by improve_ticks (but never >= best_ask)
      - If no book, bid at max_buy (but you control max_buy via env)
    """
    if best_bid is None and best_ask is None:
        return max_buy

    if best_bid is None and best_ask is not None:
        # try to sit one tick below ask if possible
        px = max(1, best_ask - max(1, improve_ticks))
        return min(px, max_buy)

    # have a bid
    px = best_bid + max(1, improve_ticks)
    if best_ask is not None:
        px = min(px, best_ask - 1)  # keep maker
    px = max(1, px)
    return min(px, max_buy)


def find_existing_yes_buy(open_orders: List[dict], ticker: str) -> Optional[dict]:
    # We try multiple field names because APIs vary
    for o in open_orders:
        t = o.get("ticker") or o.get("market_ticker")
        if t != ticker:
            continue
        side = (o.get("side") or o.get("contract") or o.get("outcome") or "").lower()
        action = (o.get("action") or o.get("direction") or "").lower()
        if "yes" in side and "buy" in action:
            return o
        # sometimes: action="buy", side="yes"
        if action == "buy" and side == "yes":
            return o
    return None


def order_price_cents(order: dict) -> Optional[int]:
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

    log.info(f"API_BASE={cfg.api_base}")
    log.info(f"MARKET_TICKER={cfg.market_ticker}")
    log.info(f"ENABLE_TRADING={cfg.enable_trading} DRY_RUN={cfg.dry_run} POLL_SECONDS={cfg.poll_seconds}")
    log.info(f"YES-only: BASE_SIZE={cfg.base_size} POST_ONLY={cfg.post_only} IMPROVE_TICKS={cfg.improve_ticks} MAX_BUY_PRICE_CENTS={cfg.max_buy_price_cents}")

    while True:
        t0 = time.time()
        try:
            # Orderbook
            ob = get_orderbook(cfg, cfg.market_ticker)
            preview = {"keys": list(ob.keys())} if isinstance(ob, dict) else {"type": str(type(ob))}
            # small peek at whether "orderbook"/"yes" exists
            try:
                yes_preview = None
                if isinstance(ob, dict):
                    root = ob.get("orderbook", ob)
                    yes_preview = (root.get("yes") if isinstance(root, dict) else None)
                preview["yes"] = None if yes_preview is None else "present"
            except Exception:
                pass

            log.info(f"[OB] {cfg.market_ticker} orderbook_preview={preview}")

            best_bid, best_ask = parse_yes_best_bid_ask(ob)
            log.info(f"[SPREAD] {cfg.market_ticker} YES best_bid={best_bid} best_ask={best_ask}")

            target_px = choose_yes_buy_price(best_bid, best_ask, cfg.improve_ticks, cfg.max_buy_price_cents)
            log.info(f"[QUOTE] {cfg.market_ticker} target YES buy = {target_px}c post_only={cfg.post_only}")

            # Open orders (auth required)
            open_orders = list_open_orders(cfg, private_key)
            existing = find_existing_yes_buy(open_orders, cfg.market_ticker)
            existing_px = order_price_cents(existing) if existing else None

            if existing:
                log.info(f"[OPEN] Found existing YES buy order (price={existing_px}c) id={existing.get('id') or existing.get('order_id')}")
            else:
                log.info("[OPEN] No existing YES buy order found for this market")

            # Place / replace
            if existing and existing_px == target_px:
                log.info("[SKIP] Existing order already at target price")
            else:
                if cfg.dry_run or not cfg.enable_trading:
                    log.info("[DRYRUN] Would place/replace YES buy now")
                else:
                    # Optional: cancel old before place
                    if existing:
                        oid = existing.get("id") or existing.get("order_id")
                        if oid:
                            cancel_order(cfg, private_key, str(oid))

                    resp = create_order_yes_buy(cfg, private_key, cfg.market_ticker, target_px, cfg.base_size, cfg.post_only)
                    log.info(f"[PLACED] YES buy placed: {resp}")

        except Exception as e:
            log.error(f"[LOOPERR] {repr(e)}")

        elapsed = time.time() - t0
        sleep_for = max(0.0, cfg.poll_seconds - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
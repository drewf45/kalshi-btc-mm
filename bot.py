# bot.py
# Kalshi YES-only rolling 15m market maker
# ✅ FIX (ONLY CHANGE): prevent "post only cross" by:
#   1) enforcing post-only safety (BUY < best_ask, SELL > best_bid)
#   2) re-fetching the orderbook right before placing the SECOND leg (SELL)
#   3) if SELL fails, cancel the BUY so you don't get stuck one-sided

import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return default if v is None or str(v).strip() == "" else int(str(v).strip())
    except Exception:
        return default


def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


# -----------------------------
# Config
# -----------------------------
API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")

# creds
KALSHI_API_KEY = getenv_first(["KALSHI_API_KEY", "API_KEY"], "")
KALSHI_KEY_ID = getenv_first(["KALSHI_KEY_ID", "KEY_ID"], "")
KALSHI_PRIVATE_KEY_PATH = getenv_first(["KALSHI_PRIVATE_KEY_PATH", "PRIVATE_KEY_PATH"], "")

if not KALSHI_API_KEY or not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_PATH:
    raise RuntimeError("Missing env: KALSHI_API_KEY, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH")

# bot behavior
DRY_RUN = env_bool("DRY_RUN", False)
LOOP_SECONDS = float(os.getenv("LOOP_SECONDS", "1.5"))

# spread / stability gates (these match your log phrases)
MIN_SPREAD_CENTS = env_int("MIN_SPREAD_CENTS", 3)          # "ok(spread=3)"
STABLE_SECONDS = float(os.getenv("STABLE_SECONDS", "0.50"))  # "0.00s<0.50s"

# order mgmt
REPRICE_THRESHOLD_CENTS = env_int("REPRICE_THRESHOLD_CENTS", 2)  # "off_by=1<thresh(2)"
MAX_CHASE_CENTS = env_int("MAX_CHASE_CENTS", 3)                  # "off_by=7>=MAX_CHASE_CENTS=3"
TIGHT_SPREAD_HOLD_SECONDS = float(os.getenv("TIGHT_SPREAD_HOLD_SECONDS", "1.25"))

# what to trade
SERIES = os.getenv("SERIES", "KXBTC15M")
QTY = env_int("QTY", 1)


# -----------------------------
# RSA signer
# -----------------------------
def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


_PRIVATE_KEY = load_private_key(KALSHI_PRIVATE_KEY_PATH)


def sign_request(timestamp_ms: int, method: str, path_with_query: str, body: str) -> str:
    """
    Kalshi-style signature: base64(RSA-SHA256( ts + method + path + body ))
    """
    msg = f"{timestamp_ms}{method.upper()}{path_with_query}{body}".encode("utf-8")
    sig = _PRIVATE_KEY.sign(msg, asy_padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


def headers_for(method: str, path_with_query: str, body: str) -> Dict[str, str]:
    ts = now_utc_ts_ms()
    sig = sign_request(ts, method, path_with_query, body)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-KEY-ID": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


# -----------------------------
# HTTP wrapper
# -----------------------------
def kalshi_request(method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    method = method.upper()
    qs = ""
    if params:
        qs = "?" + urlencode(params)

    path_with_query = f"{path}{qs}"
    url = f"{API_BASE}{path_with_query}"

    body_str = ""
    if json_body is not None:
        body_str = json.dumps(json_body, separators=(",", ":"))

    hdrs = headers_for(method, path_with_query, body_str)

    log.debug(f"[REQ] {method} {path_with_query}")
    r = requests.request(method, url, headers=hdrs, data=(body_str if body_str else None), timeout=15)

    # Try parse JSON; keep head text for non-json errors
    try:
        data = r.json()
    except Exception:
        data = {"_non_json": True, "_text_head": (r.text[:500] if r.text else "")}

    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {data}")

    return data


# -----------------------------
# Market discovery
# -----------------------------
def get_markets_for_series(series: str, status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
    # NOTE: series endpoint is not available; use /markets?series=...
    data = kalshi_request("GET", "/trade-api/v2/markets", params={"series": series, "status": status, "limit": limit})
    return data.get("markets", []) or []


def pick_target_market_id(markets: List[Dict[str, Any]]) -> Optional[str]:
    """
    Pick the nearest-expiring open market.
    Each market has fields like: market_id, close_time, status, etc.
    """
    if not markets:
        return None

    def parse_close(m: Dict[str, Any]) -> float:
        ct = m.get("close_time") or m.get("closeTime") or ""
        try:
            # kalshi uses ISO timestamps
            return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except Exception:
            return float("inf")

    open_mkts = [m for m in markets if (m.get("status") == "open")]
    if not open_mkts:
        open_mkts = markets

    open_mkts.sort(key=parse_close)
    return open_mkts[0].get("market_id") or open_mkts[0].get("marketId")


# -----------------------------
# Orderbook parsing (YES only)
# -----------------------------
def get_orderbook(market_id: str) -> Dict[str, Any]:
    return kalshi_request("GET", f"/trade-api/v2/markets/{market_id}/orderbook")


def parse_yes_best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Supports both:
      - {"yes": {"bids":[{"price":..,"quantity":..}], "asks":[...]}}
      - or flat structures where yes bids/asks are directly present

    We only need best bid and best ask in cents.
    """
    # common layout: orderbook["orderbook"]["yes"]["bids"/"asks"] or orderbook["yes"]["bids"/"asks"]
    ob = orderbook.get("orderbook") or orderbook

    yes = ob.get("yes") or {}
    bids = yes.get("bids")
    asks = yes.get("asks")

    # fallback: sometimes keys are "buy"/"sell" etc; if so, user would have already adjusted earlier
    if bids is None or asks is None:
        # attempt a couple common alternates
        bids = ob.get("yes_bids") or ob.get("bids") or bids
        asks = ob.get("yes_asks") or ob.get("asks") or asks

    best_bid = None
    best_ask = None

    if isinstance(bids, list) and bids:
        # assume bids highest price first OR sort defensively
        try:
            best_bid = max(int(x.get("price")) for x in bids if x.get("price") is not None)
        except Exception:
            best_bid = None

    if isinstance(asks, list) and asks:
        # assume asks lowest price first OR sort defensively
        try:
            best_ask = min(int(x.get("price")) for x in asks if x.get("price") is not None)
        except Exception:
            best_ask = None

    return best_bid, best_ask


# -----------------------------
# Order management
# -----------------------------
def place_order(market_id: str, side: str, price: int, qty: int, post_only: bool = True) -> Optional[str]:
    if DRY_RUN:
        log.info(f"[OM] {market_id} {side.upper()} PLACE @{price} qty={qty} DRY_RUN=True")
        return "DRY_RUN_ORDER_ID"

    payload = {
        "market_id": market_id,
        "side": side.lower(),        # "buy" or "sell"
        "type": "limit",
        "price": int(price),
        "count": int(qty),
        "post_only": bool(post_only),
    }

    log.info(f"[OM] {market_id} {side.upper()} PLACE @{price} qty={qty} DRY_RUN=False")
    data = kalshi_request("POST", "/trade-api/v2/portfolio/orders", json_body=payload)

    order_id = data.get("order_id") or (data.get("order") or {}).get("order_id")
    log.info(f"[OM] {market_id} {side.upper()} POSTED order_id={order_id} @ {price} qty={qty}")
    return order_id


def cancel_order(order_id: str) -> None:
    if not order_id or order_id == "DRY_RUN_ORDER_ID":
        return
    if DRY_RUN:
        log.info(f"[OM] CANCEL order_id={order_id} DRY_RUN=True")
        return
    try:
        kalshi_request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")
        log.info(f"[OM] CANCEL OK order_id={order_id}")
    except Exception as e:
        msg = str(e)
        # your logs show 404 not_found often; keep ignoring that
        if "404" in msg or "not_found" in msg:
            log.info(f"[OM] CANCEL already-gone order_id={order_id} (ignoring 404 not_found)")
            return
        raise


# -----------------------------
# ✅ FIX: post-only safety
# -----------------------------
def post_only_safe_prices(
    buy_px: Optional[int],
    sell_px: Optional[int],
    best_bid: Optional[int],
    best_ask: Optional[int],
) -> Optional[Tuple[int, int]]:
    """
    ✅ FIX: Prevent "post only cross"
      - BUY must be < best_ask
      - SELL must be > best_bid
    If either would cross, return None (skip quoting this cycle).
    """
    if buy_px is None or sell_px is None:
        return None
    if best_ask is not None and buy_px >= best_ask:
        return None
    if best_bid is not None and sell_px <= best_bid:
        return None
    return buy_px, sell_px


# -----------------------------
# Strategy: YES-only spread quoting
# -----------------------------
@dataclass
class LiveOrders:
    buy_id: Optional[str] = None
    buy_px: Optional[int] = None
    sell_id: Optional[str] = None
    sell_px: Optional[int] = None
    tight_since: Optional[float] = None
    stable_since: Optional[float] = None


def compute_quote(best_bid: Optional[int], best_ask: Optional[int]) -> Tuple[Optional[int], Optional[int], str]:
    if best_bid is None or best_ask is None:
        return None, None, "missing_bid_or_ask"

    spread = best_ask - best_bid
    if spread < MIN_SPREAD_CENTS:
        return None, None, f"spread_too_tight({spread})"

    # quote inside by 1 tick each side
    bid_px = best_bid + 1
    ask_px = best_ask - 1
    # keep at least MIN_SPREAD_CENTS between our quotes
    if ask_px - bid_px < MIN_SPREAD_CENTS:
        return None, None, f"spread_too_tight({spread})"

    return bid_px, ask_px, f"ok(spread={spread})"


def main():
    markets = get_markets_for_series(SERIES, status="open")
    market_id = pick_target_market_id(markets)
    if not market_id:
        raise RuntimeError(f"No open markets found for series={SERIES}")

    log.info(f"[BOOT] series={SERIES} market_id={market_id} DRY_RUN={DRY_RUN}")

    state = LiveOrders()

    while True:
        try:
            ob = get_orderbook(market_id)
            best_bid, best_ask = parse_yes_best_bid_ask(ob)
            log.info(f"[QUOTE] {market_id} YES bid={best_bid} ask={best_ask}")

            bid_px, ask_px, reason = compute_quote(best_bid, best_ask)

            # stability gate (same as your logs)
            now = time.time()
            if reason.startswith("ok"):
                if state.stable_since is None:
                    state.stable_since = now
                stable_for = now - state.stable_since
                if stable_for < STABLE_SECONDS:
                    log.info(f"[TARGET] {market_id} → SKIP (spread_ok_not_stable({best_ask - best_bid}) {stable_for:.2f}s<{STABLE_SECONDS:.2f}s)")
                    time.sleep(LOOP_SECONDS)
                    continue
            else:
                state.stable_since = None
                log.info(f"[TARGET] {market_id} → SKIP ({reason})")
                # if tight, start/track hold timer
                if reason.startswith("spread_too_tight"):
                    if state.tight_since is None:
                        state.tight_since = now
                    held_for = now - state.tight_since
                    log.info(f"[OM] {market_id} HOLD (tight_spread) held_for={held_for:.2f}s<{TIGHT_SPREAD_HOLD_SECONDS:.2f}s DRY_RUN={DRY_RUN}")
                    if held_for >= TIGHT_SPREAD_HOLD_SECONDS:
                        # cancel both if we have any live
                        if state.buy_id:
                            log.info(f"[OM] {market_id} BUY CANCEL @{state.buy_px} (tight_spread>{TIGHT_SPREAD_HOLD_SECONDS:.2f}s({reason})) DRY_RUN={DRY_RUN}")
                            cancel_order(state.buy_id)
                            state.buy_id, state.buy_px = None, None
                        if state.sell_id:
                            log.info(f"[OM] {market_id} SELL CANCEL @{state.sell_px} (tight_spread>{TIGHT_SPREAD_HOLD_SECONDS:.2f}s({reason})) DRY_RUN={DRY_RUN}")
                            cancel_order(state.sell_id)
                            state.sell_id, state.sell_px = None, None
                        state.tight_since = None
                else:
                    state.tight_since = None

                time.sleep(LOOP_SECONDS)
                continue

            # we have ok quote; reset tight timer
            state.tight_since = None

            # ✅ FIX: post-only safety check with CURRENT best_bid/best_ask
            safe = post_only_safe_prices(bid_px, ask_px, best_bid, best_ask)
            if safe is None:
                log.info(f"[TARGET] {market_id} → SKIP (post_only_would_cross)")
                time.sleep(LOOP_SECONDS)
                continue
            bid_px, ask_px = safe

            log.info(f"[TARGET] {market_id} YES-only would_quote: bid@{bid_px} ask@{ask_px} ({reason}) DRY_RUN={DRY_RUN}")

            # --- BUY side management ---
            desired_buy = bid_px
            if state.buy_id and state.buy_px is not None:
                off_by = abs(desired_buy - state.buy_px)
                if off_by < REPRICE_THRESHOLD_CENTS:
                    log.info(f"[OM] {market_id} BUY KEEP @{state.buy_px} qty={QTY} (off_by={off_by}<thresh({REPRICE_THRESHOLD_CENTS})) DRY_RUN={DRY_RUN} order_id={state.buy_id}")
                elif off_by >= MAX_CHASE_CENTS:
                    log.info(f"[OM] {market_id} BUY CANCEL @{state.buy_px} (too_far_to_chase(off_by={off_by}>=MAX_CHASE_CENTS={MAX_CHASE_CENTS})) DRY_RUN={DRY_RUN}")
                    cancel_order(state.buy_id)
                    state.buy_id, state.buy_px = None, None
                else:
                    log.info(f"[OM] {market_id} BUY CANCEL @{state.buy_px} (reprice(off_by={off_by})) DRY_RUN={DRY_RUN}")
                    cancel_order(state.buy_id)
                    state.buy_id, state.buy_px = None, None

            if state.buy_id is None:
                buy_id = place_order(market_id, "buy", desired_buy, QTY, post_only=True)
                state.buy_id = buy_id
                state.buy_px = desired_buy

            # ✅ FIX: re-fetch orderbook BEFORE placing SELL (prevents "post only cross")
            ob2 = get_orderbook(market_id)
            best_bid2, best_ask2 = parse_yes_best_bid_ask(ob2)
            log.info(f"[QUOTE] {market_id} YES bid={best_bid2} ask={best_ask2}")

            # recompute SELL using freshest book (keep same desired spread logic)
            bid_px2, ask_px2, reason2 = compute_quote(best_bid2, best_ask2)
            if not reason2.startswith("ok") or bid_px2 is None or ask_px2 is None:
                # if we can't safely quote, do NOT place sell this cycle
                time.sleep(LOOP_SECONDS)
                continue

            safe2 = post_only_safe_prices(bid_px2, ask_px2, best_bid2, best_ask2)
            if safe2 is None:
                # if SELL would cross now, skip placing it
                time.sleep(LOOP_SECONDS)
                continue

            _, desired_sell = safe2  # use the safe sell from fresh book

            # --- SELL side management ---
            if state.sell_id and state.sell_px is not None:
                off_by = abs(desired_sell - state.sell_px)
                if off_by < REPRICE_THRESHOLD_CENTS:
                    log.info(f"[OM] {market_id} SELL KEEP @{state.sell_px} qty={QTY} (off_by={off_by}<thresh({REPRICE_THRESHOLD_CENTS})) DRY_RUN={DRY_RUN} order_id={state.sell_id}")
                elif off_by >= MAX_CHASE_CENTS:
                    log.info(f"[OM] {market_id} SELL CANCEL @{state.sell_px} (too_far_to_chase(off_by={off_by}>=MAX_CHASE_CENTS={MAX_CHASE_CENTS})) DRY_RUN={DRY_RUN}")
                    cancel_order(state.sell_id)
                    state.sell_id, state.sell_px = None, None
                else:
                    log.info(f"[OM] {market_id} SELL CANCEL @{state.sell_px} (reprice(off_by={off_by})) DRY_RUN={DRY_RUN}")
                    cancel_order(state.sell_id)
                    state.sell_id, state.sell_px = None, None

            if state.sell_id is None:
                try:
                    sell_id = place_order(market_id, "sell", desired_sell, QTY, post_only=True)
                    state.sell_id = sell_id
                    state.sell_px = desired_sell
                except Exception as e:
                    # ✅ FIX: if SELL fails (e.g., post-only cross), cancel BUY so we don't stay one-sided
                    msg = str(e)
                    log.error(f"[LOOPERR] {msg}")
                    if state.buy_id:
                        log.info(f"[OM] {market_id} BUY CANCEL @{state.buy_px} (sell_failed) DRY_RUN={DRY_RUN}")
                        cancel_order(state.buy_id)
                        state.buy_id, state.buy_px = None, None
                    # don't crash the loop; keep going
                    time.sleep(LOOP_SECONDS)
                    continue

            time.sleep(LOOP_SECONDS)

        except Exception as e:
            log.error(f"[LOOPERR] {repr(e)}")
            time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
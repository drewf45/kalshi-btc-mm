"""Chunk 2 — Kalshi exchange client. Single source of exchange truth.

Rules:
- Per-order fee from the API response, never a formula
- Expiry timestamps only from the exchange market object, never computed locally
- Every write path re-reads live balance first
"""

import os
import time
import uuid
import base64
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlparse
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

log = logging.getLogger("k_worker.kalshi")

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

SERIES_TICKER = "KXBTC15M"
ENGINE_ID = "k_worker"


class OrderStatusUnavailable(Exception):
    """Raised when order status cannot be determined (API failure).
    Callers must handle — never guess state."""
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


class KalshiClient:
    def __init__(self, api_base: str, api_prefix: str, key_id: str, pem_b64: str):
        self.api_base = api_base.rstrip("/")
        self.api_prefix = api_prefix if api_prefix.startswith("/") else f"/{api_prefix}"
        self.key_id = key_id
        if not pem_b64:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")
        self.private_key = serialization.load_pem_private_key(
            base64.b64decode(pem_b64), password=None
        )
        self.session = requests.Session()

    def _sign_headers(self, method: str, full_url: str) -> Dict[str, str]:
        ts = str(now_ms())
        path = urlparse(full_url).path
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            asy_padding.PSS(
                mgf=asy_padding.MGF1(hashes.SHA256()),
                salt_length=asy_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request(self, method: str, path: str,
                params: Optional[Dict] = None,
                json_body: Optional[Dict] = None,
                timeout: float = 10.0) -> Any:
        import random
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.api_base}{self.api_prefix}{path}"
        url_with_q = url + "?" + urlencode(params) if params else url

        max_retries = 3
        base_delay = 0.5
        for attempt in range(max_retries + 1):
            headers = self._sign_headers(method, url)
            headers["Accept"] = "application/json"
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            resp = self.session.request(
                method=method.upper(), url=url_with_q,
                headers=headers, json=json_body, timeout=timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    log.warning(f"[API] HTTP {resp.status_code} {path} — retry {attempt+1}/{max_retries} in {delay:.1f}s")
                    time.sleep(delay)
                    continue
            break
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text or ''}")
        return resp.json() if resp.content else None


def build_client() -> KalshiClient:
    """Build a KalshiClient from env vars."""
    kalshi_env = os.environ.get("KALSHI_ENV", "demo").lower()
    if kalshi_env == "live":
        api_base = os.environ.get("KALSHI_API_BASE", "https://external-api.kalshi.com")
    else:
        api_base = os.environ.get("KALSHI_API_BASE", "https://external-api.demo.kalshi.co")
    api_prefix = os.environ.get("KALSHI_API_PREFIX", "/trade-api/v2")
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY_PEM_BASE64", "")
    return KalshiClient(api_base, api_prefix, key_id, pem_b64)


# ── Balance ──────────────────────────────────────────────────────

def get_balance(client: KalshiClient) -> Tuple[Optional[float], Optional[float]]:
    """Returns (cash_usd, portfolio_value_usd) or (None, None)."""
    try:
        resp = client.request("GET", "/portfolio/balance")
    except Exception as e:
        log.warning(f"[BALANCE] {e}")
        return None, None
    if not isinstance(resp, dict):
        return None, None
    cash_cents = resp.get("balance")
    pv_cents = resp.get("portfolio_value", 0)
    if cash_cents is not None:
        cash = float(cash_cents) / 100.0
        pv = float(pv_cents) / 100.0
        total = cash + pv
        log.info(f"[BALANCE] cash=${cash:.2f} positions=${pv:.2f} total=${total:.2f}")
        return cash, pv
    return None, None


# ── Market discovery ─────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def resolve_close_ts(mobj: Dict, ticker: str) -> Optional[int]:
    """Extract close timestamp from market object. Exchange truth only."""
    for k in ("close_ts", "closeTs", "close_time_ts", "expiration_ts"):
        v = mobj.get(k)
        if isinstance(v, (int, float)):
            vv = int(v)
            return vv // 1000 if vv > 10_000_000_000 else vv
    for k in ("close_time", "closeTime", "expiration_time"):
        v = mobj.get(k)
        if isinstance(v, str):
            ts = _parse_iso(v)
            if ts:
                return ts
    return None


def discover_market(client: KalshiClient) -> Tuple[str, str, Dict]:
    """Find the current KXBTC15M market. Returns (event_ticker, ticker, market_obj)."""
    params = {"series_ticker": SERIES_TICKER, "limit": 200}
    resp = client.request("GET", "/markets", params=params)
    mkts = resp.get("markets", []) if isinstance(resp, dict) else []
    if not mkts:
        raise RuntimeError(f"No open markets for {SERIES_TICKER}")

    now = time.time()
    best = None
    best_secs = float("inf")
    active = [m for m in mkts if m.get("status") == "active"]
    candidates = active if active else [
        m for m in mkts if m.get("status") not in ("finalized", "settled", "closed")
    ]
    for m in candidates:
        ticker = m.get("ticker") or m.get("market_ticker", "")
        close_ts = resolve_close_ts(m, ticker)
        if close_ts is None:
            continue
        secs = close_ts - now
        if secs < -60:
            continue
        if secs < best_secs:
            best_secs = secs
            best = (m.get("event_ticker", ""), ticker, m)
    if best:
        return best
    m = mkts[0]
    ticker = m.get("ticker", "")
    return m.get("event_ticker", ""), ticker, m


def get_market(client: KalshiClient, ticker: str) -> Dict:
    """Fetch a single market by ticker."""
    resp = client.request("GET", f"/markets/{ticker}")
    return resp.get("market", resp) if isinstance(resp, dict) else {}


# ── Orderbook ────────────────────────────────────────────────────

@dataclass
class Book:
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    yes_bid_qty: int = 0
    no_bid_qty: int = 0
    yes_bid_fp: Optional[str] = None
    no_bid_fp: Optional[str] = None


_ob_shape_logged = False


def fetch_orderbook(client: KalshiClient, ticker: str) -> Book:
    """Fetch and parse orderbook for a ticker.

    Endpoint: GET /markets/{ticker}/orderbook
    Fixed-point response: {"orderbook_fp": {"yes_dollars": [["0.97","5.00"], ...],
                                             "no_dollars": [...]}}
    Each side carries BIDS ONLY; arrays sorted ASCENDING (highest bid LAST).
    Strings support subpenny prices and fractional counts.
    """
    from decimal import Decimal
    global _ob_shape_logged
    try:
        resp = client.request("GET", f"/markets/{ticker}/orderbook")
    except Exception as e:
        log.warning(f"[OB] {ticker}: {e}")
        return Book()

    if not isinstance(resp, dict):
        return Book()

    if not _ob_shape_logged:
        log.info(f"[OB] shape: {list(resp.keys())}")
        _ob_shape_logged = True

    ob = resp.get("orderbook_fp") or {}

    def best(levels):
        if not levels:
            return None, None, None
        price_str, count_str = levels[-1]
        d = Decimal(price_str) * 100
        cents = int(d)
        if d != cents:
            log.debug(f"[OB] subpenny bid {price_str} on {ticker} — floored to {cents}c")
        return cents, int(Decimal(count_str)), price_str

    yes_bid, yes_bid_qty, yes_fp = best(ob.get("yes_dollars") or [])
    no_bid, no_bid_qty, no_fp = best(ob.get("no_dollars") or [])

    return Book(
        yes_bid=yes_bid, yes_bid_qty=yes_bid_qty or 0,
        no_bid=no_bid, no_bid_qty=no_bid_qty or 0,
        yes_ask=(100 - no_bid) if no_bid is not None else None,
        no_ask=(100 - yes_bid) if yes_bid is not None else None,
        yes_bid_fp=yes_fp,
        no_bid_fp=no_fp,
    )


# ── Positions ────────────────────────────────────────────────────

def get_positions(client: KalshiClient) -> List[Dict]:
    resp = client.request("GET", "/portfolio/positions", params={"limit": 200})
    if isinstance(resp, dict):
        for k in ("positions", "market_positions"):
            if k in resp and isinstance(resp[k], list):
                return resp[k]
    return resp if isinstance(resp, list) else []


def position_for_market(client: KalshiClient, ticker: str) -> int:
    """Returns contract count on ticker (positive=yes, 0=none)."""
    positions = get_positions(client)
    for p in positions:
        t = p.get("ticker") or p.get("market_ticker") or ""
        if str(t) != str(ticker):
            continue
        for k in ("position", "net_position", "yes_position", "qty", "count"):
            if k in p:
                try:
                    return int(p[k])
                except (ValueError, TypeError):
                    continue
    return 0


# ── Orders ───────────────────────────────────────────────────────

# P7 (0708): V2 events route FIRST — legacy /portfolio/orders* is slated
# for deprecation no earlier than 2026-05-21 with rate-limit costs rising
# from 2026-05-14 (Kalshi API changelog). Legacy kept as fallback only.
_ORDERS_ROUTE_CANDIDATES = ["/portfolio/events/orders", "/portfolio/orders"]
_orders_route: str = "/portfolio/events/orders"


def probe_orders_route(client: KalshiClient) -> str:
    """Probe orders-list route at boot — FATAL if neither candidate works."""
    global _orders_route
    for route in _ORDERS_ROUTE_CANDIDATES:
        try:
            client.request("GET", route, params={"limit": 1})
            _orders_route = route
            log.warning(f"[ORDERS] Route probe: {route} ✓")
            return route
        except Exception as e:
            log.warning(f"[ORDERS] Route probe: {route} ✗ ({e})")
    raise RuntimeError("FATAL: no orders-list route responded")


def cancel_order(client: KalshiClient, order_id: str) -> str:
    try:
        client.request("DELETE", f"/portfolio/events/orders/{order_id}")
        return "canceled"
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            try:
                client.request("DELETE", f"{_orders_route}/{order_id}")
                log.warning(f"[CANCEL] Fallback route {_orders_route}/{order_id} succeeded (migration)")
                return "canceled"
            except RuntimeError as e2:
                if "HTTP 404" in str(e2):
                    return "not_found"
                raise
        raise


def cancel_all_for_market(client: KalshiClient, ticker: str) -> int:
    """Cancel all resting orders for a ticker.
    Returns count cancelled, or -1 if cancellation state is unknown."""
    cancelled = 0
    try:
        resp = client.request("GET", _orders_route,
                              params={"ticker": ticker, "status": "resting", "limit": 200})
    except Exception as e:
        log.error(f"[CANCEL] Failed to list orders for {ticker}: {e}")
        return -1
    for o in (resp or {}).get("orders", []):
        oid = o.get("order_id") or o.get("id")
        if oid:
            try:
                cancel_order(client, str(oid))
                cancelled += 1
            except Exception as e:
                log.error(f"[CANCEL] Failed to cancel order {oid} on {ticker}: {e}")
                return -1
    return cancelled


_FILLS_ROUTE_PRIMARY = "/portfolio/events/fills"
_FILLS_ROUTE_FALLBACK = "/portfolio/fills"
_fills_route: str = _FILLS_ROUTE_FALLBACK


def probe_fills_route(client: KalshiClient) -> str:
    """Probe fills route at boot — FATAL if neither candidate works."""
    global _fills_route
    for route in [_FILLS_ROUTE_PRIMARY, _FILLS_ROUTE_FALLBACK]:
        try:
            client.request("GET", route, params={"limit": 1})
            _fills_route = route
            log.warning(f"[FILLS] Route probe: {route} ✓")
            return route
        except Exception as e:
            log.warning(f"[FILLS] Route probe: {route} ✗ ({e})")
    raise RuntimeError("FATAL: No working fills route — engine cannot see fills")


_fill_parse_warned = False


def parse_fill(fill: dict, our_side: str) -> Tuple[Optional[float], int, int]:
    """Parse a Kalshi fill into (cost_cents, fee_cents, count) for our_side.

    Kalshi fills carry yes_price/no_price. We map to cost for our held side.
    Unwraps one level if nested under 'fill'/'order'. Tries extended key
    candidates (yes_price_cents, price_cents, yes_price_dollars).
    Returns (None, fee, count) if price is unparseable — caller uses fallback.
    Logs the raw fill dict once on first parse failure (self-diagnosing).
    """
    global _fill_parse_warned
    from decimal import Decimal

    inner = fill
    for wrap_key in ("fill", "order"):
        if isinstance(fill.get(wrap_key), dict):
            inner = fill[wrap_key]
            break
    dicts = [inner, fill] if inner is not fill else [fill]

    def _get(key):
        for d in dicts:
            v = d.get(key)
            if v is not None:
                return v
        return None

    def _to_cents(raw):
        if raw is None:
            return None
        try:
            val = Decimal(str(raw))
            return float(val * 100) if val < 1 else float(val)
        except Exception:
            return None

    def _raw_cents(raw):
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    cost_cents = None
    fee_cents = 0
    count = 1

    # Tape 0709: live fills carry count_fp ('1.00') — parse via Decimal.
    for ck in ("count", "count_fp", "quantity", "qty"):
        raw = _get(ck)
        if raw is not None:
            try:
                count = int(Decimal(str(raw)))
            except Exception:
                pass
            break

    YES_DOLLAR_KEYS = ("yes_price", "yes_price_dollars")
    NO_DOLLAR_KEYS = ("no_price", "no_price_dollars")

    if our_side == "yes":
        for k in YES_DOLLAR_KEYS:
            cost_cents = _to_cents(_get(k))
            if cost_cents is not None:
                break
        if cost_cents is None:
            cost_cents = _raw_cents(_get("yes_price_cents"))
        if cost_cents is None:
            for k in NO_DOLLAR_KEYS:
                no_c = _to_cents(_get(k))
                if no_c is not None:
                    cost_cents = 100 - no_c
                    break
        if cost_cents is None:
            no_c = _raw_cents(_get("no_price_cents"))
            if no_c is not None:
                cost_cents = 100 - no_c
    else:
        for k in NO_DOLLAR_KEYS:
            cost_cents = _to_cents(_get(k))
            if cost_cents is not None:
                break
        if cost_cents is None:
            cost_cents = _raw_cents(_get("no_price_cents"))
        if cost_cents is None:
            for k in YES_DOLLAR_KEYS:
                yes_c = _to_cents(_get(k))
                if yes_c is not None:
                    cost_cents = 100 - yes_c
                    break
        if cost_cents is None:
            yes_c = _raw_cents(_get("yes_price_cents"))
            if yes_c is not None:
                cost_cents = 100 - yes_c

    if cost_cents is None:
        val = _to_cents(_get("price"))
        if val is not None:
            cost_cents = val if our_side == "yes" else (100 - val)
    if cost_cents is None:
        pc = _raw_cents(_get("price_cents"))
        if pc is not None:
            cost_cents = pc if our_side == "yes" else (100 - pc)

    if cost_cents is None and not _fill_parse_warned:
        log.warning(f"[FILLS] Unparsed fill record (raw): {fill}")
        _fill_parse_warned = True

    # Tape 0709: live fills carry fee_cost ('0.000000', dollars).
    for fee_key in ("fee", "fee_cost", "taker_fee", "maker_fee"):
        raw = _get(fee_key)
        if raw is not None:
            try:
                val = Decimal(str(raw))
                fee_cents = int(val * 100) if val < 1 else int(val)
            except Exception:
                pass
            break

    return cost_cents, fee_cents, count


def parse_fills(fill_records: list, our_side: str) -> Tuple[Optional[float], int, int]:
    """Parse multiple fill records into (avg_cost_cents, total_fee_cents, total_count).
    Computes count-weighted average cost across records for partial fills."""
    if not fill_records:
        return None, 0, 0

    weighted_cost = 0.0
    parsed_count = 0
    total_fee = 0
    total_count = 0

    for fr in fill_records:
        cost, fee, cnt = parse_fill(fr, our_side)
        total_fee += fee
        total_count += cnt
        if cost is not None:
            weighted_cost += cost * cnt
            parsed_count += cnt

    if parsed_count > 0:
        return weighted_cost / parsed_count, total_fee, total_count
    return None, total_fee, total_count


_resting_shape_logged = False
_fills_shape_logged = False


def order_status(client: KalshiClient, ticker: str, order_id: str) -> Dict:
    """Fills are immutable broker truth — check them FIRST.
    Returns {state: 'resting'|'filled'|'gone', raw: ...}.
    Raises OrderStatusUnavailable on API failure (caller must handle; never guess)."""
    global _resting_shape_logged, _fills_shape_logged

    # 1. Fills first — immutable broker truth
    try:
        fr = client.request("GET", _fills_route,
                            params={"ticker": ticker, "limit": 100})
    except Exception as e:
        raise OrderStatusUnavailable(f"fills endpoint failed for {ticker}: {e}") from e

    fills = (fr or {}).get("fills") or []
    if fills and not _fills_shape_logged:
        log.info(f"[FILLS-V2] shape: {fills[0]}")
        _fills_shape_logged = True
    mine = [f for f in fills if str(f.get("order_id")) == str(order_id)]
    if mine:
        return {"state": "filled", "raw": mine}

    # 2. Per-order GET — single-object reads index sooner than list
    try:
        o = client.request("GET", f"{_orders_route}/{order_id}")
        if o:
            order_obj = o.get("order") or o
            if not _resting_shape_logged:
                log.info(f"[ORDER-V2] per-order shape: {order_obj}")
                _resting_shape_logged = True
            st = str(order_obj.get("status", "")).lower()
            if st in ("resting", "open", "pending"):
                return {"state": "resting", "raw": order_obj}
            if st in ("executed", "filled"):
                return {"state": "resting", "raw": order_obj}
            return {"state": "gone", "raw": order_obj}
    except Exception:
        pass  # fall through to list endpoint

    # 3. Orders list — fallback if per-order GET fails (404, 5xx, etc.)
    try:
        resp = client.request("GET", _orders_route,
                              params={"ticker": ticker, "limit": 200})
    except Exception as e:
        raise OrderStatusUnavailable(f"orders list failed for {ticker}: {e}") from e

    orders = (resp or {}).get("orders") or []
    for o in orders:
        if str(o.get("order_id") or o.get("id")) == str(order_id):
            st = str(o.get("status", "")).lower()
            if st in ("resting", "open", "pending"):
                return {"state": "resting", "raw": o}
            if st in ("executed", "filled"):
                return {"state": "resting", "raw": o}
            return {"state": "gone", "raw": o}

    return {"state": "gone", "raw": None}


def amend_order(client: KalshiClient, order_id: str, ticker: str, side: str,
                price_cents: int, count: int = 1,
                v2_price_str: Optional[str] = None) -> Tuple[Optional[str], Dict]:
    """P4 (0708): atomic reprice via Amend Order V2 —
    POST /portfolio/events/orders/{order_id}/amend.
    Replaces the cancel+place path, which had a fill-race double-entry window
    (cancel returning not_found on a just-filled order was ignored and a new
    order was placed anyway). Amend cannot double-enter by construction.

    side: engine 'yes'/'no'. V2 quotes the YES leg: yes->bid at price,
    no->ask at (1 - price). Returns (new_order_id, response) or (None, {})
    on failure — caller treats failure as safe (order state unchanged or
    gone; monitor loop's fills-first status will resolve it).
    """
    from decimal import Decimal
    if v2_price_str is not None:
        if side == "yes":
            v2_side, v2_price = "bid", v2_price_str
        else:
            v2_side, v2_price = "ask", str(Decimal("1") - Decimal(v2_price_str))
    elif side == "yes":
        v2_side, v2_price = "bid", f"{price_cents / 100:.2f}"
    else:
        v2_side, v2_price = "ask", f"{(100 - price_cents) / 100:.2f}"

    # Recover the original client_order_id from the per-order GET (amend
    # request shape wants both original and updated client_order_id).
    client_order_id = None
    try:
        o = client.request("GET", f"{_orders_route}/{order_id}")
        if o:
            obj = o.get("order") or o
            client_order_id = obj.get("client_order_id")
    except Exception as e:
        log.warning(f"[AMEND] per-order GET failed for {order_id}: {e}")

    body: Dict[str, Any] = {
        "ticker": ticker,
        "side": v2_side,
        "price": v2_price,
        "count": str(max(1, int(count))),
        "updated_client_order_id": str(uuid.uuid4()),
    }
    if client_order_id:
        body["client_order_id"] = str(client_order_id)

    # Tape 0709: this API base 404s on /portfolio/events/* GETs — try the
    # events amend path first, fall back to the probed legacy route.
    resp = None
    for route in (f"/portfolio/events/orders/{order_id}/amend",
                  f"{_orders_route}/{order_id}/amend"):
        try:
            resp = client.request("POST", route, json_body=body)
            break
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                log.warning(f"[AMEND] {route} 404 — trying fallback")
                continue
            log.warning(f"[AMEND] amend failed for {order_id}: {e}")
            return None, {}
        except Exception as e:
            log.warning(f"[AMEND] amend failed for {order_id}: {e}")
            return None, {}
    if resp is None:
        log.warning(f"[AMEND] no amend route responded for {order_id}")
        return None, {}
    log.info(f"[AMEND] resp: {resp}")
    if isinstance(resp, dict):
        new_oid = resp.get("order_id") or order_id
        return str(new_oid), resp
    return None, {}


def place_order_maker(client: KalshiClient, ticker: str, side: str,
                      price_cents: int, count: int = 1,
                      expiration_ts: Optional[int] = None,
                      v2_price_str: Optional[str] = None) -> Tuple[str, Dict]:
    """Place a post_only (maker) limit order via V2. Returns (order_id, response).

    side: 'yes'/'no' (engine convention). price_cents: COST of the held side.
    v2_price_str: exact fixed-point dollar string from orderbook_fp — used when
    available so orders rest at the true touch (subpenny precision).
    V2 quotes the YES leg only: buy NO == ask YES at (100 - cost).
    The ONLY order placement function in the engine — no taker path exists.
    """
    from decimal import Decimal
    if v2_price_str is not None:
        if side == "yes":
            v2_side = "bid"
            v2_price = v2_price_str
        else:
            v2_side = "ask"
            v2_price = str(Decimal("1") - Decimal(v2_price_str))
    elif side == "yes":
        v2_side = "bid"
        v2_price = f"{price_cents / 100:.2f}"
    else:
        v2_side = "ask"
        v2_price = f"{(100 - price_cents) / 100:.2f}"

    body: Dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": v2_side,
        "count": str(max(1, int(count))),
        "price": v2_price,
        "time_in_force": "good_till_canceled",
        "post_only": True,
        "self_trade_prevention_type": "taker_at_cross",
    }
    if expiration_ts is not None and expiration_ts > int(time.time()):
        body["expiration_time"] = int(expiration_ts)

    resp = client.request("POST", "/portfolio/events/orders", json_body=body)
    log.info(f"[ORDER-V2] resp: {resp}")
    if isinstance(resp, dict):
        oid = resp.get("order_id")
        if oid:
            return str(oid), resp
    raise RuntimeError(f"Unexpected V2 order response: {resp}")


# ── Settlement ───────────────────────────────────────────────────

def get_settlement_result(client: KalshiClient, ticker: str) -> Optional[str]:
    """Check if a market has settled. Returns 'yes', 'no', or None."""
    try:
        mkt = get_market(client, ticker)
        result = mkt.get("result", "").lower()
        if result in ("yes", "no"):
            return result
    except Exception:
        pass
    return None


def get_fills(client: KalshiClient, ticker: str) -> List[Dict]:
    """Get fills for a specific ticker.
    Raises on API failure — caller must handle (empty list is indistinguishable from failure)."""
    resp = client.request("GET", _fills_route,
                          params={"ticker": ticker, "limit": 100})
    if isinstance(resp, dict):
        return resp.get("fills", [])
    return resp if isinstance(resp, list) else []


def get_all_recent_fills(client: KalshiClient, limit: int = 200) -> List[Dict]:
    """Get recent fills across all tickers for reconciliation.
    Raises on API failure."""
    resp = client.request("GET", _fills_route,
                          params={"limit": limit})
    if isinstance(resp, dict):
        return resp.get("fills", [])
    return resp if isinstance(resp, list) else []


# ── BTC Spot (Fix 4) ───────────────────────────────────────────

_spot_cache: Dict[str, Any] = {"price": None, "ts": 0.0}
SPOT_MAX_STALE_SEC = 30.0


def get_btc_spot() -> Optional[float]:
    """BTC spot from Coinbase public ticker, cached <=5s.
    P6 (0708): on fetch failure the cache is only served if it is younger
    than SPOT_MAX_STALE_SEC — an unbounded stale fallback was silently
    feeding hour-old prices into H8 distance and the top-rung guard.
    Older than the bound -> None; consumers already fail safe on None
    (H8_NO_SPOT skip, guard skipped). Fail-loud doctrine."""
    now = time.time()
    if _spot_cache["price"] is not None and (now - _spot_cache["ts"]) < 5:
        return _spot_cache["price"]
    try:
        resp = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5,
        )
        resp.raise_for_status()
        price = float(resp.json()["data"]["amount"])
        _spot_cache["price"] = price
        _spot_cache["ts"] = now
        return price
    except Exception as e:
        age = now - _spot_cache["ts"]
        if _spot_cache["price"] is not None and age < SPOT_MAX_STALE_SEC:
            log.warning(f"[SPOT] Coinbase fetch failed: {e} — serving cache age {age:.0f}s")
            return _spot_cache["price"]
        log.warning(f"[SPOT] Coinbase fetch failed: {e} — cache stale ({age:.0f}s) -> BLIND (None)")
        return None


# ── Boundary extraction (Fix 4) ────────────────────────────────

_boundary_keys_logged = False


def extract_boundaries(market_obj: Dict) -> Tuple[Optional[float], Optional[float]]:
    """Extract lo/hi price boundaries from market object. Never parse tickers."""
    global _boundary_keys_logged
    if not _boundary_keys_logged:
        log.info(f"[BOUNDARY] Market keys: {sorted(market_obj.keys())}")
        _boundary_keys_logged = True

    lo, hi = None, None
    for k in ("floor_strike", "custom_strike_floor", "strike_low", "range_low"):
        v = market_obj.get(k)
        if v is not None:
            try:
                lo = float(v)
                break
            except (ValueError, TypeError):
                pass
    for k in ("cap_strike", "custom_strike_cap", "strike_high", "range_high"):
        v = market_obj.get(k)
        if v is not None:
            try:
                hi = float(v)
                break
            except (ValueError, TypeError):
                pass
    cs = market_obj.get("custom_strike")
    if isinstance(cs, dict):
        if lo is None:
            for ck in ("floor", "low", "min"):
                try:
                    lo = float(cs[ck])
                    break
                except (KeyError, ValueError, TypeError):
                    pass
        if hi is None:
            for ck in ("cap", "high", "max"):
                try:
                    hi = float(cs[ck])
                    break
                except (KeyError, ValueError, TypeError):
                    pass
    return lo, hi


# ── Fee-change tripwire (P8, 0708) ─────────────────────────────

_FEE_ROUTE_CANDIDATES = ["/exchange/series_fee_changes", "/series/fee_changes"]
_fee_route_dead = False


def get_series_fee_changes(client: KalshiClient,
                           series_ticker: str = SERIES_TICKER) -> Optional[list]:
    """P8 (0708): the thesis is sized in single cents — a maker-fee change on
    KXBTC15M kills the 98-99c rungs. Kalshi publishes Get Series Fee Changes;
    exact route probed from candidates (schema drift tolerated, fail-loud
    once). Returns list of change records touching the series, [] if none,
    None if the endpoint is unreachable (caller alerts once, non-fatal)."""
    global _fee_route_dead
    if _fee_route_dead:
        return None
    last_err = None
    for route in _FEE_ROUTE_CANDIDATES:
        try:
            resp = client.request("GET", route,
                                  params={"series_ticker": series_ticker, "limit": 50})
            if isinstance(resp, dict):
                for k in ("fee_changes", "series_fee_changes", "changes"):
                    if isinstance(resp.get(k), list):
                        return [c for c in resp[k]
                                if series_ticker in str(c.get("series_ticker", c))]
                return []
            return []
        except Exception as e:
            last_err = e
            continue
    log.warning(f"[FEES] No fee-changes route responded ({last_err}) — tripwire disabled this boot")
    _fee_route_dead = True
    return None


# ── Queue position (P9, 0708) ──────────────────────────────────

def get_order_queue_position(client: KalshiClient, order_id: str) -> Optional[int]:
    """P9 (0708): read our resting order's queue position. Primary source is
    the queue_position field on the per-order GET; dedicated endpoint as
    fallback. Instruments the 22% no-fill baseline — distinguishes 'deep in
    queue' from 'book never traded', and gates the queue-jump knob (P12)
    on measurement instead of guesswork. Returns None on any failure."""
    try:
        o = client.request("GET", f"{_orders_route}/{order_id}")
        if o:
            obj = o.get("order") or o
            qp = obj.get("queue_position")
            if qp is not None:
                return int(qp)
    except Exception:
        pass
    try:
        r = client.request("GET", f"{_orders_route}/{order_id}/queue_position")
        if isinstance(r, dict):
            qp = r.get("queue_position")
            if qp is not None:
                return int(qp)
    except Exception:
        pass
    return None


# ── Census (Part 4.2) ──────────────────────────────────────────

def get_todays_settled_tickers(client: KalshiClient) -> List[str]:
    """Get all KXBTC15M tickers that closed/settled today (ET day boundary)."""
    now_et = datetime.now(NY)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        resp = client.request("GET", "/markets",
                              params={"series_ticker": SERIES_TICKER, "limit": 200})
    except Exception as e:
        log.warning(f"[CENSUS] Failed to fetch markets: {e}")
        return []
    mkts = resp.get("markets", []) if isinstance(resp, dict) else []
    tickers = []
    for m in mkts:
        ticker = m.get("ticker") or m.get("market_ticker", "")
        status = (m.get("status") or "").lower()
        if status not in ("settled", "finalized", "closed"):
            continue
        close_ts = resolve_close_ts(m, ticker)
        if close_ts is not None and close_ts >= today_start:
            tickers.append(ticker)
    return tickers

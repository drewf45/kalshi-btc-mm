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
                salt_length=asy_padding.PSS.MAX_LENGTH,
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
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.api_base}{self.api_prefix}{path}"
        url_with_q = url + "?" + urlencode(params) if params else url
        headers = self._sign_headers(method, url)
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self.session.request(
            method=method.upper(), url=url_with_q,
            headers=headers, json=json_body, timeout=timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text or ''}")
        return resp.json() if resp.content else None


def build_client() -> KalshiClient:
    """Build a KalshiClient from env vars."""
    kalshi_env = os.environ.get("KALSHI_ENV", "demo").lower()
    if kalshi_env == "live":
        api_base = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com")
    else:
        api_base = os.environ.get("KALSHI_API_BASE", "https://demo-api.kalshi.co")
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


def parse_orderbook(ob: Any) -> Book:
    """Parse orderbook response into Book with best bid/ask and depth at touch."""
    if not isinstance(ob, dict):
        return Book()

    ob_data = ob.get("orderbook", ob)
    yes_levels = ob_data.get("yes", [])
    no_levels = ob_data.get("no", [])

    def _best_bid_with_qty(levels):
        if not isinstance(levels, list) or not levels:
            return None, 0
        best_p, best_q = None, 0
        for lv in levels:
            p, q = None, 0
            if isinstance(lv, (list, tuple)) and len(lv) >= 1:
                try:
                    p = int(lv[0])
                    q = int(lv[1]) if len(lv) >= 2 else 0
                except (ValueError, TypeError):
                    continue
            elif isinstance(lv, dict):
                try:
                    p = int(lv.get("price", lv.get("p", 0)))
                    q = int(lv.get("quantity", lv.get("q", 0)))
                except (ValueError, TypeError):
                    continue
            if p is not None and 1 <= p <= 99:
                if best_p is None or p > best_p:
                    best_p, best_q = p, q
        return best_p, best_q

    def _best_ask(levels):
        if not isinstance(levels, list) or not levels:
            return None
        asks = []
        for lv in levels:
            p = None
            if isinstance(lv, (list, tuple)) and len(lv) >= 1:
                try:
                    p = int(lv[0])
                except (ValueError, TypeError):
                    continue
            elif isinstance(lv, dict):
                try:
                    p = int(lv.get("price", lv.get("p", 0)))
                except (ValueError, TypeError):
                    continue
            if p is not None and 1 <= p <= 99:
                asks.append(p)
        return min(asks) if asks else None

    yes_bid, yes_bid_qty = _best_bid_with_qty(yes_levels)
    no_bid, no_bid_qty = _best_bid_with_qty(no_levels)

    _no_ask_raw = _best_ask(no_levels)
    _yes_ask_raw = _best_ask(yes_levels)
    yes_ask = (100 - _no_ask_raw) if _no_ask_raw is not None else None
    no_ask = (100 - _yes_ask_raw) if _yes_ask_raw is not None else None

    return Book(
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        yes_bid_qty=yes_bid_qty, no_bid_qty=no_bid_qty,
    )


def fetch_orderbook(client: KalshiClient, ticker: str) -> Book:
    """Fetch and parse orderbook for a ticker."""
    try:
        ob = client.request("GET", f"/orderbook/v2/{ticker}")
        return parse_orderbook(ob)
    except Exception as e:
        log.warning(f"[OB] {ticker}: {e}")
        return Book()


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

def get_open_orders(client: KalshiClient) -> List[Dict]:
    resp = client.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
    if isinstance(resp, dict):
        return resp.get("orders", [])
    return resp if isinstance(resp, list) else []


def cancel_order(client: KalshiClient, order_id: str) -> str:
    try:
        client.request("DELETE", f"/portfolio/orders/{order_id}")
        return "canceled"
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return "not_found"
        raise


def cancel_all_for_market(client: KalshiClient, ticker: str) -> int:
    """Cancel all resting orders for a ticker. Returns count cancelled."""
    cancelled = 0
    try:
        for o in get_open_orders(client):
            if str(o.get("ticker", "")) == str(ticker):
                oid = o.get("order_id") or o.get("id")
                if oid:
                    try:
                        cancel_order(client, str(oid))
                        cancelled += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return cancelled


def get_order(client: KalshiClient, order_id: str) -> Optional[Dict]:
    try:
        resp = client.request("GET", f"/portfolio/orders/{order_id}")
        return resp.get("order", resp) if isinstance(resp, dict) else None
    except Exception as e:
        log.warning(f"[ORDER] get {order_id}: {e}")
        return None


def place_order_maker(client: KalshiClient, ticker: str, side: str,
                      price_cents: int, count: int = 1,
                      expiration_ts: Optional[int] = None) -> str:
    """Place a post_only (maker) limit order. Returns order_id or raises.

    The ONLY order placement function in the engine — no taker path exists.
    """
    body: Dict[str, Any] = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": max(1, int(count)),
        "client_order_id": f"{ENGINE_ID}-{uuid.uuid4().hex[:12]}",
        "post_only": True,
        "time_in_force": "good_till_canceled",
    }
    if side == "yes":
        body["yes_price"] = int(price_cents)
    else:
        body["no_price"] = int(price_cents)
    if expiration_ts is not None and expiration_ts > int(time.time()):
        body["expiration_ts"] = expiration_ts

    resp = client.request("POST", "/portfolio/orders", json_body=body)
    if isinstance(resp, dict):
        order = resp.get("order", resp)
        oid = order.get("order_id") or resp.get("order_id")
        if oid:
            return str(oid)
    raise RuntimeError(f"Unexpected order response: {resp}")


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
    """Get fills for a specific ticker."""
    try:
        resp = client.request("GET", "/portfolio/fills",
                              params={"ticker": ticker, "limit": 100})
        if isinstance(resp, dict):
            return resp.get("fills", [])
        return resp if isinstance(resp, list) else []
    except Exception as e:
        log.warning(f"[FILLS] {ticker}: {e}")
        return []

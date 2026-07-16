# f_worker/lib/kalshi.py
# BORROWED (behaviour) from bot.py: the RSA-PSS signed Kalshi client — auth, request,
# orders, cancels, amends, fills, orderbook, balance, positions, 15M discovery.
#
# This is the ONLY module in f_worker that imports `cryptography`. Keep it that way:
# the testable core (walls, state machine, feemath, ledger, manager, pricebrain) must
# stay importable without the crypto stack. Crypto-free helpers live in marketutil.py.

import base64
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

from .marketutil import (pick_active_market, resolve_close_ts,  # crypto-free helpers
                         throttled_print as _throttled_print)


def now_ms() -> int:
    return int(time.time() * 1000)


class KalshiClient:
    def __init__(self, api_base: str, api_prefix: str, key_id: str, private_key_pem_b64: str):
        self.api_base = api_base.rstrip("/")
        self.api_prefix = api_prefix if api_prefix.startswith("/") else f"/{api_prefix}"
        self.key_id = key_id
        if not private_key_pem_b64:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_BASE64")
        pem_bytes = base64.b64decode(private_key_pem_b64)
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        self.session = requests.Session()

    def _sign_headers(self, method: str, full_url: str) -> Dict[str, str]:
        ts = str(now_ms())
        path = urlparse(full_url).path
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            asy_padding.PSS(mgf=asy_padding.MGF1(hashes.SHA256()),
                            salt_length=asy_padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                json_body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.api_base}{self.api_prefix}{path}"
        url_with_q = url + "?" + urlencode(params) if params else url
        headers = self._sign_headers(method, url)
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self.session.request(method=method.upper(), url=url_with_q, headers=headers,
                                    json=json_body, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: body={resp.text or ''}")
        return resp.json() if resp.content else None

    # ---- balance (W6: live re-read before every order) ----
    def get_balance_usd(self) -> Tuple[Optional[float], Optional[float]]:
        try:
            resp = self.request("GET", "/portfolio/balance")
        except Exception:
            return None, None
        if not isinstance(resp, dict):
            return None, None
        base = resp.get("balance") if isinstance(resp.get("balance"), dict) else resp
        av = tot = None
        for k in ("available_balance", "available", "available_cash", "available_funds",
                  "free_collateral", "available_collateral"):
            if k in base:
                try:
                    av = float(base[k]); break
                except Exception:
                    pass
        for k in ("balance", "total_balance", "total", "equity", "account_value"):
            if k in base:
                try:
                    tot = float(base[k]); break
                except Exception:
                    pass
        return av, tot

    # ---- orders / cancels / amends ----
    def place_order(self, payload: Dict[str, Any]) -> str:
        resp = self.request("POST", "/portfolio/orders", json_body=payload)
        if isinstance(resp, dict):
            if isinstance(resp.get("order"), dict) and resp["order"].get("order_id"):
                return str(resp["order"]["order_id"])
            if resp.get("order_id"):
                return str(resp["order_id"])
        raise RuntimeError(f"Unexpected create order response: {resp}")

    def cancel_order(self, order_id: str) -> str:
        try:
            self.request("DELETE", f"/portfolio/orders/{order_id}")
            return "canceled"
        except RuntimeError as e:
            if ("HTTP 404" in str(e)) or ("not_found" in str(e)):
                return "not_found"
            raise

    def amend_order(self, order_id: str, new_price_cents: int, side: str, count: int) -> str:
        body: Dict[str, Any] = {"count": int(count)}
        body["yes_price" if side == "yes" else "no_price"] = int(new_price_cents)
        resp = self.request("POST", f"/portfolio/orders/{order_id}/amend", json_body=body)
        if isinstance(resp, dict) and isinstance(resp.get("order"), dict):
            return str(resp["order"].get("order_id", order_id))
        return order_id

    def get_open_orders(self, market_ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"status": "resting", "limit": 200}
        if market_ticker:
            params["ticker"] = market_ticker
        resp = self.request("GET", "/portfolio/orders", params=params)
        return resp.get("orders", []) if isinstance(resp, dict) else (resp or [])

    def get_fills(self, market_ticker: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit}
        if market_ticker:
            params["ticker"] = market_ticker
        resp = self.request("GET", "/portfolio/fills", params=params)
        return resp.get("fills", []) if isinstance(resp, dict) else (resp or [])

    def get_positions(self) -> List[Dict[str, Any]]:
        resp = self.request("GET", "/portfolio/positions", params={"limit": 200})
        if isinstance(resp, dict):
            for k in ("positions", "market_positions", "portfolio_positions"):
                if isinstance(resp.get(k), list):
                    return resp[k]
        return resp if isinstance(resp, list) else []

    def get_orderbook(self, market_ticker: str, depth: int = 10) -> Dict[str, Any]:
        resp = self.request("GET", f"/markets/{market_ticker}/orderbook", params={"depth": depth})
        return resp if isinstance(resp, dict) else {}

    def get_market(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Single market object — carries status/result (settlement), fee params (tripwire),
        and is the direct-discovery probe (THE COMPUTED BELL). Returns None on 404 (the
        market is not born yet); raises on any other error so it never masks a real fault."""
        try:
            resp = self.request("GET", f"/markets/{market_ticker}")
        except RuntimeError as e:
            if "HTTP 404" in str(e) or "not_found" in str(e):
                return None
            raise
        if isinstance(resp, dict):
            return resp.get("market") if isinstance(resp.get("market"), dict) else resp
        return None

    def list_markets(self, series_ticker: str, status: Optional[str] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
        # F3.1: NO status filter by default. At a bell, status="open" returns ONLY the
        # just-closed corpse (still flagged open) — the newborn window is "unopened" and
        # invisible. Fetch every status and let the clock filter in pick_active_market be
        # the sole arbiter (proven by tape: `markets=1` at the bell was the whole bug).
        params: Dict[str, Any] = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        resp = self.request("GET", "/markets", params=params)
        return resp.get("markets", []) if isinstance(resp, dict) else (resp or [])

    # ---- discovery: nearest KXBTC15M window (borrowed from bot.py) ----
    def discover_market(self, series_ticker: str) -> Optional[Tuple[str, str, Dict[str, Any], int]]:
        markets = self.list_markets(series_ticker)   # all statuses; clock arbitrates
        if not markets:
            _throttled_print(f"[discover] list_markets returned 0 markets for {series_ticker}")
            return None
        try:
            event_ticker, market_ticker, chosen = pick_active_market(markets)
        except RuntimeError as e:
            # never swallow the picker's reason anonymously — the desk turns None into a
            # loud discovery_empty page, and this print names WHY it was empty.
            _throttled_print(f"[discover] {e} (markets={len(markets)})")
            return None
        close_ts = resolve_close_ts(chosen, market_ticker)
        if close_ts is None:
            return None
        return event_ticker, market_ticker, chosen, close_ts

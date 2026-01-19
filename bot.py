#!/usr/bin/env python3
import os
import json
import time
import base64
import logging
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List, Union

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# ----------------------------
# Small utils
# ----------------------------
def is_dict(x: Any) -> bool: return isinstance(x, dict)
def is_list(x: Any) -> bool: return isinstance(x, list)

def first_item(x: Any) -> Any:
    return x[0] if is_list(x) and x else None

def env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return default

def env_bool(*names: str, default: bool = False) -> bool:
    v = env_first(*names)
    if v is None: return default
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")

def env_int(*names: str, default: int) -> int:
    v = env_first(*names)
    if v is None: return default
    try: return int(str(v).strip())
    except Exception: return default

def env_float(*names: str, default: float) -> float:
    v = env_first(*names)
    if v is None: return default
    try: return float(str(v).strip())
    except Exception: return default

def now_et() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))

def usd_to_cents(u: float) -> int:
    return int(round(float(u) * 100))

def cents_to_usd(c: int) -> float:
    return float(c) / 100.0

# ----------------------------
# Email (SMTP)
# ----------------------------
import smtplib
from email.message import EmailMessage

def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        return
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_TO:
        log.warning("EMAIL_ENABLED=True but SMTP/EMAIL_TO not fully configured. Skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        if SMTP_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls()
                s.login(SMTP_USERNAME, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.login(SMTP_USERNAME, SMTP_PASSWORD)
                s.send_message(msg)
        log.info("Daily email sent.")
    except Exception as e:
        log.exception(f"Email send failed: {e}")

# ----------------------------
# Kalshi client (RSA signing)
# ----------------------------
class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key_pem_b64: str, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.subaccount = subaccount

        pem_bytes = base64.b64decode(private_key_pem_b64)
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.api_prefix: Optional[str] = None

    def _sign(self, method: str, path_with_query: str, body: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        message = (ts + method.upper() + path_with_query + body).encode("utf-8")

        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount
        return headers

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> requests.Response:
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))

        if params:
            parts = [f"{k}={params[k]}" for k in sorted(params.keys())]
            qs = "&".join(parts)
            path_for_sig = f"{path}?{qs}"
        else:
            path_for_sig = path

        headers = self._sign(method, path_for_sig, body)
        url = self._url(path)

        return self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            data=None if json_body is None else body,
            headers=headers,
            timeout=20,
        )

    def get(self, path: str, params: Optional[dict] = None) -> requests.Response:
        return self.request("GET", path, params=params, json_body=None)

    def post(self, path: str, json_body: dict) -> requests.Response:
        return self.request("POST", path, params=None, json_body=json_body)

    def discover_prefix(self) -> str:
        candidates = ["/trade-api/v2", "/trade-api/v1", "/trade-api", "/v2", "/v1", ""]
        last = None
        for pref in candidates:
            test_path = f"{pref}/markets"
            r = self.get(test_path, params={"limit": 1})
            last = r
            if r.status_code == 200:
                self.api_prefix = pref
                log.info(f"Discovered API prefix: {pref or '/'} (probe {test_path}?limit=1 -> 200)")
                return pref
        raise RuntimeError(f"Could not discover API prefix. Last={last.status_code if last else 'NA'} {last.text[:200] if last else 'NA'}")

    def p(self, suffix: str) -> str:
        if self.api_prefix is None:
            self.discover_prefix()
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{self.api_prefix}{suffix}"

# ----------------------------
# JSON helpers
# ----------------------------
def parse_json_safe(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None

def summarize_shape(x: Any, depth: int = 2) -> str:
    """Keys/types only. No values. Safe for logs."""
    if depth <= 0:
        return type(x).__name__
    if is_dict(x):
        keys = sorted(list(x.keys()))
        preview = keys[:30]
        return f"dict keys={preview}{'...' if len(keys) > 30 else ''}"
    if is_list(x):
        return f"list len={len(x)} first={summarize_shape(first_item(x), depth-1)}"
    if x is None:
        return "None"
    return type(x).__name__

def find_cash_recursively(node: Any) -> Optional[float]:
    """
    Robust cash finder:
    - looks for keys containing 'cash' at any level
    - supports USD floats or cents ints
    - supports dict/list nesting
    """
    if node is None:
        return None

    # If list, scan items
    if is_list(node):
        for item in node[:25]:
            got = find_cash_recursively(item)
            if got is not None:
                return got
        return None

    if not is_dict(node):
        return None

    # 1) Direct key hits (USD-like)
    usd_keys = [
        "cash", "available_cash", "cash_available", "availableCash",
        "cash_usd", "available_cash_usd", "cashAvailable",
    ]
    for k in usd_keys:
        if k in node and node[k] is not None:
            try:
                # Sometimes cash is nested dict like {"value": 49.8}
                if is_dict(node[k]):
                    for kk in ("usd", "value", "amount", "dollars"):
                        if kk in node[k] and node[k][kk] is not None:
                            return float(node[k][kk])
                    for kk in ("cents", "value_cents", "amount_cents"):
                        if kk in node[k] and node[k][kk] is not None:
                            return cents_to_usd(int(node[k][kk]))
                return float(node[k])
            except Exception:
                pass

    # 2) Cents-like hits
    cents_keys = [
        "cash_cents", "available_cash_cents", "cashAvailableCents", "availableCashCents",
        "available_cents", "cash_in_cents"
    ]
    for k in cents_keys:
        if k in node and node[k] is not None:
            try:
                return cents_to_usd(int(node[k]))
            except Exception:
                pass

    # 3) Heuristic: any key that contains 'cash' (safe, but controlled)
    for k, v in node.items():
        lk = str(k).lower()
        if "cash" in lk and v is not None:
            try:
                if is_dict(v):
                    for kk in ("usd", "value", "amount", "dollars"):
                        if kk in v and v[kk] is not None:
                            return float(v[kk])
                    for kk in ("cents", "value_cents", "amount_cents"):
                        if kk in v and v[kk] is not None:
                            return cents_to_usd(int(v[kk]))
                if isinstance(v, (int, float, str)):
                    # If it looks like cents (large int), we won't guess—only parse if key says cents
                    return float(v)
            except Exception:
                pass

    # 4) Recurse into children (bounded)
    for _, v in node.items():
        got = find_cash_recursively(v)
        if got is not None:
            return got

    return None

def auth_check(kc: KalshiClient) -> Optional[float]:
    """
    Returns cash USD float if parseable, else None.
    Your current shape: dict keys=['balance','portfolio_value','updated_ts']
    So we specifically recurse into 'balance' first.
    """
    paths = [
        kc.p("/portfolio/balance"),
        kc.p("/portfolio/balances"),
        kc.p("/portfolio"),
        kc.p("/account/balance"),
        kc.p("/account"),
    ]

    last_err = None
    last_shape = None

    for path in paths:
        r = kc.get(path)
        if r.status_code == 200:
            data = parse_json_safe(r)
            last_shape = summarize_shape(data)

            # Special: your observed top-level keys
            if is_dict(data) and "balance" in data:
                cash = find_cash_recursively(data["balance"])
                if cash is not None:
                    return cash

            # Fallback: recurse whole response
            cash = find_cash_recursively(data)
            if cash is not None:
                return cash

            log.info(f"Auth check OK (endpoint 200), but cash not parseable. shape={last_shape}")
            # Additional safe hint: show balance child shape if present
            if is_dict(data) and "balance" in data:
                log.info(f"Balance child shape: {summarize_shape(data['balance'])}")
            return None

        last_err = f"HTTP {r.status_code} {r.text[:200]}"

    raise RuntimeError(f"Auth check failed: {last_err} last_shape={last_shape}")

# ----------------------------
# Markets / orderbook
# ----------------------------
def list_markets(kc: KalshiClient, limit: int = 200) -> List[Dict[str, Any]]:
    # Do NOT rely on status filter—Kalshi status strings vary.
    r = kc.get(kc.p("/markets"), params={"limit": limit})
    if r.status_code != 200:
        raise RuntimeError(f"markets fetch failed: HTTP {r.status_code} {r.text[:200]}")
    data = parse_json_safe(r)

    markets = None
    if is_dict(data):
        markets = data.get("markets") or data.get("data") or data.get("results")
    elif is_list(data):
        markets = data

    if not is_list(markets):
        return []

    out = []
    for m in markets:
        if is_dict(m):
            out.append(m)
    return out

def list_openish_series_markets(kc: KalshiClient, series_prefix: str) -> List[Dict[str, Any]]:
    allm = list_markets(kc, limit=200)

    def status_ok(m: dict) -> bool:
        s = (m.get("status") or m.get("market_status") or "").lower()
        # include common values
        return s in ("open", "active", "trading", "listed", "")  # "" if API omits it

    out = []
    for m in allm:
        t = m.get("ticker") or ""
        if t.startswith(series_prefix) and status_ok(m):
            out.append(m)

    # newest tickers tend to sort higher lexicographically for these series
    out.sort(key=lambda x: (x.get("ticker") or ""), reverse=True)
    return out

def fetch_orderbook(kc: KalshiClient, ticker: str, depth: int = 1) -> Any:
    paths = [
        kc.p(f"/markets/{ticker}/orderbook"),
        kc.p(f"/markets/{ticker}/order-book"),
        kc.p(f"/markets/{ticker}/quotes"),
    ]
    for pth in paths:
        r = kc.get(pth, params={"depth": depth})
        if r.status_code == 200:
            return parse_json_safe(r)
    return None

def parse_level(level: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Supports:
    - {"price": 51, "quantity": 10}
    - {"p": 51, "q": 10}
    - [51, 10]
    """
    if is_dict(level):
        px = level.get("price", level.get("p"))
        qty = level.get("quantity", level.get("qty", level.get("q")))
        try: px_i = int(px) if px is not None else None
        except Exception: px_i = None
        try: qty_i = int(qty) if qty is not None else None
        except Exception: qty_i = None
        return px_i, qty_i

    if is_list(level) and len(level)